import { DatabaseSync } from 'node:sqlite';
import { chmodSync, closeSync, constants, existsSync, fstatSync, lstatSync, mkdirSync, openSync, readFileSync, unlinkSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { createHash, randomBytes, randomUUID, scryptSync, timingSafeEqual } from 'node:crypto';
import { applicationConfig } from '../config/application.ts';
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
db.exec('CREATE TABLE IF NOT EXISTS auth_runtime_state(name TEXT PRIMARY KEY,value TEXT NOT NULL)');
db.exec('BEGIN IMMEDIATE');
try {
  if (!db.prepare('SELECT name FROM auth_migrations WHERE name=?').get('external-auth-v1')) {
    db.exec('DELETE FROM sessions;');
    db.prepare('INSERT INTO auth_migrations(name) VALUES(?)').run('external-auth-v1');
  }
  const previousMode = db.prepare('SELECT value FROM auth_runtime_state WHERE name=?').get('mode') as { value: string } | undefined;
  if (previousMode?.value !== applicationConfig.authentication.mode) {
    db.exec('DELETE FROM sessions;');
    db.prepare('INSERT INTO auth_runtime_state(name,value) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value').run('mode', applicationConfig.authentication.mode);
  }
  db.exec('COMMIT');
} catch (error) {
  db.exec('ROLLBACK');
  throw error;
}
for (const path of [environment.userDatabasePath, `${environment.userDatabasePath}-shm`, `${environment.userDatabasePath}-wal`]) if (existsSync(path)) chmodSync(path, 0o600);
const hashToken = (value:string)=>createHash('sha256').update(value).digest('hex');
function validateUsername(value:string):string { const username=value.trim(); if(!username||username!==value||username.length>128||/[\u0000-\u001f\u007f]/u.test(username)) throw new Error('Username must contain 1-128 visible characters without leading or trailing spaces.'); return username; }
function validatePassword(value:string):void { if(value.length<12||value.length>256) throw new Error('Пароль должен содержать от 12 до 256 символов.'); }
function hashPassword(password:string):string { const salt=randomBytes(16); const hash=scryptSync(password,salt,64); return `scrypt$${salt.toString('base64')}$${hash.toString('base64')}`; }
function verifyPassword(password:string,stored:string):boolean {
  try {
    const parts=stored.split('$');
    if(parts.length!==3||parts[0]!=='scrypt')return false;
    const salt=Buffer.from(parts[1],'base64'),expected=Buffer.from(parts[2],'base64');
    if(salt.length!==16||expected.length!==64)return false;
    return timingSafeEqual(expected,scryptSync(password,salt,expected.length));
  } catch {return false;}
}
const dummyHash=hashPassword(randomBytes(24).toString('base64url'));
function rowUser(row:Record<string,unknown>):AuthUser{return{id:String(row.id),username:String(row.username),mustChangePassword:applicationConfig.authentication.mode==='local'&&Boolean(row.must_change),isAdmin:Boolean(row.is_admin)};}
export function generateTemporaryPassword():string{return `${randomBytes(18).toString('base64url')}!7a`;}
export function createLocalUser(rawUsername:string,password:string,mustChange=true,isAdmin=false):AuthUser {
  const username=validateUsername(rawUsername);validatePassword(password);
  const id=randomUUID(),now=new Date().toISOString();
  try {db.prepare('INSERT INTO users(id,username,password_hash,must_change,created_at,updated_at,is_admin) VALUES(?,?,?,?,?,?,?)').run(id,username,hashPassword(password),mustChange?1:0,now,now,isAdmin?1:0);}
  catch(error){if(error instanceof Error&&error.message.toLowerCase().includes('unique'))throw new Error(`Пользователь «${username}» уже существует.`);throw error;}
  return{id,username,mustChangePassword:mustChange,isAdmin};
}
export function loginLocalUser(rawUsername:string,password:string):{user:AuthUser;token:string;csrf:string;created:false}|null {
  const username=validateUsername(rawUsername);
  const row=db.prepare('SELECT id,username,password_hash,must_change,is_admin FROM users WHERE username=? COLLATE NOCASE').get(username) as Record<string,unknown>|undefined;
  const accepted=verifyPassword(password,row?String(row.password_hash):dummyHash);
  if(!row||row.username!==username||!accepted)return null;
  const token=randomBytes(32).toString('base64url'),csrf=randomBytes(24).toString('base64url');
  db.prepare('DELETE FROM sessions WHERE expires_at<=?').run(Date.now());
  db.prepare('INSERT INTO sessions VALUES(?,?,?,?,?)').run(hashToken(token),String(row.id),csrf,Date.now()+12*60*60_000,new Date().toISOString());
  return{user:rowUser(row),token,csrf,created:false};
}
export function changeLocalPassword(userId:string,current:string,next:string):void {
  validatePassword(next);
  const row=db.prepare('SELECT password_hash FROM users WHERE id=?').get(userId) as {password_hash:string}|undefined;
  if(!row||!verifyPassword(current,row.password_hash))throw new Error('Текущий пароль указан неверно.');
  if(current===next)throw new Error('Новый пароль должен отличаться от текущего.');
  db.prepare('UPDATE users SET password_hash=?,must_change=0,updated_at=? WHERE id=?').run(hashPassword(next),new Date().toISOString(),userId);
  db.prepare('DELETE FROM sessions WHERE user_id=?').run(userId);
}
export function resetLocalPassword(userId:string,password:string):void {
  validatePassword(password);
  if(!db.prepare('SELECT id FROM users WHERE id=?').get(userId))throw new Error('Пользователь не найден.');
  db.prepare('UPDATE users SET password_hash=?,must_change=1,updated_at=? WHERE id=?').run(hashPassword(password),new Date().toISOString(),userId);
  db.prepare('DELETE FROM sessions WHERE user_id=?').run(userId);
}
export function importLocalBootstrapUser(path=join(dirname(environment.userDatabasePath),'bootstrap-user.json')):boolean {
  if(!existsSync(path))return false;
  const descriptor=openSync(path,constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0));
  let file: ReturnType<typeof fstatSync>;
  let input: {username?:unknown;password?:unknown};
  try {
    file=fstatSync(descriptor);
    if(!file.isFile()||(process.platform!=='win32'&&((file.mode&0o077)!==0||(process.getuid&&file.uid!==process.getuid()))))throw new Error('bootstrap-user.json должен быть обычным файлом владельца процесса с правами 0600.');
    input=JSON.parse(readFileSync(descriptor,'utf8')) as {username?:unknown;password?:unknown};
  } finally {closeSync(descriptor);}
  if(typeof input?.username!=='string'||typeof input.password!=='string')throw new Error('bootstrap-user.json требует username и password.');
  const username=validateUsername(input.username);validatePassword(input.password);
  const count=Number((db.prepare('SELECT COUNT(*) AS count FROM users').get() as {count:number}).count);
  if(count===0)createLocalUser(username,input.password,true,true);
  else {
    const row=db.prepare('SELECT id,username,is_admin FROM users WHERE username=? COLLATE NOCASE').get(username) as {id:string;username:string;is_admin:number}|undefined;
    if(!row||row.username!==username||!row.is_admin)throw new Error('Для существующей базы bootstrap-user.json должен указывать точное имя администратора.');
    resetLocalPassword(row.id,input.password);
  }
  const current=lstatSync(path);
  if(!current.isFile()||current.ino!==file.ino||current.dev!==file.dev)throw new Error('bootstrap-user.json изменился во время чтения; файл не удалён.');
  unlinkSync(path);
  console.log('One-time local administrator password imported; bootstrap file removed.');
  return true;
}
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

if(applicationConfig.authentication.mode==='local') {
  importLocalBootstrapUser();
  const available=Number((db.prepare("SELECT COUNT(*) AS count FROM users WHERE is_admin=1 AND password_hash LIKE 'scrypt$%'").get() as {count:number}).count);
  if(available===0)throw new Error(`Локальная авторизация: нет администратора с локальным паролем. Создайте ${join(dirname(environment.userDatabasePath),'bootstrap-user.json')} с правами 0600 и перезапустите Quaber.`);
}
