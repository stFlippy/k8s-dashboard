import test from 'node:test';
import assert from 'node:assert/strict';
import * as k8s from '@kubernetes/client-node';
import { sanitizeLoginKubeconfig } from './store.ts';
import { authenticateKubectlLoginInput } from '../kubernetes/kubectl-login-cli.ts';
import type { ClusterInputDto } from '../types/api.ts';

function config(server = 'https://cluster.example:6443') {
  const kubeconfig = new k8s.KubeConfig();
  kubeconfig.loadFromOptions({
    clusters: [{ name: 'cluster', server, skipTLSVerify: true }],
    users: [{ name: 'user', token: 'header.payload.signature' }],
    contexts: [{ name: 'context', cluster: 'cluster', user: 'user' }],
    currentContext: 'context'
  });
  return kubeconfig.exportConfig();
}

test('rejects kubeconfig supplied by a client, even when a password is present', async () => {
  await assert.rejects(authenticateKubectlLoginInput({ name: 'demo', authType: 'kubectl-login', server: 'https://cluster.example:6443', username: 'user', password: 'password', kubeconfigData: config() } as ClusterInputDto), /Credentials must be obtained/);
});

test('rejects kubeconfig whose actual API endpoint differs from selected server', () => {
  assert.throws(() => sanitizeLoginKubeconfig(config('https://other.example:6443'), 'https://cluster.example:6443'), /does not match/);
});

test('rejects exec plugin and strips unused kubeconfig entries', () => {
  const malicious = new k8s.KubeConfig();
  malicious.loadFromOptions({
    clusters: [{ name: 'cluster', server: 'https://cluster.example:6443', skipTLSVerify: true }],
    users: [{ name: 'user', token: 'header.payload.signature', exec: { apiVersion: 'client.authentication.k8s.io/v1', command: '/bin/true' } }],
    contexts: [{ name: 'context', cluster: 'cluster', user: 'user' }],
    currentContext: 'context'
  });
  const unsafe = malicious.exportConfig();
  assert.throws(() => sanitizeLoginKubeconfig(unsafe, 'https://cluster.example:6443'), /unsafe kubeconfig/);
  const safe = sanitizeLoginKubeconfig(config(), 'https://cluster.example:6443');
  const parsed = new k8s.KubeConfig();
  parsed.loadFromString(safe);
  assert.equal(parsed.getCurrentCluster()?.server, 'https://cluster.example:6443');
  assert.equal(parsed.getCurrentUser()?.token, 'header.payload.signature');
  assert.equal(parsed.users.length, 1);
});
