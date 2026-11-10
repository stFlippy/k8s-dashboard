import { DatabaseSync } from 'node:sqlite';
import { chmodSync, existsSync, mkdirSync } from 'node:fs';
import { dirname } from 'node:path';
import { createHash, randomBytes, randomUUID } from 'node:crypto';
import { environment } from '../config/environment.ts';

export interface AuthUser { id: string; username: string; mustChangePassword: boolean; isAdmin: boolean; }
export interface AuthSession { user: AuthUser; csrf: string; }
mkdirSync(dirname(environment.userDatabasePath), { recursive: true });
const db = new DatabaseSync(environment.userDatabasePath);
db.exec(`PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,username TEXT NOT NULL UNIQUE COLLATE NOCASE,password_hash TEXT NOT NULL,must_change INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY,user_id TEXT NOT NULL,csrf TEXT NOT NULL,expires_at INTEGER NOT NULL,created_at TEXT NOT NULL,FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS preferences(user_id TEXT PRIMARY KEY,data TEXT NOT NULL DEFAULT '{}',updated_at TEXT NOT NULL,FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE);`);
if (!(db.prepare('PRAGMA table_info(users)').all() as Array<{ name: string }>).some((column) => column.name === 'is_admin')) {
  db.exec('ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0');
  // Existing installations had no roles. Only the oldest account becomes administrator.
  db.exec('UPDATE users SET is_admin=1 WHERE id=(SELECT id FROM users ORDER BY created_at, rowid LIMIT 1)');
}
db.exec('CREATE TABLE IF NOT EXISTS security_notices(id TEXT PRIMARY KEY,user_id TEXT NOT NULL,message TEXT NOT NULL,created_at TEXT NOT NULL,FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE)');
db.exec('CREATE TABLE IF NOT EXISTS auth_migrations(name TEXT PRIMARY KEY)');
db.exec('BEGIN IMMEDIATE');
try {
  if (!db.prepare('SELECT name FROM auth_migrations WHERE name=?').get('external-auth-v1')) {
    db.exec('DELETE FROM sessions; UPDATE users SET must_change=0;');
    db.prepare('INSERT INTO auth_migrations(name) VALUES(?)').run('external-auth-v1');
  }
  db.exec('COMMIT');
} catch (error) {
  db.exec('ROLLBACK');
  throw error;
}
for (const path of [environment.userDatabasePath, `${environment.userDatabasePath}-shm`, `${environment.userDatabasePath}-wal`]) if (existsSync(path)) chmodSync(path, 0o600);
const hashToken = (value:string)=>createHash('sha256').update(value).digest('hex');
function validateUsername(value:string):string { const username=value.trim(); if(!username||username!==value||username.length>128||/[\u0000-\u001f\u007f]/u.test(username)) throw new Error('Username must contain 1-128 visible characters without leading or trailing spaces.'); return username; }
function rowUser(row:Record<string,unknown>):AuthUser{return{id:String(row.id),username:String(row.username),mustChangePassword:Boolean(row.must_change),isAdmin:Boolean(row.is_admin)};}
export function loginVerifiedUser(rawUsername:string):{user:AuthUser;token:string;csrf:string;created:boolean} {
  const username=validateUsername(rawUsername),now=new Date().toISOString();
  db.exec('BEGIN IMMEDIATE');
  try {
    let row=db.prepare('SELECT id,username,must_change,is_admin FROM users WHERE username=? COLLATE NOCASE').get(username) as Record<string,unknown>|undefined;
    if (row && row.username !== username) throw new Error('Имя пользователя отличается регистром от существующей учётной записи Quaber. Используйте точное написание логина.');
    const created=!row;
    if (!row) {
      const first=Number((db.prepare('SELECT COUNT(*) AS count FROM users').get() as {count:number}).count)===0;
      const id=randomUUID();
      db.prepare('INSERT INTO users(id,username,password_hash,must_change,created_at,updated_at,is_admin) VALUES(?,?,?,?,?,?,?)').run(id,username,'external-auth-only',0,now,now,first?1:0);
      row={id,username,must_change:0,is_admin:first?1:0};
    }
    const token=randomBytes(32).toString('base64url'),csrf=randomBytes(24).toString('base64url');
    db.prepare('DELETE FROM sessions WHERE expires_at<=?').run(Date.now());
    db.prepare('INSERT INTO sessions VALUES(?,?,?,?,?)').run(hashToken(token),String(row.id),csrf,Date.now()+12*60*60_000,now);
    db.exec('COMMIT');
    return {user:rowUser(row),token,csrf,created};
  } catch(error){db.exec('ROLLBACK');throw error;}
}
export function listUsers():AuthUser[]{return (db.prepare('SELECT id,username,must_change,is_admin FROM users ORDER BY username').all() as Record<string,unknown>[]).map(rowUser);}
export function setAdministrator(userId:string,admin:boolean,actorId:string):void {if(userId===actorId&&!admin)throw new Error('You cannot revoke your own administrator role.');const row=db.prepare('SELECT id FROM users WHERE id=?').get(userId);if(!row)throw new Error('User not found.');db.prepare('UPDATE users SET is_admin=?,updated_at=? WHERE id=?').run(admin?1:0,new Date().toISOString(),userId);db.prepare('DELETE FROM sessions WHERE user_id=?').run(userId);}
export function addSecurityNotice(userId:string,message:string):void {db.prepare('INSERT INTO security_notices VALUES(?,?,?,?)').run(randomUUID(),userId,message,new Date().toISOString());}
export function takeSecurityNotices(userId:string):string[] {const rows=db.prepare('SELECT id,message FROM security_notices WHERE user_id=? ORDER BY created_at').all(userId) as Array<{id:string;message:string}>;for(const row of rows)db.prepare('DELETE FROM security_notices WHERE id=?').run(row.id);return rows.map((row)=>row.message);}
export function session(token:string|undefined):AuthSession|null {if(!token)return null;const row=db.prepare('SELECT u.id,u.username,u.must_change,u.is_admin,s.csrf FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>?').get(hashToken(token),Date.now()) as Record<string,unknown>|undefined;return row?{user:rowUser(row),csrf:String(row.csrf)}:null;}
export function logout(token:string|undefined):void{if(token)db.prepare('DELETE FROM sessions WHERE token_hash=?').run(hashToken(token));}
export function getPreferences(userId:string):unknown{const row=db.prepare('SELECT data FROM preferences WHERE user_id=?').get(userId) as {data:string}|undefined;try{return JSON.parse(row?.data??'{}');}catch{return{};}}
export function setPreferences(userId:string,data:unknown):void{if(!data||typeof data!=='object'||Array.isArray(data)||Object.getPrototypeOf(data)!==Object.prototype)throw new Error('Preferences must be an object.');for(const[key,value]of Object.entries(data)){if(!key.startsWith('quaber.')||key.length>160||typeof value!=='string')throw new Error('Preferences contain an invalid entry.');}const value=JSON.stringify(data);if(value.length>1_000_000)throw new Error('Preferences are too large.');db.prepare('INSERT INTO preferences VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET data=excluded.data,updated_at=excluded.updated_at').run(userId,value,new Date().toISOString());}
