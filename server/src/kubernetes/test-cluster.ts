import { getClusterClients } from './cluster-client.ts';
import { getKubeConfigForCluster } from './cluster-client.ts';
import { resolveClusterInput } from '../clusters/store.ts';
import type { ClusterInputDto } from '../types/api.ts';
import * as k8s from '@kubernetes/client-node';
import { authenticateKubectlLoginInput } from './kubectl-login-cli.ts';
import { stopClusterProxy } from './kubectl-proxy.ts';

export async function testCluster(clusterId: string) {
  const { kubeConfig, coreApi } = await getClusterClients(clusterId);
  const response = await coreApi.listNamespace();
  return {
    ok: true,
    context: kubeConfig.getCurrentContext(),
    namespaces: response.items.length
  };
}

export async function testClusterInput(input: ClusterInputDto, existingId?: string) {
  const authenticated = await authenticateKubectlLoginInput(input, existingId);
  const cluster = resolveClusterInput(authenticated, existingId);
  try {
    const config = await getKubeConfigForCluster(cluster);
    const response = await config.makeApiClient(k8s.CoreV1Api).listNamespace();
    return { ok: true, context: config.getCurrentContext(), namespaces: response.items.length };
  } finally {
    stopClusterProxy(cluster.id, 'connection test completed');
  }
}
