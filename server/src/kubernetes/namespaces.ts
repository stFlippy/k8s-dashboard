import type * as k8s from '@kubernetes/client-node';
import { getClusterClients } from './cluster-client.ts';
import type { NamespaceDto } from '../types/api.ts';

export async function listNamespaces(clusterId: string): Promise<NamespaceDto[]> {
  const { coreApi } = await getClusterClients(clusterId);
  const response = await coreApi.listNamespace();
  return response.items
    .map((item: k8s.V1Namespace) => ({
      name: item.metadata?.name ?? 'unknown',
      age: item.metadata?.creationTimestamp?.toISOString() ?? ''
    }))
    .sort((left: NamespaceDto, right: NamespaceDto) => left.name.localeCompare(right.name));
}
