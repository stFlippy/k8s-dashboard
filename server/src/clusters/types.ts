import type { ClusterAuthType } from '../types/api.ts';

export interface StoredCluster {
  id: string;
  ownerId: string;
  name: string;
  authType: ClusterAuthType;
  enabled: boolean;
  kubeconfigPath?: string;
  encryptedKubeconfig?: string;
  context?: string;
  server?: string;
  username?: string;
  createdAt: string;
  updatedAt: string;
}

export interface ResolvedCluster extends StoredCluster {
  managedBy: 'user';
}
