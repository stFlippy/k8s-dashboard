import type { ClusterAuthType } from '../types/api.ts';

export interface StoredCluster {
  id: string;
  name: string;
  authType: ClusterAuthType;
  enabled: boolean;
  kubeconfigPath?: string;
  context?: string;
  server?: string;
  username?: string;
  encryptedToken?: string;
  encryptedPassword?: string;
  caData?: string;
  skipTlsVerify: boolean;
  createdAt: string;
  updatedAt: string;
}

export interface ResolvedCluster extends StoredCluster {
  managedBy: 'environment' | 'user';
}
