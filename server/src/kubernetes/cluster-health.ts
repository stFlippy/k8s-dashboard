import * as k8s from '@kubernetes/client-node';
import { getClusterClients, getClusterKubeConfig } from './cluster-client.ts';
import type { ClusterHealthDto } from '../types/api.ts';

interface NodeMetric { metadata: { name?: string }; usage: { cpu?: string; memory?: string }; timestamp?: string; window?: string; }
const text = (value: unknown): string | null => value === undefined || value === null ? null : String(value);

export async function getClusterHealth(clusterId: string): Promise<ClusterHealthDto> {
  const { coreApi } = getClusterClients(clusterId);
  const nodes = await coreApi.listNode();
  let metrics = new Map<string, NodeMetric>(); let metricsError: string | null = null;
  try {
    const response = await new k8s.Metrics(getClusterKubeConfig(clusterId)).getNodeMetrics();
    metrics = new Map((response.items as NodeMetric[]).map((item) => [item.metadata.name ?? '', item]));
  } catch (error) { metricsError = error instanceof Error ? error.message : 'Node metrics are unavailable.'; }
  return {
    metricsAvailable: metrics.size > 0,
    metricsError,
    nodes: nodes.items.map((node) => {
      const name = node.metadata?.name ?? 'unknown'; const usage = metrics.get(name);
      return {
        name,
        ready: node.status?.conditions?.find((condition) => condition.type === 'Ready')?.status === 'True',
        roles: Object.keys(node.metadata?.labels ?? {}).filter((key) => key.startsWith('node-role.kubernetes.io/')).map((key) => key.split('/')[1] || 'worker'),
        addresses: (node.status?.addresses ?? []).map((address) => ({ type: address.type, address: address.address })),
        capacity: { cpu: text(node.status?.capacity?.cpu), memory: text(node.status?.capacity?.memory), pods: text(node.status?.capacity?.pods), storage: text(node.status?.capacity?.['ephemeral-storage']) },
        allocatable: { cpu: text(node.status?.allocatable?.cpu), memory: text(node.status?.allocatable?.memory), pods: text(node.status?.allocatable?.pods), storage: text(node.status?.allocatable?.['ephemeral-storage']) },
        usage: usage ? { cpu: usage.usage.cpu ?? null, memory: usage.usage.memory ?? null } : null,
        conditions: (node.status?.conditions ?? []).map((condition) => ({ type: condition.type, status: condition.status, reason: condition.reason ?? null, message: condition.message ?? null, lastTransitionTime: condition.lastTransitionTime ? new Date(condition.lastTransitionTime).toISOString() : null })),
        labels: node.metadata?.labels ?? {},
        taints: (node.spec?.taints ?? []).map((taint) => `${taint.key}${taint.value ? `=${taint.value}` : ''}:${taint.effect}`),
        kubeletVersion: node.status?.nodeInfo?.kubeletVersion ?? null,
        osImage: node.status?.nodeInfo?.osImage ?? null,
        kernelVersion: node.status?.nodeInfo?.kernelVersion ?? null,
        containerRuntime: node.status?.nodeInfo?.containerRuntimeVersion ?? null,
        architecture: node.status?.nodeInfo?.architecture ?? null
      };
    })
  };
}
