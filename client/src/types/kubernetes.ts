export type ClusterAuthType = 'kubectl-login';
export interface UserAccount { id: string; username: string; mustChangePassword: boolean; isAdmin: boolean; csrf?: string; }

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
  loginRequired: boolean;
  secretsLocked: boolean;
}

export interface ClusterInput {
  name: string;
  authType: ClusterAuthType;
  enabled: boolean;
  server?: string;
  username?: string;
  password?: string;
}

export interface ClusterTestResult { ok: boolean; namespaces: number; }
export interface Namespace { name: string; age: string; }
export interface Deployment { name: string; namespace: string; uid: string; replicas: number; availableReplicas: number; image: string; }
export interface DeploymentContainer { name: string; image: string; cpuRequest: string | null; cpuLimit: string | null; memoryRequest: string | null; memoryLimit: string | null; }
export interface DeploymentDetail extends Deployment { creationTimestamp: string | null; containers: DeploymentContainer[]; resource: unknown; }
export interface DeploymentResourcesInput { containerName: string; cpuRequest?: string | null; cpuLimit?: string | null; memoryRequest?: string | null; memoryLimit?: string | null; expectedResourceVersion?: string; acknowledgeRollout?: boolean; }

export type PodStatus = 'Running' | 'Pending' | 'Failed' | 'Unknown' | 'Completed' | 'Terminating';
export interface Pod { name: string; namespace: string; uid: string; hasController: boolean; ownerDeploymentUid: string | null; status: PodStatus; creationTimestamp: string | null; restartCount: number; ip: string | null; host: string | null; lastRestartAt: string | null; terminating: boolean; }
export interface PodCondition { type: string; status: string; reason: string | null; message: string | null; lastTransitionTime: string | null; }
export interface PodContainer { name: string; image: string; ready: boolean; restartCount: number; state: string; ports: string[]; cpuRequest: string | null; cpuLimit: string | null; memoryRequest: string | null; memoryLimit: string | null; }
export interface PodDetail extends Pod { qosClass: string | null; serviceAccount: string | null; containers: PodContainer[]; conditions: PodCondition[]; resource: unknown; }
export interface PodMetricContainer { name: string; cpu: { usageCores: number | null; requestCores: number | null; limitCores: number | null }; memory: { usageBytes: number | null; requestBytes: number | null; limitBytes: number | null }; cpuThrottlingPercent: number | null; }
export interface PodMetrics { available: boolean; timestamp: string | null; window: string | null; source: 'metrics-api' | 'kubelet' | null; throttlingAvailable: boolean; throttlingError?: string; containers: PodMetricContainer[]; error?: string; }
export interface KubernetesEvent { type: string; reason: string; message: string; source: string; objectKind: string; objectName: string; time: string | null; count: number; }
export type PodEvent = KubernetesEvent;
export type DeploymentEvent = KubernetesEvent;
export interface ExecResult { stdout: string; stderr: string; status: unknown; }

export interface ResourceQuota { name: string; namespace: string; hard: Record<string, string>; used: Record<string, string>; }
export interface ConfigMap { name: string; namespace: string; creationTimestamp: string | null; data: Record<string, string>; binaryDataKeys: string[]; resource: unknown; }

export interface WorkloadRef { clusterId: string; clusterName: string; namespace: string; deploymentName: string; }
export interface PodRef { clusterId: string; clusterName: string; namespace: string; podName: string; deploymentName?: string; initialTab?: 'logs' | 'exec' | 'describe' | 'yaml' | 'metrics' | 'events' | 'containers'; }
