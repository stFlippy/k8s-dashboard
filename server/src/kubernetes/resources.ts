import { PassThrough } from 'node:stream';
import * as k8s from '@kubernetes/client-node';
import { getClusterClients, getClusterKubeConfig } from './cluster-client.ts';
import type {
  DeploymentDetailDto,
  DeploymentResourcesInputDto,
  EventDto,
  ExecInputDto,
  ExecResultDto,
  PodConditionDto,
  PodContainerDto,
  PodDetailDto,
  PodMetricsDto
} from '../types/api.ts';

interface ExecSocket {
  on(event: 'error', listener: (error: Error) => void): void;
  on(event: 'close', listener: () => void): void;
  close(): void;
}

interface MetricContainerSource {
  name: string;
  usage: { cpu: string; memory: string };
}

interface MetricPodSource {
  metadata: { name?: string };
  timestamp?: string;
  window?: string;
  containers: MetricContainerSource[];
}

interface EventListSource {
  items: k8s.CoreV1Event[];
}

function iso(value: Date | string | undefined | null): string | null {
  if (!value) return null;
  return value instanceof Date ? value.toISOString() : new Date(value).toISOString();
}

function podLastRestartAt(statuses: k8s.V1ContainerStatus[] | undefined): string | null {
  const candidates = (statuses ?? [])
    .filter((status) => (status.restartCount ?? 0) > 0)
    .flatMap((status) => [
      iso(status.state?.running?.startedAt),
      iso(status.lastState?.terminated?.finishedAt),
      iso(status.lastState?.terminated?.startedAt)
    ])
    .filter((value): value is string => Boolean(value));
  if (!candidates.length) return null;
  return candidates.sort((left, right) => right.localeCompare(left))[0];
}

function quantity(value: unknown): string | null {
  if (value === undefined || value === null) return null;
  return String(value);
}

function podStatus(pod: k8s.V1Pod): PodDetailDto['status'] {
  const phase = pod.status?.phase;
  if (phase === 'Failed') return 'Failed';
  const containerStatuses = pod.status?.containerStatuses ?? [];
  const allContainersTerminated = containerStatuses.length > 0
    && containerStatuses.every((status: k8s.V1ContainerStatus) => Boolean(status.state?.terminated));
  if (phase === 'Succeeded' || allContainersTerminated) return 'Terminated';
  if (phase === 'Running') return 'Running';
  if (phase === 'Pending') return 'Pending';
  return 'Unknown';
}

function containerState(status: k8s.V1ContainerStatus | undefined): string {
  if (!status?.state) return 'Unknown';
  if (status.state.running) return 'Running';
  if (status.state.waiting) return status.state.waiting.reason || 'Waiting';
  if (status.state.terminated) return status.state.terminated.reason || `Terminated (${status.state.terminated.exitCode})`;
  return 'Unknown';
}

function sanitizeResource<T>(resource: T): T {
  const clone = JSON.parse(JSON.stringify(resource)) as T;
  const metadata = (clone as { metadata?: Record<string, unknown> }).metadata;
  if (metadata) {
    delete metadata.managedFields;
  }
  return clone;
}

export async function getDeploymentDetail(clusterId: string, namespace: string, name: string): Promise<DeploymentDetailDto> {
  const { appsApi } = getClusterClients(clusterId);
  const deployment = await appsApi.readNamespacedDeployment({ namespace, name });
  const containers: k8s.V1Container[] = deployment.spec?.template.spec?.containers ?? [];
  return {
    name: deployment.metadata?.name ?? name,
    namespace: deployment.metadata?.namespace ?? namespace,
    replicas: deployment.spec?.replicas ?? 0,
    availableReplicas: deployment.status?.availableReplicas ?? 0,
    image: containers.map((container: k8s.V1Container) => container.image).filter(Boolean).join(', '),
    creationTimestamp: iso(deployment.metadata?.creationTimestamp),
    containers: containers.map((container: k8s.V1Container) => ({
      name: container.name,
      image: container.image ?? '',
      cpuRequest: quantity(container.resources?.requests?.cpu),
      cpuLimit: quantity(container.resources?.limits?.cpu),
      memoryRequest: quantity(container.resources?.requests?.memory),
      memoryLimit: quantity(container.resources?.limits?.memory)
    })),
    resource: sanitizeResource(deployment)
  };
}

