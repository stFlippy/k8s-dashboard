import type * as k8s from '@kubernetes/client-node';
import { getClusterClients } from './cluster-client.ts';
import type { ConfigMapDto } from '../types/api.ts';

export async function listConfigMaps(clusterId: string, namespace: string): Promise<ConfigMapDto[]> {
  const { coreApi } = getClusterClients(clusterId);
  const response = await coreApi.listNamespacedConfigMap({ namespace });
  return response.items.map((item: k8s.V1ConfigMap) => ({
    name: item.metadata?.name ?? 'unknown',
    namespace,
    creationTimestamp: item.metadata?.creationTimestamp?.toISOString() ?? null,
    data: item.data ?? {},
    binaryDataKeys: Object.keys(item.binaryData ?? {})
  }));
}
