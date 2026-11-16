import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { Readable } from 'node:stream';
import type { IncomingMessage, ServerResponse } from 'node:http';

const directory = mkdtempSync(join(tmpdir(), 'quaber-auth-route-test-'));
process.env.USER_DATABASE_PATH = join(directory, 'users.sqlite');
const { applicationConfig } = await import('../config/application.ts');
applicationConfig.authentication.mode = 'external';
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

test('remote false rejects, true creates first admin and later regular user', async () => {
  const originalFetch = globalThis.fetch;
  let accepted = false;
  globalThis.fetch = async (_url, init) => {
    assert.equal(init?.method, 'POST');
    const sent = JSON.parse(String(init?.body)) as { username: string; password: string };
    assert.ok(sent.username);
    assert.equal(sent.password, 'remote-password');
    return new Response(JSON.stringify(accepted), { status: 200, headers: { 'Content-Type': 'application/json' } });
  };
  try {
    const rejected = await call('/api/auth/login', 'POST', { username: 'alice', password: 'remote-password' });
    assert.equal(rejected.status, 401);
    assert.deepEqual(auth.listUsers(), []);
    accepted = true;
    const first = await call('/api/auth/login', 'POST', { username: 'alice', password: 'remote-password' });
    assert.equal(first.status, 200);
    assert.equal(first.data.isAdmin, true);
    assert.ok(first.headers.get('Set-Cookie')?.includes('HttpOnly'));
    const second = await call('/api/auth/login', 'POST', { username: 'bob', password: 'remote-password' });
    assert.equal(second.data.isAdmin, false);
    assert.equal(auth.listUsers().length, 2);
  } finally { globalThis.fetch = originalFetch; }
});

test('remote outage fails closed; local password endpoints are disabled', async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => { throw new Error('test outage'); };
  try {
    const failed = await call('/api/auth/login', 'POST', { username: 'mallory', password: 'remote-password' });
    assert.equal(failed.status, 502);
    assert.equal(auth.listUsers().length, 2);
  } finally { globalThis.fetch = originalFetch; }
  const admin = auth.loginVerifiedUser('alice');
  const oldChange = await call('/api/auth/change-password', 'POST', {}, admin);
  assert.equal(oldChange.status, 410);
  const oldReset = await call(`/api/users/${admin.user.id}/reset-password`, 'POST', {}, admin);
  assert.equal(oldReset.status, 410);
});

test('first administrator can grant roles; regular users cannot', async () => {
  const administrator = auth.loginVerifiedUser('alice');
  const member = auth.loginVerifiedUser('bob');
  const forbidden = await call(`/api/users/${administrator.user.id}/administrator`, 'PUT', { isAdmin: false }, member);
  assert.equal(forbidden.status, 403);
  const granted = await call(`/api/users/${member.user.id}/administrator`, 'PUT', { isAdmin: true }, administrator);
  assert.equal(granted.status, 200);
  assert.equal(auth.listUsers().find((user) => user.id === member.user.id)?.isAdmin, true);
});

test('non-boolean remote response cannot create a local user', async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response('{"authenticated":true}', { status: 200, headers: { 'Content-Type': 'application/json' } });
  try {
    const failed = await call('/api/auth/login', 'POST', { username: 'unexpected', password: 'remote-password' });
    assert.equal(failed.status, 502);
    assert.equal(auth.listUsers().some((user) => user.username === 'unexpected'), false);
  } finally { globalThis.fetch = originalFetch; }
});

test.after(() => rmSync(directory, { recursive: true, force: true }));