async function replaceDeployment(clusterId: string, namespace: string, name: string, mutate: (deployment: k8s.V1Deployment) => void): Promise<DeploymentDetailDto> {
  const { appsApi } = getClusterClients(clusterId);
  const deployment = await appsApi.readNamespacedDeployment({ namespace, name });
  mutate(deployment);
  await appsApi.replaceNamespacedDeployment({ namespace, name, body: deployment });
  return getDeploymentDetail(clusterId, namespace, name);
}

export async function setDeploymentReplicas(clusterId: string, namespace: string, name: string, replicas: number): Promise<DeploymentDetailDto> {
  if (!Number.isInteger(replicas) || replicas < 0 || replicas > 10_000) throw new Error('replicas must be an integer between 0 and 10000.');
  return replaceDeployment(clusterId, namespace, name, (deployment) => {
    if (!deployment.spec) throw new Error('Deployment spec is missing.');
    deployment.spec.replicas = replicas;
  });
}

export async function restartDeployment(clusterId: string, namespace: string, name: string): Promise<DeploymentDetailDto> {
  return replaceDeployment(clusterId, namespace, name, (deployment) => {
    if (!deployment.spec?.template.metadata) {
      if (!deployment.spec) throw new Error('Deployment spec is missing.');
      deployment.spec.template.metadata = {};
    }
    deployment.spec.template.metadata.annotations = {
      ...(deployment.spec.template.metadata.annotations ?? {}),
      'kubectl.kubernetes.io/restartedAt': new Date().toISOString()
    };
  });
}

export async function updateDeploymentResources(
  clusterId: string,
  namespace: string,
  name: string,
  input: DeploymentResourcesInputDto
): Promise<DeploymentDetailDto> {
  return replaceDeployment(clusterId, namespace, name, (deployment) => {
    const container = deployment.spec?.template.spec?.containers?.find((item: k8s.V1Container) => item.name === input.containerName);
    if (!container) throw new Error(`Container '${input.containerName}' not found.`);
    const requests: Record<string, string> = {};
    const limits: Record<string, string> = {};
    if (input.cpuRequest?.trim()) requests.cpu = input.cpuRequest.trim();
    if (input.memoryRequest?.trim()) requests.memory = input.memoryRequest.trim();
    if (input.cpuLimit?.trim()) limits.cpu = input.cpuLimit.trim();
    if (input.memoryLimit?.trim()) limits.memory = input.memoryLimit.trim();
    container.resources = {
      requests: Object.keys(requests).length ? requests : undefined,
      limits: Object.keys(limits).length ? limits : undefined
    };
  });
}

export async function replaceDeploymentResource(clusterId: string, namespace: string, name: string, resource: unknown): Promise<DeploymentDetailDto> {
  if (!resource || typeof resource !== 'object') throw new Error('Deployment resource must be a JSON object.');
  const deployment = resource as k8s.V1Deployment;
  if (deployment.metadata?.name && deployment.metadata.name !== name) throw new Error('metadata.name cannot be changed.');
  if (deployment.metadata?.namespace && deployment.metadata.namespace !== namespace) throw new Error('metadata.namespace cannot be changed.');
  deployment.metadata = { ...(deployment.metadata ?? {}), name, namespace };
  const { appsApi } = getClusterClients(clusterId);
  await appsApi.replaceNamespacedDeployment({ namespace, name, body: deployment });
  return getDeploymentDetail(clusterId, namespace, name);
}

export async function deleteDeployment(clusterId: string, namespace: string, name: string): Promise<void> {
  const { appsApi } = getClusterClients(clusterId);
  await appsApi.deleteNamespacedDeployment({ namespace, name, propagationPolicy: 'Foreground' });
}

