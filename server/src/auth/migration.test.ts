import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { DatabaseSync } from 'node:sqlite';

const directory = mkdtempSync(join(tmpdir(), 'quaber-auth-migration-'));
const path = join(directory, 'users.sqlite');
const old = new DatabaseSync(path);
old.exec(`CREATE TABLE users(id TEXT PRIMARY KEY,username TEXT NOT NULL UNIQUE COLLATE NOCASE,password_hash TEXT NOT NULL,must_change INTEGER NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,is_admin INTEGER NOT NULL);
CREATE TABLE sessions(token_hash TEXT PRIMARY KEY,user_id TEXT NOT NULL,csrf TEXT NOT NULL,expires_at INTEGER NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE preferences(user_id TEXT PRIMARY KEY,data TEXT NOT NULL,updated_at TEXT NOT NULL);`);
old.prepare('INSERT INTO users VALUES(?,?,?,?,?,?,?)').run('old-id', 'old-admin', 'legacy-hash', 1, '2026-01-01', '2026-01-01', 1);
old.prepare('INSERT INTO sessions VALUES(?,?,?,?,?)').run('old-token', 'old-id', 'old-csrf', Date.now() + 3600_000, '2026-01-01');
old.prepare('INSERT INTO preferences VALUES(?,?,?)').run('old-id', '{"quaber.theme":"light"}', '2026-01-01');
old.close();
process.env.USER_DATABASE_PATH = path;
const { applicationConfig } = await import('../config/application.ts');
applicationConfig.authentication.mode = 'external';
const auth = await import('./store.ts');

test('external auth migration preserves users, roles and preferences, but expires old sessions', () => {
  const users = auth.listUsers();
  assert.equal(users.length, 1);
  assert.equal(users[0].id, 'old-id');
  assert.equal(users[0].isAdmin, true);
  assert.equal(users[0].mustChangePassword, false);
  assert.deepEqual(auth.getPreferences('old-id'), { 'quaber.theme': 'light' });
  const database = new DatabaseSync(path, { readOnly: true });
  try {
    assert.equal((database.prepare('SELECT COUNT(*) AS count FROM sessions').get() as { count: number }).count, 0);
    assert.equal((database.prepare('SELECT password_hash FROM users WHERE id=?').get('old-id') as { password_hash: string }).password_hash, 'legacy-hash');
  } finally { database.close(); }
  const login = auth.loginVerifiedUser('old-admin');
  assert.equal(login.user.id, 'old-id');
  assert.equal(login.user.isAdmin, true);
});

test.after(() => rmSync(directory, { recursive: true, force: true }));
