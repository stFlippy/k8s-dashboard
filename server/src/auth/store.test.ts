import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { DatabaseSync } from 'node:sqlite';

const directory = mkdtempSync(join(tmpdir(), 'quaber-auth-test-'));
process.env.USER_DATABASE_PATH = join(directory, 'users.sqlite');
const { applicationConfig } = await import('../config/application.ts');
applicationConfig.authentication.mode = 'external';
const auth = await import('./store.ts');

test('first externally verified user is administrator, later users are not', () => {
  const first = auth.loginVerifiedUser('first@example.test');
  const second = auth.loginVerifiedUser('second@example.test');
  assert.equal(first.created, true);
  assert.equal(first.user.isAdmin, true);
  assert.equal(first.user.mustChangePassword, false);
  assert.equal(second.user.isAdmin, false);
  const existing = auth.loginVerifiedUser('first@example.test');
  assert.equal(existing.created, false);
  assert.equal(existing.user.id, first.user.id);
  assert.throws(() => auth.loginVerifiedUser('FIRST@example.test'), /отличается регистром/);

  const database = new DatabaseSync(process.env.USER_DATABASE_PATH!, { readOnly: true });
  try {
    const hash = database.prepare('SELECT password_hash FROM users WHERE id=?').get(first.user.id) as { password_hash: string };
    assert.equal(hash.password_hash, 'external-auth-only');
  } finally { database.close(); }
});

test('administrator roles, sessions and preferences remain local', () => {
  const first = auth.loginVerifiedUser('first@example.test');
  const second = auth.loginVerifiedUser('second@example.test');
  auth.setPreferences(first.user.id, { 'quaber.theme': 'light' });
  auth.setAdministrator(second.user.id, true, first.user.id);
  assert.equal(auth.session(second.token), null);
  assert.equal(auth.loginVerifiedUser(second.user.username).user.isAdmin, true);
  assert.deepEqual(auth.getPreferences(first.user.id), { 'quaber.theme': 'light' });
  assert.throws(() => auth.setAdministrator(first.user.id, false, first.user.id), /cannot revoke/);
});

test.after(() => rmSync(directory, { recursive: true, force: true }));
