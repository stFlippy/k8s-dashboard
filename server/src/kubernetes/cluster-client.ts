import * as k8s from '@kubernetes/client-node';
import { getDecryptedCredentials, getResolvedCluster } from '../clusters/store.ts';

function normalizeCaData(value: string | undefined): string | undefined {
  if (!value) return undefined;
  if (value.includes('BEGIN CERTIFICATE')) return Buffer.from(value, 'utf8').toString('base64');
  return value.replace(/\s+/g, '');
}

export function getClusterKubeConfig(clusterId: string): k8s.KubeConfig {
  const cluster = getResolvedCluster(clusterId);
  const kc = new k8s.KubeConfig();

  if (cluster.authType === 'kubeconfig') {
    const source = new k8s.KubeConfig();
    if (cluster.kubeconfigPath) source.loadFromFile(cluster.kubeconfigPath);
    else source.loadFromDefault();
    if (cluster.context) source.setCurrentContext(cluster.context);

    // Isolate cluster/user names per dashboard entry. The client library caches
    // some credentials by kubeconfig username, so unique names reduce collisions
    // when several kubeconfigs use generic user names such as "admin".
    const sourceCluster = source.getCurrentCluster();
    if (!sourceCluster) throw new Error(`No active cluster in kubeconfig for '${cluster.name}'.`);
    const sourceUser = source.getCurrentUser();
    const clusterName = `cluster-${cluster.id}`;
    const userName = `user-${cluster.id}`;
    const contextName = `context-${cluster.id}`;

    kc.loadFromOptions({
      clusters: [{ ...sourceCluster, name: clusterName }],
      users: [{ ...(sourceUser ?? {}), name: userName }],
      contexts: [{ name: contextName, cluster: clusterName, user: userName }],
      currentContext: contextName
    });
    return kc;
  }

  const credentials = getDecryptedCredentials(cluster);
  const clusterName = `cluster-${cluster.id}`;
  const userName = `user-${cluster.id}`;
  const contextName = `context-${cluster.id}`;

  kc.loadFromOptions({
    clusters: [{
      name: clusterName,
      server: cluster.server!,
      caData: normalizeCaData(cluster.caData),
      skipTLSVerify: cluster.skipTlsVerify
    }],
    users: [{
      name: userName,
      token: cluster.authType === 'token' ? credentials.token : undefined,
      username: cluster.authType === 'basic' ? cluster.username : undefined,
      password: cluster.authType === 'basic' ? credentials.password : undefined
    }],
    contexts: [{ name: contextName, cluster: clusterName, user: userName }],
    currentContext: contextName
  });
  return kc;
}

export function getClusterClients(clusterId: string) {
  const kubeConfig = getClusterKubeConfig(clusterId);
  return {
    kubeConfig,
    coreApi: kubeConfig.makeApiClient(k8s.CoreV1Api),
    appsApi: kubeConfig.makeApiClient(k8s.AppsV1Api)
  };
}
