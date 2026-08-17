import { apiDelete, apiGet, apiSend } from './client';
import type {
  Cluster,
  ClusterInput,
  ClusterTestResult,
  ClusterHealth,
  ConfigMap,
  Deployment,
  DeploymentDetail,
  DeploymentEvent,
  DeploymentResourcesInput,
  ExecResult,
  Namespace,
  Pod,
  PodDetail,
  PodEvent,
  PodMetrics,
  ResourceQuota
} from '../types/kubernetes';

const q = (value: string) => encodeURIComponent(value);
const scope = (clusterId: string, namespace?: string) => `clusterId=${q(clusterId)}${namespace ? `&namespace=${q(namespace)}` : ''}`;

export const getClusters = () => apiGet<Cluster[]>('/api/clusters');
export const createCluster = (input: ClusterInput) => apiSend<Cluster>('/api/clusters', 'POST', input);
export const updateCluster = (id: string, input: ClusterInput) => apiSend<Cluster>(`/api/clusters/${q(id)}`, 'PUT', input);
export const deleteCluster = (id: string) => apiDelete(`/api/clusters/${q(id)}`);
export const testCluster = (id: string) => apiSend<ClusterTestResult>(`/api/clusters/${q(id)}/test`, 'POST', {});
export const testClusterInput = (input: ClusterInput, existingId?: string) => apiSend<ClusterTestResult>('/api/clusters/test-input', 'POST', { input, existingId });

export const getNamespaces = (clusterId: string) => apiGet<Namespace[]>(`/api/namespaces?${scope(clusterId)}`);
export const getClusterHealth = (clusterId: string) => apiGet<ClusterHealth>(`/api/cluster-health?${scope(clusterId)}`);
export const getDeployments = (clusterId: string, namespace: string) => apiGet<Deployment[]>(`/api/deployments?${scope(clusterId, namespace)}`);
export const getPods = (clusterId: string, namespace: string) => apiGet<Pod[]>(`/api/pods?${scope(clusterId, namespace)}`);
export const getResourceQuotas = (clusterId: string, namespace: string) => apiGet<ResourceQuota[]>(`/api/resource-quotas?${scope(clusterId, namespace)}`);
export const getQuotas = getResourceQuotas;
export const getConfigMaps = (clusterId: string, namespace: string) => apiGet<ConfigMap[]>(`/api/configmaps?${scope(clusterId, namespace)}`);
export const replaceConfigMap = (clusterId: string, namespace: string, name: string, resource: unknown) => apiSend<ConfigMap>(`/api/configmaps/${q(name)}?${scope(clusterId, namespace)}`, 'PUT', { resource });

export const getDeployment = (clusterId: string, namespace: string, name: string) => apiGet<DeploymentDetail>(`/api/deployments/${q(name)}?${scope(clusterId, namespace)}`);
export const getDeploymentEvents = (clusterId: string, namespace: string, name: string) => apiGet<DeploymentEvent[]>(`/api/deployments/${q(name)}/events?${scope(clusterId, namespace)}`);
export const setDeploymentReplicas = (clusterId: string, namespace: string, name: string, replicas: number) => apiSend<DeploymentDetail>(`/api/deployments/${q(name)}/replicas?${scope(clusterId, namespace)}`, 'PUT', { replicas });
export const restartDeployment = (clusterId: string, namespace: string, name: string) => apiSend<DeploymentDetail>(`/api/deployments/${q(name)}/restart?${scope(clusterId, namespace)}`, 'POST', {});
export const updateDeploymentResources = (clusterId: string, namespace: string, name: string, input: DeploymentResourcesInput) => apiSend<DeploymentDetail>(`/api/deployments/${q(name)}/resources?${scope(clusterId, namespace)}`, 'PUT', input);
export const replaceDeploymentResource = (clusterId: string, namespace: string, name: string, resource: unknown) => apiSend<DeploymentDetail>(`/api/deployments/${q(name)}?${scope(clusterId, namespace)}`, 'PUT', { resource });
export const removeDeployment = (clusterId: string, namespace: string, name: string) => apiDelete(`/api/deployments/${q(name)}?${scope(clusterId, namespace)}`);

export const getPod = (clusterId: string, namespace: string, name: string) => apiGet<PodDetail>(`/api/pods/${q(name)}?${scope(clusterId, namespace)}`);
export const getPodLogs = async (clusterId: string, namespace: string, name: string, container: string, tailLines: number, previous = false): Promise<string> => {
  const response = await fetch(`/api/pods/${q(name)}/logs?${scope(clusterId, namespace)}&container=${q(container)}&tailLines=${tailLines}&previous=${previous ? '1' : '0'}`);
  const text = await response.text();
  if (!response.ok) {
    try { throw new Error((JSON.parse(text) as { error?: string }).error || `Request failed: ${response.status}`); }
    catch (error) { if (error instanceof SyntaxError) throw new Error(text || `Request failed: ${response.status}`); throw error; }
  }
  return text;
};
export const execPod = (clusterId: string, namespace: string, name: string, container: string, command: string) => apiSend<ExecResult>(`/api/pods/${q(name)}/exec?${scope(clusterId, namespace)}`, 'POST', { container, command });
export const getPodMetrics = (clusterId: string, namespace: string, name: string) => apiGet<PodMetrics>(`/api/pods/${q(name)}/metrics?${scope(clusterId, namespace)}`);
export const getPodEvents = (clusterId: string, namespace: string, name: string) => apiGet<PodEvent[]>(`/api/pods/${q(name)}/events?${scope(clusterId, namespace)}`);
export const replacePodResource = (clusterId: string, namespace: string, name: string, resource: unknown) => apiSend<PodDetail>(`/api/pods/${q(name)}?${scope(clusterId, namespace)}`, 'PUT', { resource });
export const removePod = (clusterId: string, namespace: string, name: string) => apiDelete(`/api/pods/${q(name)}?${scope(clusterId, namespace)}`);
