import type * as k8s from '@kubernetes/client-node';
import { getClusterClients } from './cluster-client.ts';
import type { ResourceQuotaDto } from '../types/api.ts';

function quantities(source: Record<string, unknown> | undefined): Record<string, string> {
  return Object.fromEntries(Object.entries(source ?? {}).map(([key, value]) => [key, String(value)]));
}

export async function listResourceQuotas(clusterId: string, namespace: string): Promise<ResourceQuotaDto[]> {
  const { coreApi } = getClusterClients(clusterId);
  const response = await coreApi.listNamespacedResourceQuota({ namespace });
  return response.items.map((item: k8s.V1ResourceQuota) => ({
    name: item.metadata?.name ?? 'unknown',
    namespace,
    hard: quantities(item.status?.hard as Record<string, unknown> | undefined),
    used: quantities(item.status?.used as Record<string, unknown> | undefined)
  }));
}
