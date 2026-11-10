import type * as k8s from '@kubernetes/client-node';
import { getClusterClients } from './cluster-client.ts';
import type { PodDto } from '../types/api.ts';

function normalizePodStatus(pod: k8s.V1Pod): PodDto['status'] {
  if (pod.metadata?.deletionTimestamp) return 'Terminating';
  const phase = pod.status?.phase;
  if (phase === 'Failed') return 'Failed';

  if (phase === 'Running') return 'Running';
  if (phase === 'Pending') return 'Pending';
  if (phase === 'Succeeded') return 'Completed';
  return 'Unknown';
}

function toIso(value: Date | string | null | undefined): string | null {
  if (!value) return null;
  return value instanceof Date ? value.toISOString() : new Date(value).toISOString();
}

function lastRestartAt(containerStatuses: k8s.V1ContainerStatus[] | undefined): string | null {
  const candidates = (containerStatuses ?? [])
    .filter((container: k8s.V1ContainerStatus) => (container.restartCount ?? 0) > 0)
    .flatMap((container: k8s.V1ContainerStatus) => [
      toIso(container.state?.running?.startedAt),
      toIso(container.lastState?.terminated?.finishedAt),
      toIso(container.lastState?.terminated?.startedAt)
    ])
    .filter((value: string | null): value is string => Boolean(value));
  if (!candidates.length) return null;
  return candidates.sort((left: string, right: string) => right.localeCompare(left))[0];
}

export async function listPods(clusterId: string, namespace: string): Promise<PodDto[]> {
  const { coreApi, appsApi } = await getClusterClients(clusterId);
  const [response, replicaSets] = await Promise.all([
    coreApi.listNamespacedPod({ namespace }),
    appsApi.listNamespacedReplicaSet({ namespace })
  ]);
  const replicaSetDeployments = new Map(replicaSets.items.map((replicaSet) => [
    replicaSet.metadata?.uid ?? '',
    replicaSet.metadata?.ownerReferences?.find((owner) => owner.controller && owner.kind === 'Deployment')?.uid ?? null
  ]));

  return response.items.map((item: k8s.V1Pod) => ({
    name: item.metadata?.name ?? 'unknown',
    namespace: item.metadata?.namespace ?? namespace,
    uid: item.metadata?.uid ?? '',
    hasController: Boolean(item.metadata?.ownerReferences?.some((owner) => owner.controller)),
    ownerDeploymentUid: (() => {
      const owner = item.metadata?.ownerReferences?.find((reference) => reference.controller);
      return owner?.kind === 'ReplicaSet' ? replicaSetDeployments.get(owner.uid) ?? null : owner?.kind === 'Deployment' ? owner.uid : null;
    })(),
    status: normalizePodStatus(item),
    creationTimestamp: item.metadata?.creationTimestamp?.toISOString() ?? null,
    restartCount: item.status?.containerStatuses?.reduce(
      (sum: number, container: { restartCount?: number }) => sum + (container.restartCount ?? 0),
      0
    ) ?? 0,
    ip: item.status?.podIP ?? null,
    host: item.spec?.nodeName ?? null,
    lastRestartAt: lastRestartAt(item.status?.containerStatuses),
    terminating: Boolean(item.metadata?.deletionTimestamp)
  }));
}
