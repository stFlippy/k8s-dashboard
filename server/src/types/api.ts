export type ClusterAuthType = 'kubectl-login';

export interface ClusterDto {
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

export interface ClusterInputDto {
  name: string;
  authType: ClusterAuthType;
  enabled?: boolean;
  server?: string | null;
  username?: string | null;
  password?: string | null;
}

/** Internal result of the server-side kubectl login, never accepted from the API. */
export interface AuthenticatedClusterInput extends ClusterInputDto {
  kubeconfigData?: string;
}

export interface NamespaceDto {
  name: string;
  age: string;
}

export interface NodeResourceDto { cpu: string | null; memory: string | null; pods?: string | null; storage?: string | null; }
export interface ClusterNodeDto { name: string; ready: boolean; roles: string[]; addresses: Array<{ type: string; address: string }>; capacity: NodeResourceDto; allocatable: NodeResourceDto; usage: Pick<NodeResourceDto, 'cpu' | 'memory'> | null; conditions: Array<{ type: string; status: string; reason: string | null; message: string | null; lastTransitionTime: string | null }>; labels: Record<string, string>; taints: string[]; kubeletVersion: string | null; osImage: string | null; kernelVersion: string | null; containerRuntime: string | null; architecture: string | null; }
export interface ClusterHealthDto { metricsAvailable: boolean; metricsError: string | null; nodes: ClusterNodeDto[]; }

export interface DeploymentDto {
  name: string;
  namespace: string;
  uid: string;
  replicas: number;
  availableReplicas: number;
  image: string;
}

export interface DeploymentDetailDto extends DeploymentDto {
  creationTimestamp: string | null;
  containers: DeploymentContainerDto[];
  resource: unknown;
}

export interface DeploymentContainerDto {
  name: string;
  image: string;
  cpuRequest: string | null;
  cpuLimit: string | null;
  memoryRequest: string | null;
  memoryLimit: string | null;
}

export interface DeploymentResourcesInputDto {
  containerName: string;
  cpuRequest?: string | null;
  cpuLimit?: string | null;
  memoryRequest?: string | null;
  memoryLimit?: string | null;
  expectedResourceVersion?: string;
  acknowledgeRollout?: boolean;
}

export interface PodDto {
  name: string;
  namespace: string;
  uid: string;
  hasController: boolean;
  ownerDeploymentUid: string | null;
  status: 'Running' | 'Pending' | 'Failed' | 'Unknown' | 'Completed' | 'Terminating';
  creationTimestamp: string | null;
  restartCount: number;
  ip: string | null;
  host: string | null;
  lastRestartAt: string | null;
  terminating: boolean;
}

export interface PodConditionDto {
  type: string;
  status: string;
  reason: string | null;
  message: string | null;
  lastTransitionTime: string | null;
}

export interface PodContainerDto {
  name: string;
  image: string;
  ready: boolean;
  restartCount: number;
  state: string;
  ports: string[];
  cpuRequest: string | null;
  cpuLimit: string | null;
  memoryRequest: string | null;
  memoryLimit: string | null;
}

export interface PodDetailDto extends PodDto {
  qosClass: string | null;
  serviceAccount: string | null;
  containers: PodContainerDto[];
  conditions: PodConditionDto[];
  resource: unknown;
}

export interface PodMetricContainerDto {
  name: string;
  cpu: { usageCores: number | null; requestCores: number | null; limitCores: number | null };
  memory: { usageBytes: number | null; requestBytes: number | null; limitBytes: number | null };
  cpuThrottlingPercent: number | null;
}

export interface PodMetricsDto {
  available: boolean;
  timestamp: string | null;
  window: string | null;
  source: 'metrics-api' | 'kubelet' | null;
  throttlingAvailable: boolean;
  throttlingError?: string;
  containers: PodMetricContainerDto[];
  error?: string;
}

export interface EventDto {
  type: string;
  reason: string;
  message: string;
  source: string;
  objectKind: string;
  objectName: string;
  time: string | null;
  count: number;
}

export interface ExecInputDto {
  container: string;
  command: string;
}

export interface ExecResultDto {
  stdout: string;
  stderr: string;
  status: unknown;
}

export interface ResourceQuotaDto {
  name: string;
  namespace: string;
  hard: Record<string, string>;
  used: Record<string, string>;
}

export interface ConfigMapDto {
  name: string;
  namespace: string;
  creationTimestamp: string | null;
  data: Record<string, string>;
  binaryDataKeys: string[];
  resource: unknown;
}
