export type ClusterAuthType = 'kubeconfig' | 'token' | 'basic';
export interface UserAccount { id: string; username: string; mustChangePassword: boolean; csrf?: string; }

export interface Cluster {
  id: string;
  name: string;
  authType: ClusterAuthType;
  enabled: boolean;
  managedBy: 'environment' | 'user';
  context: string | null;
  kubeconfigPath: string | null;
  kubeconfigConfigured: boolean;
  server: string | null;
  username: string | null;
  tokenMasked: string | null;
  passwordMasked: string | null;
  caConfigured: boolean;
  skipTlsVerify: boolean;
  secretsLocked: boolean;
}

export interface ClusterInput {
  name: string;
  authType: ClusterAuthType;
  enabled: boolean;
  context?: string;
  kubeconfigPath?: string;
  kubeconfigData?: string;
  server?: string;
  username?: string;
  token?: string;
  password?: string;
  caData?: string;
  skipTlsVerify?: boolean;
}

export interface ClusterTestResult { ok: boolean; namespaces: number; }
export interface NodeResource { cpu: string | null; memory: string | null; pods?: string | null; storage?: string | null; }
export interface ClusterNode { name: string; ready: boolean; roles: string[]; addresses: Array<{ type: string; address: string }>; capacity: NodeResource; allocatable: NodeResource; usage: Pick<NodeResource, 'cpu' | 'memory'> | null; conditions: Array<{ type: string; status: string; reason: string | null; message: string | null; lastTransitionTime: string | null }>; labels: Record<string, string>; taints: string[]; kubeletVersion: string | null; osImage: string | null; kernelVersion: string | null; containerRuntime: string | null; architecture: string | null; }
export interface ClusterHealth { metricsAvailable: boolean; metricsError: string | null; nodes: ClusterNode[]; }
export interface Namespace { name: string; age: string; }
export interface Deployment { name: string; namespace: string; replicas: number; availableReplicas: number; image: string; }
export interface DeploymentContainer { name: string; image: string; cpuRequest: string | null; cpuLimit: string | null; memoryRequest: string | null; memoryLimit: string | null; }
export interface DeploymentDetail extends Deployment { creationTimestamp: string | null; containers: DeploymentContainer[]; resource: unknown; }
export interface DeploymentResourcesInput { containerName: string; cpuRequest?: string | null; cpuLimit?: string | null; memoryRequest?: string | null; memoryLimit?: string | null; }

export type PodStatus = 'Running' | 'Pending' | 'Failed' | 'Unknown';
export interface Pod { name: string; namespace: string; status: PodStatus; creationTimestamp: string | null; restartCount: number; ip: string | null; host: string | null; lastRestartAt: string | null; terminating: boolean; }
export interface PodCondition { type: string; status: string; reason: string | null; message: string | null; lastTransitionTime: string | null; }
export interface PodContainer { name: string; image: string; ready: boolean; restartCount: number; state: string; ports: string[]; cpuRequest: string | null; cpuLimit: string | null; memoryRequest: string | null; memoryLimit: string | null; }
export interface PodDetail extends Pod { qosClass: string | null; serviceAccount: string | null; containers: PodContainer[]; conditions: PodCondition[]; resource: unknown; }
export interface PodMetrics { available: boolean; timestamp: string | null; window: string | null; containers: Array<{ name: string; cpu: string; memory: string }>; error?: string; }
export interface KubernetesEvent { type: string; reason: string; message: string; source: string; objectKind: string; objectName: string; time: string | null; count: number; }
export type PodEvent = KubernetesEvent;
export type DeploymentEvent = KubernetesEvent;
export interface ExecResult { stdout: string; stderr: string; status: unknown; }

export interface ResourceQuota { name: string; namespace: string; hard: Record<string, string>; used: Record<string, string>; }
export interface ConfigMap { name: string; namespace: string; creationTimestamp: string | null; data: Record<string, string>; binaryDataKeys: string[]; resource: unknown; }

export interface WorkloadRef { clusterId: string; clusterName: string; namespace: string; deploymentName: string; }
export interface PodRef { clusterId: string; clusterName: string; namespace: string; podName: string; deploymentName?: string; initialTab?: 'logs' | 'exec' | 'describe' | 'yaml' | 'metrics' | 'events' | 'containers'; }
