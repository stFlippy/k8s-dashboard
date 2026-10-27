import assert from 'node:assert/strict';
import test from 'node:test';
import { chmodSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { runLogin } from './kubectl-login-cli.ts';

test('kubectl login receives URL, username and password flags and reads its kubeconfig', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'quaber-login-test-'));
  try {
    const executable = join(directory, 'kubectl');
    const kubeconfig = join(directory, 'config');
    writeFileSync(executable, '#!/bin/sh\nprintf "%s\\n" "$@" > "$KUBECONFIG.args"\nprintf "kind: Config\\n" > "$KUBECONFIG"\n');
    chmodSync(executable, 0o700);
    const value = await runLogin('https://cluster.example:6443', 'alice', 'test-password', kubeconfig, { ...process.env, KUBECONFIG: kubeconfig }, executable);
    assert.equal(value, 'kind: Config\n');
    assert.deepEqual(readFileSync(`${kubeconfig}.args`, 'utf8').trimEnd().split('\n'), [
      'login', 'https://cluster.example:6443', '--username', 'alice', '--password', 'test-password'
    ]);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test('kubectl stderr is logged verbatim except for the supplied password', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'quaber-login-test-'));
  const originalError = console.error;
  const logLines: string[] = [];
  try {
    const executable = join(directory, 'kubectl');
    const kubeconfig = join(directory, 'config');
    writeFileSync(executable, '#!/bin/sh\nprintf "kubectl: original failure with password test-password\\n" >&2\nexit 7\n');
    chmodSync(executable, 0o700);
    console.error = (...values: unknown[]) => { logLines.push(values.map(String).join(' ')); };
    await assert.rejects(
      runLogin('https://cluster.example:6443', 'alice', 'test-password', kubeconfig, { ...process.env, KUBECONFIG: kubeconfig }, executable),
      /kubectl: original failure with password \[REDACTED\]/
    );
    assert.match(logLines.join('\n'), /kubectl: original failure with password \[REDACTED\]/);
    assert.doesNotMatch(logLines.join('\n'), /test-password/);
  } finally {
    console.error = originalError;
    rmSync(directory, { recursive: true, force: true });
  }
});
