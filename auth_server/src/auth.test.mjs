import test from 'node:test';
import assert from 'node:assert/strict';
import { randomBytes, scryptSync } from 'node:crypto';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { verifyCredentials } from './auth.mjs';

const directory = mkdtempSync(join(tmpdir(), 'quaber-auth-mock-test-'));
const path = join(directory, 'users.json');
const salt = randomBytes(16);
const hash = scryptSync('known-password', salt, 64);
writeFileSync(path, JSON.stringify({ users: [{ username: 'alice', passwordHash: `scrypt$${salt.toString('base64')}$${hash.toString('base64')}` }] }));

test('mock returns true only for matching credentials', () => {
  assert.equal(verifyCredentials('alice', 'known-password', path), true);
  assert.equal(verifyCredentials('alice', 'wrong-password', path), false);
  assert.equal(verifyCredentials('absent', 'known-password', path), false);
  assert.equal(verifyCredentials('ALICE', 'known-password', path), false);
});

test.after(() => rmSync(directory, { recursive: true, force: true }));
