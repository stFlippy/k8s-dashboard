import * as k8s from '@kubernetes/client-node';
import { getResolvedCluster } from '../clusters/store.ts';
import type { ResolvedCluster } from '../clusters/types.ts';
import { getProxyKubeConfig } from './kubectl-proxy.ts';

export async function getKubeConfigForCluster(cluster: ResolvedCluster): Promise<k8s.KubeConfig> {
  return getProxyKubeConfig(cluster);
}

export async function getClusterKubeConfig(clusterId: string): Promise<k8s.KubeConfig> {
  return getKubeConfigForCluster(getResolvedCluster(clusterId));
}

export async function getClusterClients(clusterId: string) {
  const kubeConfig = await getClusterKubeConfig(clusterId);
  return {
    kubeConfig,
    coreApi: kubeConfig.makeApiClient(k8s.CoreV1Api),
    appsApi: kubeConfig.makeApiClient(k8s.AppsV1Api)
  };
}