export async function getPodDetail(clusterId: string, namespace: string, name: string): Promise<PodDetailDto> {
  const { coreApi } = getClusterClients(clusterId);
  const pod = await coreApi.readNamespacedPod({ namespace, name });
  const statuses = new Map<string, k8s.V1ContainerStatus>((pod.status?.containerStatuses ?? []).map((status: k8s.V1ContainerStatus) => [status.name, status]));
  const containers: PodContainerDto[] = (pod.spec?.containers ?? []).map((container: k8s.V1Container) => {
    const status = statuses.get(container.name);
    return {
      name: container.name,
      image: container.image ?? '',
      ready: status?.ready ?? false,
      restartCount: status?.restartCount ?? 0,
      state: containerState(status),
      ports: (container.ports ?? []).map((port: k8s.V1ContainerPort) => `${port.containerPort}/${port.protocol ?? 'TCP'}`),
      cpuRequest: quantity(container.resources?.requests?.cpu),
      cpuLimit: quantity(container.resources?.limits?.cpu),
      memoryRequest: quantity(container.resources?.requests?.memory),
      memoryLimit: quantity(container.resources?.limits?.memory)
    };
  });
  const conditions: PodConditionDto[] = (pod.status?.conditions ?? []).map((condition: k8s.V1PodCondition) => ({
    type: condition.type,
    status: condition.status,
    reason: condition.reason ?? null,
    message: condition.message ?? null,
    lastTransitionTime: iso(condition.lastTransitionTime)
  }));
  return {
    name: pod.metadata?.name ?? name,
    namespace: pod.metadata?.namespace ?? namespace,
    status: podStatus(pod),
    creationTimestamp: iso(pod.metadata?.creationTimestamp),
    restartCount: (pod.status?.containerStatuses ?? []).reduce((sum: number, status: k8s.V1ContainerStatus) => sum + (status.restartCount ?? 0), 0),
    ip: pod.status?.podIP ?? null,
    host: pod.spec?.nodeName ?? null,
    lastRestartAt: podLastRestartAt(pod.status?.containerStatuses),
    qosClass: pod.status?.qosClass ?? null,
    serviceAccount: pod.spec?.serviceAccountName ?? null,
    containers,
    conditions,
    resource: sanitizeResource(pod)
  };
}

export async function deletePod(clusterId: string, namespace: string, name: string): Promise<void> {
  const { coreApi } = getClusterClients(clusterId);
  await coreApi.deleteNamespacedPod({ namespace, name });
}

export async function replacePodResource(clusterId: string, namespace: string, name: string, resource: unknown): Promise<PodDetailDto> {
  if (!resource || typeof resource !== 'object') throw new Error('Pod resource must be a JSON object.');
  const pod = resource as k8s.V1Pod;
  if (pod.metadata?.name && pod.metadata.name !== name) throw new Error('metadata.name cannot be changed.');
  if (pod.metadata?.namespace && pod.metadata.namespace !== namespace) throw new Error('metadata.namespace cannot be changed.');
  pod.metadata = { ...(pod.metadata ?? {}), name, namespace };
  const { coreApi } = getClusterClients(clusterId);
  await coreApi.replaceNamespacedPod({ namespace, name, body: pod });
  return getPodDetail(clusterId, namespace, name);
}

export async function getPodLogs(
  clusterId: string,
  namespace: string,
  name: string,
  containerName: string | undefined,
  tailLines: number,
  previous: boolean
): Promise<string> {
  const detail = await getPodDetail(clusterId, namespace, name);
  const container = containerName || detail.containers[0]?.name;
  if (!container) throw new Error('Pod has no containers.');
  const config = getClusterKubeConfig(clusterId);
  const log = new k8s.Log(config);
  const output = new PassThrough();
  const chunks: Buffer[] = [];
  output.on('data', (chunk: Buffer | string) => chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk)));
  const finished = new Promise<void>((resolve, reject) => {
    output.once('finish', resolve);
    output.once('error', reject);
  });
  await log.log(namespace, name, container, output, {
    follow: false,
    tailLines: Math.max(1, Math.min(5000, tailLines)),
    timestamps: true,
    previous
  });
  await finished;
  return Buffer.concat(chunks).toString('utf8');
}

export async function execPod(clusterId: string, namespace: string, name: string, input: ExecInputDto): Promise<ExecResultDto> {
  const command = input.command.trim();
  if (!command) throw new Error('Command is required.');
  const detail = await getPodDetail(clusterId, namespace, name);
  const container = input.container || detail.containers[0]?.name;
  if (!container) throw new Error('Pod has no containers.');

  const stdoutChunks: Buffer[] = [];
  const stderrChunks: Buffer[] = [];
  const stdout = new PassThrough();
  const stderr = new PassThrough();
  stdout.on('data', (chunk: Buffer | string) => stdoutChunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk)));
  stderr.on('data', (chunk: Buffer | string) => stderrChunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk)));
  const stdoutFinished = new Promise<void>((resolve, reject) => {
    stdout.once('finish', resolve);
    stdout.once('error', reject);
  });
  const stderrFinished = new Promise<void>((resolve, reject) => {
    stderr.once('finish', resolve);
    stderr.once('error', reject);
  });
  const exec = new k8s.Exec(getClusterKubeConfig(clusterId));

  let status: unknown = null;
  await new Promise<void>((resolve, reject) => {
    let settled = false;
    let socket: ExecSocket | null = null;
    const timer = setTimeout(() => {
      if (!settled) {
        settled = true;
        socket?.close();
        stdout.end();
        stderr.end();
        reject(new Error('Exec timed out after 30 seconds.'));
      }
    }, 30_000);
    exec.exec(namespace, name, container, ['/bin/sh', '-lc', command], stdout, stderr, null, false, (result: k8s.V1Status) => {
      status = result;
      if (!settled) {
        settled = true;
        clearTimeout(timer);
        resolve();
      }
    }).then((connectedSocket: ExecSocket) => {
      socket = connectedSocket;
      connectedSocket.on('error', (error: Error) => {
        if (!settled) {
          settled = true;
          clearTimeout(timer);
          reject(error);
        }
      });
      connectedSocket.on('close', () => {
        if (!settled) {
          settled = true;
          clearTimeout(timer);
          stdout.end();
          stderr.end();
          resolve();
        }
      });
    }).catch((error: unknown) => {
      if (!settled) {
        settled = true;
        clearTimeout(timer);
        reject(error);
      }
    });
  });
  await Promise.all([stdoutFinished, stderrFinished]);

  return {
    stdout: Buffer.concat(stdoutChunks).toString('utf8'),
    stderr: Buffer.concat(stderrChunks).toString('utf8'),
    status
  };
}

