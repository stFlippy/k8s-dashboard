import assert from 'node:assert/strict';
import test from 'node:test';
import { describeKubectlLoginFailure } from './login-diagnostics.ts';

test('shows a parameter mismatch reported only on stdout', () => {
  const message = describeKubectlLoginFailure('', 'Error: unknown flag: --server\nSee kubectl login --help for usage', 1);
  assert.match(message, /не принял параметры/);
});

test('distinguishes an exit without diagnostic output', () => {
  const message = describeKubectlLoginFailure('', '', 1);
  assert.match(message, /без диагностического вывода/);
  assert.match(message, /--password/);
});

test('never forwards an unclassified client message containing a token', () => {
  const message = describeKubectlLoginFailure('', 'Error: token=super-secret-token-123', 1);
  assert.doesNotMatch(message, /super-secret-token-123/);
  assert.match(message, /безопасное для показа сообщение/);
});

test('classifies TLS errors without exposing certificate contents', () => {
  const message = describeKubectlLoginFailure('x509: certificate signed by unknown authority', '', 1);
  assert.match(message, /TLS-сертификат/);
});
