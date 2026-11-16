import test from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { Readable } from 'node:stream';
import { DatabaseSync } from 'node:sqlite';
import type { IncomingMessage, ServerResponse } from 'node:http';

const directory = mkdtempSync(join(tmpdir(), 'quaber-local-auth-test-'));
const databasePath = join(directory, 'users.sqlite');
const bootstrapPath = join(directory, 'bootstrap-user.json');
const existing = new DatabaseSync(databasePath);
existing.exec(`CREATE TABLE users(id TEXT PRIMARY KEY,username TEXT NOT NULL UNIQUE COLLATE NOCASE,password_hash TEXT NOT NULL,must_change INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,is_admin INTEGER NOT NULL DEFAULT 0);
CREATE TABLE sessions(token_hash TEXT PRIMARY KEY,user_id TEXT NOT NULL,csrf TEXT NOT NULL,expires_at INTEGER NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE preferences(user_id TEXT PRIMARY KEY,data TEXT NOT NULL DEFAULT '{}',updated_at TEXT NOT NULL);
CREATE TABLE auth_migrations(name TEXT PRIMARY KEY);
CREATE TABLE auth_runtime_state(name TEXT PRIMARY KEY,value TEXT NOT NULL);
INSERT INTO users VALUES('existing-admin','local-admin','external-auth-only',0,'2026-01-01','2026-01-01',1);
INSERT INTO sessions VALUES('old-session','existing-admin','old-csrf',9999999999999,'2026-01-01');
INSERT INTO preferences VALUES('existing-admin','{"quaber.theme":"dark"}','2026-01-01');
INSERT INTO auth_migrations VALUES('external-auth-v1');
INSERT INTO auth_runtime_state VALUES('mode','external');`);
existing.close();
writeFileSync(bootstrapPath, JSON.stringify({ username: 'local-admin', password: 'TemporaryPass!123' }), { mode: 0o600 });
process.env.USER_DATABASE_PATH = databasePath;
const { applicationConfig } = await import('../config/application.ts');
applicationConfig.authentication.mode = 'local';
const auth = await import('./store.ts');
const { handleAuthRequest } = await import('./router.ts');

async function call(path: string, method: string, payload: unknown, session?: { token: string; csrf: string }) {
  const request = Readable.from([JSON.stringify(payload)]) as unknown as IncomingMessage;
  request.method = method;
  request.socket = { remoteAddress: '127.0.0.1' } as IncomingMessage['socket'];
  request.headers = session ? { cookie: `quaber_session=${session.token}`, 'x-csrf-token': session.csrf } : {};
  const headers = new Map<string, string>();
  let output = '';
  const response = {
    statusCode: 200,
    setHeader: (key: string, value: string) => { headers.set(key, value); },
    end: (data: string) => { output = data; }
  } as unknown as ServerResponse;
  await handleAuthRequest(request, response, new URL(`http://localhost${path}`));
  return { status: response.statusCode, data: JSON.parse(output) as Record<string, unknown>, headers };
}

test('one-time bootstrap converts an existing local database into a working administrator', () => {
  assert.equal(existsSync(bootstrapPath), false);
  const users = auth.listUsers();
  assert.equal(users.length, 1);
  assert.equal(users[0].id, 'existing-admin');
  assert.equal(users[0].isAdmin, true);
  assert.equal(users[0].mustChangePassword, true);
  const database = new DatabaseSync(databasePath, { readOnly: true });
  try {
    const row = database.prepare('SELECT password_hash FROM users WHERE id=?').get(users[0].id) as { password_hash: string };
    assert.match(row.password_hash, /^scrypt\$/);
    assert.equal(row.password_hash.includes('TemporaryPass!123'), false);
    assert.equal((database.prepare('SELECT COUNT(*) AS count FROM sessions').get() as { count: number }).count, 0);
    assert.equal((database.prepare('SELECT data FROM preferences WHERE user_id=?').get(users[0].id) as { data: string }).data, '{"quaber.theme":"dark"}');
  } finally { database.close(); }
});

test('local login never calls external provider and requires password change', async () => {
  const previousFetch = globalThis.fetch;
  globalThis.fetch = async () => { throw new Error('external provider must not be called'); };
  try {
    const wrong = await call('/api/auth/login', 'POST', { username: 'local-admin', password: 'wrong-password' });
    assert.equal(wrong.status, 401);
    const first = await call('/api/auth/login', 'POST', { username: 'local-admin', password: 'TemporaryPass!123' });
    assert.equal(first.status, 200);
    assert.equal(first.data.authMode, 'local');
    assert.equal(first.data.mustChangePassword, true);
    const current = auth.loginLocalUser('local-admin', 'TemporaryPass!123');
    assert.ok(current);
    const forbidden = await call('/api/users', 'GET', {}, current);
    assert.equal(forbidden.status, 403);
    const mismatch = await call('/api/auth/change-password', 'POST', { currentPassword: 'TemporaryPass!123', newPassword: 'PermanentPass!123', repeatPassword: 'different' }, current);
    assert.equal(mismatch.status, 400);
    const changed = await call('/api/auth/change-password', 'POST', { currentPassword: 'TemporaryPass!123', newPassword: 'PermanentPass!123', repeatPassword: 'PermanentPass!123' }, current);
    assert.equal(changed.status, 200);
    assert.equal(auth.session(current.token), null);
    assert.equal(auth.loginLocalUser('local-admin', 'TemporaryPass!123'), null);
    assert.equal(auth.loginLocalUser('local-admin', 'PermanentPass!123')?.user.mustChangePassword, false);
  } finally { globalThis.fetch = previousFetch; }
});

test('administrator creates and resets local users without changing their existing identity', async () => {
  const admin = auth.loginLocalUser('local-admin', 'PermanentPass!123');
  assert.ok(admin);
  const created = await call('/api/users', 'POST', { username: 'local-member' }, admin);
  assert.equal(created.status, 201);
  assert.equal(typeof created.data.temporaryPassword, 'string');
  const member = created.data.user as { id: string; username: string };
  const firstPassword = String(created.data.temporaryPassword);
  assert.equal(auth.loginLocalUser(member.username, firstPassword)?.user.mustChangePassword, true);
  const reset = await call(`/api/users/${member.id}/reset-password`, 'POST', {}, admin);
  assert.equal(reset.status, 200);
  assert.equal(auth.loginLocalUser(member.username, firstPassword), null);
  assert.equal(auth.loginLocalUser(member.username, String(reset.data.temporaryPassword))?.user.mustChangePassword, true);
  const duplicate = await call('/api/users', 'POST', { username: 'LOCAL-MEMBER' }, admin);
  assert.equal(duplicate.status, 400);
  const externalOnly = auth.loginVerifiedUser('external-placeholder');
  assert.equal(auth.loginLocalUser(externalOnly.user.username, 'fake-password'), null);
});

test.after(() => rmSync(directory, { recursive: true, force: true }));
