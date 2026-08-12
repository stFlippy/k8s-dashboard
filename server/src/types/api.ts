export type ClusterAuthType = 'kubeconfig' | 'token' | 'basic';

export interface ClusterDto {
  id: string;
  name: string;
  authType: ClusterAuthType;
  enabled: boolean;
  managedBy: 'environment' | 'user';
  context: string | null;
  kubeconfigPath: string | null;
  server: string | null;
  username: string | null;
  tokenMasked: string | null;
  passwordMasked: string | null;
  caConfigured: boolean;
  skipTlsVerify: boolean;
  secretsLocked: boolean;
}

export interface ClusterInputDto {
  name: string;
  authType: ClusterAuthType;
  enabled?: boolean;
  context?: string | null;
  kubeconfigPath?: string | null;
  server?: string | null;
  username?: string | null;
  token?: string | null;
  password?: string | null;
  caData?: string | null;
  skipTlsVerify?: boolean;
}

export interface NamespaceDto {
  name: string;
  age: string;
}

export interface DeploymentDto {
  name: string;
  namespace: string;
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
}

export interface PodDto {
  name: string;
  namespace: string;
  status: 'Running' | 'Pending' | 'Failed' | 'Succeeded' | 'Unknown' | 'Terminated';
  creationTimestamp: string | null;
  restartCount: number;
  ip: string | null;
  host: string | null;
  lastRestartAt: string | null;
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
  cpu: string;
  memory: string;
}

export interface PodMetricsDto {
  available: boolean;
  timestamp: string | null;
  window: string | null;
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
}
