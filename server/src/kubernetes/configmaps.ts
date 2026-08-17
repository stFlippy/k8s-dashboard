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
    binaryDataKeys: Object.keys(item.binaryData ?? {}),
    resource: item
  }));
}

export async function replaceConfigMap(
  clusterId: string,
  namespace: string,
  name: string,
  resource: unknown
): Promise<ConfigMapDto> {
  if (!resource || typeof resource !== 'object') throw new Error('ConfigMap resource must be a JSON object.');
  const configMap = resource as k8s.V1ConfigMap;
  if (configMap.metadata?.name && configMap.metadata.name !== name) throw new Error('metadata.name cannot be changed.');
  if (configMap.metadata?.namespace && configMap.metadata.namespace !== namespace) throw new Error('metadata.namespace cannot be changed.');
  configMap.metadata = { ...(configMap.metadata ?? {}), name, namespace };
  const { coreApi } = getClusterClients(clusterId);
  await coreApi.replaceNamespacedConfigMap({ namespace, name, body: configMap });
  const updated = await coreApi.readNamespacedConfigMap({ namespace, name });
  return {
    name: updated.metadata?.name ?? name,
    namespace: updated.metadata?.namespace ?? namespace,
    creationTimestamp: updated.metadata?.creationTimestamp?.toISOString() ?? null,
    data: updated.data ?? {},
    binaryDataKeys: Object.keys(updated.binaryData ?? {}),
    resource: updated
  };
}