export async function getPodMetrics(clusterId: string, namespace: string, name: string): Promise<PodMetricsDto> {
  try {
    const metrics = new k8s.Metrics(getClusterKubeConfig(clusterId));
    const list = await metrics.getPodMetrics(namespace);
    const pod = list.items.find((item: MetricPodSource) => item.metadata.name === name);
    if (!pod) return { available: false, timestamp: null, window: null, containers: [], error: 'Metrics for this pod were not returned.' };
    return {
      available: true,
      timestamp: pod.timestamp ?? null,
      window: pod.window ?? null,
      containers: pod.containers.map((container: MetricContainerSource) => ({ name: container.name, cpu: container.usage.cpu, memory: container.usage.memory }))
    };
  } catch (error) {
    return {
      available: false,
      timestamp: null,
      window: null,
      containers: [],
      error: error instanceof Error ? error.message : 'Metrics API is unavailable.'
    };
  }
}

function eventDto(event: k8s.CoreV1Event): EventDto {
  return {
    type: event.type ?? 'Unknown',
    reason: event.reason ?? '',
    message: event.message ?? '',
    source: event.source?.component ?? event.reportingComponent ?? '',
    objectKind: event.involvedObject.kind ?? '',
    objectName: event.involvedObject.name ?? '',
    time: iso(event.eventTime ?? event.lastTimestamp ?? event.firstTimestamp),
    count: event.count ?? 1
  };
}

export async function getPodEvents(clusterId: string, namespace: string, name: string): Promise<EventDto[]> {
  const { coreApi } = getClusterClients(clusterId);
  const events = await coreApi.listNamespacedEvent({
    namespace,
    fieldSelector: `involvedObject.kind=Pod,involvedObject.name=${name}`
  });
  return events.items.map(eventDto).sort((left: EventDto, right: EventDto) => (right.time ?? '').localeCompare(left.time ?? ''));
}

export async function getDeploymentEvents(clusterId: string, namespace: string, name: string): Promise<EventDto[]> {
  const { coreApi, appsApi } = getClusterClients(clusterId);
  const deployment = await appsApi.readNamespacedDeployment({ namespace, name });
  const replicaSets = await appsApi.listNamespacedReplicaSet({ namespace });
  const relatedReplicaSets = replicaSets.items.filter((replicaSet: k8s.V1ReplicaSet) =>
    (replicaSet.metadata?.ownerReferences ?? []).some((owner: k8s.V1OwnerReference) =>
      owner.kind === 'Deployment' && owner.name === name && (!deployment.metadata?.uid || !owner.uid || owner.uid === deployment.metadata.uid)
    )
  );

  const selectors = [
    `involvedObject.kind=Deployment,involvedObject.name=${name}`,
    ...relatedReplicaSets
      .map((replicaSet: k8s.V1ReplicaSet) => replicaSet.metadata?.name)
      .filter((replicaSetName: string | undefined): replicaSetName is string => Boolean(replicaSetName))
      .map((replicaSetName: string) => `involvedObject.kind=ReplicaSet,involvedObject.name=${replicaSetName}`)
  ];

  const eventLists = await Promise.all(selectors.map((fieldSelector) => coreApi.listNamespacedEvent({ namespace, fieldSelector })));
  return eventLists
    .flatMap((eventList: EventListSource) => eventList.items.map(eventDto))
    .sort((left: EventDto, right: EventDto) => (right.time ?? '').localeCompare(left.time ?? ''));
}
