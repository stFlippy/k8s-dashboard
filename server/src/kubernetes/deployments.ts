import type * as k8s from '@kubernetes/client-node';
import { getClusterClients } from './cluster-client.ts';
import type { DeploymentDto } from '../types/api.ts';

export async function listDeployments(clusterId: string, namespace: string): Promise<DeploymentDto[]> {
  const { appsApi } = getClusterClients(clusterId);
  const response = await appsApi.listNamespacedDeployment({ namespace });

  return response.items.map((item: k8s.V1Deployment) => ({
    name: item.metadata?.name ?? 'unknown',
    namespace,
    replicas: item.spec?.replicas ?? 0,
    availableReplicas: item.status?.availableReplicas ?? 0,
    image: item.spec?.template?.spec?.containers?.[0]?.image ?? 'unknown'
  }));
}
