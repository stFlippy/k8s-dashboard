import test from 'node:test';
import assert from 'node:assert/strict';
import { assertPodDeletionAllowed } from './resources.ts';

const oldUid = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa';
const newUid = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb';

test('Pod deletion rejects stale UID even when name is unchanged', () => {
  assert.throws(() => assertPodDeletionAllowed({ uid: newUid, ownerReferences: [{ apiVersion: 'apps/v1', kind: 'ReplicaSet', name: 'rs', uid: 'cccccccc-cccc-cccc-cccc-cccccccccccc', controller: true }] }, oldUid, 'restart'), /changed since confirmation/);
});

test('unmanaged Pod cannot be described as restart, controlled Pod cannot use delete mode', () => {
  assert.throws(() => assertPodDeletionAllowed({ uid: oldUid }, oldUid, 'restart'), /no controller/);
  const controlled = { uid: oldUid, ownerReferences: [{ apiVersion: 'apps/v1', kind: 'ReplicaSet', name: 'rs', uid: newUid, controller: true }] };
  assert.throws(() => assertPodDeletionAllowed(controlled, oldUid, 'delete'), /restart action/);
  assert.doesNotThrow(() => assertPodDeletionAllowed(controlled, oldUid, 'restart'));
});
