import test from 'node:test';
import assert from 'node:assert/strict';
import { mergeContainerResources } from './resources.ts';

test('CPU/RAM edit preserves unrelated requests, limits and claims', () => {
  const original = {
    requests: { cpu: '100m', memory: '128Mi', 'ephemeral-storage': '2Gi', 'example.com/hugepage': '1Gi' },
    limits: { cpu: '1', memory: '512Mi', 'nvidia.com/gpu': '1' },
    claims: [{ name: 'model-cache' }]
  };
  const updated = mergeContainerResources(original, { containerName: 'worker', cpuRequest: '250m', memoryLimit: null });
  assert.equal(updated.requests?.cpu, '250m');
  assert.equal(updated.requests?.['ephemeral-storage'], '2Gi');
  assert.equal(updated.requests?.['example.com/hugepage'], '1Gi');
  assert.equal(updated.limits?.['nvidia.com/gpu'], '1');
  assert.equal(updated.limits?.memory, undefined);
  assert.deepEqual(updated.claims, original.claims);
  assert.equal(original.requests.cpu, '100m');
});
