import test from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import type { ServerResponse } from 'node:http';

const directory = mkdtempSync(join(tmpdir(), 'quaber-mutation-audit-test-'));
process.env.USER_DATABASE_PATH = join(directory, 'users.sqlite');
const { runAsUser } = await import('../auth/request-context.ts');
const { auditMutation, systemStatus } = await import('./mutations.ts');

class FakeResponse extends EventEmitter {
  statusCode = 200;
  headers = new Map<string, string>();
  setHeader(name: string, value: string) { this.headers.set(name, value); return this; }
  getHeader(name: string) { return this.headers.get(name); }
}

test('mutation audit records exact target and final outcome without payload', async () => {
  const response = new FakeResponse();
  const url = new URL('http://localhost/api/pods/example?clusterId=cluster-1&namespace=demo&uid=aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa');
  runAsUser({ userId: 'user-1', username: 'alice' }, () => auditMutation('DELETE', url, response as unknown as ServerResponse));
  assert.ok(response.getHeader('X-Operation-Id'));
  response.statusCode = 204;
  response.emit('finish');
  const status = await systemStatus('user-1');
  assert.equal(status.recent.length, 1);
  const row = status.recent[0] as { name: string; kind: string; state: string; uid: string };
  assert.deepEqual({ name: row.name, kind: row.kind, state: row.state, uid: row.uid }, { name: 'example', kind: 'Pod', state: 'succeeded', uid: 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa' });
  assert.ok(status.databaseBytes > 0);
  assert.equal((await systemStatus('other-user')).recent.length, 0);
});

test.after(() => rmSync(directory, { recursive: true, force: true }));
