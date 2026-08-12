import { getClusterClients } from './cluster-client.ts';

export async function testCluster(clusterId: string) {
  const { kubeConfig, coreApi } = getClusterClients(clusterId);
  const response = await coreApi.listNamespace();
  return {
    ok: true,
    context: kubeConfig.getCurrentContext(),
    namespaces: response.items.length
  };
}
