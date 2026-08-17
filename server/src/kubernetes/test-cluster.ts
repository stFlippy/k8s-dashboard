import { getClusterClients } from './cluster-client.ts';
import { getKubeConfigForCluster } from './cluster-client.ts';
import { resolveClusterInput } from '../clusters/store.ts';
import type { ClusterInputDto } from '../types/api.ts';
import * as k8s from '@kubernetes/client-node';

export async function testCluster(clusterId: string) {
  const { kubeConfig, coreApi } = getClusterClients(clusterId);
  const response = await coreApi.listNamespace();
  return {
    ok: true,
    context: kubeConfig.getCurrentContext(),
    namespaces: response.items.length
  };
}

export async function testClusterInput(input: ClusterInputDto, existingId?: string) {
  const config = getKubeConfigForCluster(resolveClusterInput(input, existingId));
  const response = await config.makeApiClient(k8s.CoreV1Api).listNamespace();
  return { ok: true, context: config.getCurrentContext(), namespaces: response.items.length };
}
