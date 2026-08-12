import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  createCluster,
  deleteCluster,
  getClusters,
  getConfigMaps,
  getDeployments,
  getNamespaces,
  getPods,
  getResourceQuotas,
  testCluster,
  updateCluster
} from '../api/kubernetes';
import type { ClusterInput } from '../types/kubernetes';

export function useClusters() {
  return useQuery({ queryKey: ['clusters'], queryFn: getClusters, refetchInterval: 15_000, refetchOnWindowFocus: true });
}

export function useNamespaces(clusterId: string) {
  return useQuery({
    queryKey: ['namespaces', clusterId],
    queryFn: () => getNamespaces(clusterId),
    enabled: Boolean(clusterId),
    refetchInterval: 10_000,
    refetchOnWindowFocus: true
  });
}

export function useDeployments(clusterId: string, namespace: string) {
  return useQuery({
    queryKey: ['deployments', clusterId, namespace],
    queryFn: () => getDeployments(clusterId, namespace),
    enabled: Boolean(clusterId && namespace),
    refetchInterval: 5_000,
    refetchOnWindowFocus: true
  });
}

export function usePods(clusterId: string, namespace: string) {
  return useQuery({
    queryKey: ['pods', clusterId, namespace],
    queryFn: () => getPods(clusterId, namespace),
    enabled: Boolean(clusterId && namespace),
    refetchInterval: 5_000,
    refetchOnWindowFocus: true
  });
}

export function useResourceQuotas(clusterId: string, namespace: string) {
  return useQuery({
    queryKey: ['resource-quotas', clusterId, namespace],
    queryFn: () => getResourceQuotas(clusterId, namespace),
    enabled: Boolean(clusterId && namespace),
    refetchInterval: 10_000,
    refetchOnWindowFocus: true
  });
}

export function useConfigMaps(clusterId: string, namespace: string) {
  return useQuery({
    queryKey: ['configmaps', clusterId, namespace],
    queryFn: () => getConfigMaps(clusterId, namespace),
    enabled: Boolean(clusterId && namespace),
    refetchInterval: 10_000,
    refetchOnWindowFocus: true
  });
}

export function useClusterMutations() {
  const client = useQueryClient();
  const invalidate = async () => {
    await client.invalidateQueries({ queryKey: ['clusters'] });
    await Promise.all([
      client.invalidateQueries({ queryKey: ['namespaces'] }),
      client.invalidateQueries({ queryKey: ['deployments'] }),
      client.invalidateQueries({ queryKey: ['pods'] }),
      client.invalidateQueries({ queryKey: ['resource-quotas'] }),
      client.invalidateQueries({ queryKey: ['configmaps'] })
    ]);
  };
  return {
    create: useMutation({ mutationFn: (input: ClusterInput) => createCluster(input), onSuccess: invalidate }),
    update: useMutation({ mutationFn: ({ id, input }: { id: string; input: ClusterInput }) => updateCluster(id, input), onSuccess: invalidate }),
    remove: useMutation({ mutationFn: (id: string) => deleteCluster(id), onSuccess: invalidate }),
    test: useMutation({ mutationFn: (id: string) => testCluster(id) })
  };
}
