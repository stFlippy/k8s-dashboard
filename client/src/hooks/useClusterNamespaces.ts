import { useQueries } from '@tanstack/react-query';
import type { UseQueryResult } from '@tanstack/react-query';
import { getNamespaces } from '../api/kubernetes';
import { useClusters } from './useKubernetes';
import type { Cluster, Namespace } from '../types/kubernetes';

export interface ClusterNamespaceGroup {
  cluster: Cluster;
  namespaces: Namespace[];
  isLoading: boolean;
  error: Error | null;
}

export function useClusterNamespaces(): {
  clustersLoading: boolean;
  clustersError: Error | null;
  groups: ClusterNamespaceGroup[];
} {
  const clustersQuery = useClusters();
  const clusters = (clustersQuery.data ?? []).filter((cluster) => cluster.enabled);
  const namespaceQueries = useQueries({
    queries: clusters.map((cluster) => ({
      queryKey: ['namespaces', cluster.id],
      queryFn: () => getNamespaces(cluster.id),
      refetchInterval: 10_000,
      refetchOnWindowFocus: true
    }))
  }) as UseQueryResult<Namespace[], Error>[];

  return {
    clustersLoading: clustersQuery.isLoading,
    clustersError: clustersQuery.error ?? null,
    groups: clusters.map((cluster, index) => ({
      cluster,
      namespaces: namespaceQueries[index]?.data ?? [],
      isLoading: namespaceQueries[index]?.isLoading ?? true,
      error: namespaceQueries[index]?.error ?? null
    }))
  };
}
