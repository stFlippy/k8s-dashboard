import { chmodSync, existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs';
import { dirname } from 'node:path';
import { randomUUID } from 'node:crypto';
import { environment } from '../config/environment.ts';
import { decryptSecret, encryptSecret, secretsAvailable } from '../security/crypto.ts';
import type { ClusterDto, ClusterInputDto } from '../types/api.ts';
import type { ResolvedCluster, StoredCluster } from './types.ts';
import { currentUser } from '../auth/request-context.ts';
import * as k8s from '@kubernetes/client-node';

interface StoreFile {
  version: 2;
  clusters: StoredCluster[];
}

function readStore(): StoreFile {
  if (!existsSync(environment.clusterStorePath)) return { version: 2, clusters: [] };
  const parsed = JSON.parse(readFileSync(environment.clusterStorePath, 'utf8')) as StoreFile | { version: 1; clusters: Array<Omit<StoredCluster, 'ownerId'>> };
  if (!Array.isArray(parsed.clusters) || (parsed.version !== 1 && parsed.version !== 2)) throw new Error('Invalid cluster store format.');
  if (parsed.version === 1) {
    const ownerId = currentUser().userId;
    const migrated: StoreFile = { version: 2, clusters: parsed.clusters.map((cluster) => ({ ...cluster, ownerId })) };
    writeStore(migrated);
    return migrated;
  }
  return parsed;
}

function writeStore(store: StoreFile): void {
  mkdirSync(dirname(environment.clusterStorePath), { recursive: true });
  const temporaryPath = `${environment.clusterStorePath}.tmp`;
  writeFileSync(temporaryPath, `${JSON.stringify(store, null, 2)}\n`, { mode: 0o600 });
  chmodSync(temporaryPath, 0o600);
  renameSync(temporaryPath, environment.clusterStorePath);
  chmodSync(environment.clusterStorePath, 0o600);
}

export function getResolvedClusters(): ResolvedCluster[] {
  const ownerId = currentUser().userId;
  return readStore().clusters.filter((cluster) => cluster.ownerId === ownerId).map((cluster) => ({ ...cluster, managedBy: 'user' as const }));
}

export function getResolvedCluster(id: string): ResolvedCluster {
  const cluster = getResolvedClusters().find((item) => item.id === id);
  if (!cluster) throw new Error(`Cluster '${id}' not found.`);
  if (!cluster.enabled) throw new Error(`Cluster '${cluster.name}' is disabled.`);
  return cluster;
}

function maskSecret(encrypted: string | undefined): string | null {
  return encrypted ? '••••••••••••' : null;
}

export function toClusterDto(cluster: ResolvedCluster): ClusterDto {
  return {
    id: cluster.id,
    name: cluster.name,
    authType: cluster.authType,
    enabled: cluster.enabled,
    managedBy: cluster.managedBy,
    context: cluster.context ?? null,
    kubeconfigPath: cluster.kubeconfigPath ?? null,
    kubeconfigConfigured: Boolean(cluster.encryptedKubeconfig || cluster.kubeconfigPath),
    server: cluster.server ?? null,
    username: cluster.username ?? null,
    tokenMasked: maskSecret(cluster.encryptedToken),
    passwordMasked: maskSecret(cluster.encryptedPassword),
    caConfigured: Boolean(cluster.caData),
    skipTlsVerify: cluster.skipTlsVerify,
    secretsLocked: !secretsAvailable()
  };
}

function normalizeInput(input: ClusterInputDto, existing?: StoredCluster): StoredCluster {
  if (!input || typeof input !== 'object' || typeof input.name !== 'string' || !['kubeconfig', 'token', 'basic'].includes(input.authType)) throw new Error('Cluster settings are invalid.');
  const name = input.name.trim();
  if (!name) throw new Error('Cluster name is required.');
  if (name.length > 80) throw new Error('Cluster name must not exceed 80 characters.');
  if (/[\u0000-\u001f\u007f]/.test(name)) throw new Error('Cluster name contains control characters.');
  const enabled = input.enabled ?? true;
  const now = new Date().toISOString();
  const id = existing?.id ?? randomUUID();
  const ownerId = currentUser().userId;
  const authType = input.authType;

  if (authType === 'kubeconfig') {
    if ((input.kubeconfigData?.length ?? 0) > 1_000_000 || (input.context?.length ?? 0) > 253) throw new Error('Kubeconfig or context is too large.');
    const kubeconfigData = input.kubeconfigData?.trim();
    if (kubeconfigData) {
      try {
        const parsed = new k8s.KubeConfig();
        parsed.loadFromString(kubeconfigData);
        if (input.context?.trim()) parsed.setCurrentContext(input.context.trim());
        if (!parsed.getCurrentCluster()) throw new Error('Current cluster is missing.');
      } catch {
        throw new Error('Kubeconfig is not a valid Kubernetes configuration.');
      }
    }
    const encryptedKubeconfig = kubeconfigData ? encryptSecret(kubeconfigData) : existing?.encryptedKubeconfig;
    const legacyPath = existing?.kubeconfigPath;
    if (!encryptedKubeconfig && !legacyPath) throw new Error('Kubeconfig file content is required.');
    return {
      id,
      ownerId,
      name,
      authType,
      enabled,
      encryptedKubeconfig,
      kubeconfigPath: legacyPath,
      context: input.context?.trim() || undefined,
      skipTlsVerify: false,
      createdAt: existing?.createdAt ?? now,
      updatedAt: now
    };
  }

  const server = input.server?.trim();
  if (!server) throw new Error('API server URL is required for token/basic authentication.');
  let serverUrl: URL;
  try { serverUrl = new URL(server); } catch { throw new Error('API server URL is invalid.'); }
  if (!['http:', 'https:'].includes(serverUrl.protocol) || serverUrl.username || serverUrl.password) throw new Error('API server must be an http(s) URL without embedded credentials.');

  const token = input.token?.trim();
  const password = input.password ?? undefined;
  if ((token?.length ?? 0) > 65_536 || (password?.length ?? 0) > 1024 || (input.username?.length ?? 0) > 256 || (input.caData?.length ?? 0) > 1_000_000) throw new Error('Cluster credentials are too large.');
  const encryptedToken = token ? encryptSecret(token) : existing?.encryptedToken;
  const encryptedPassword = password ? encryptSecret(password) : existing?.encryptedPassword;

  if (authType === 'token' && !encryptedToken) throw new Error('Bearer token is required.');
  if (authType === 'basic' && (!input.username?.trim() || !encryptedPassword)) {
    throw new Error('Username and password are required.');
  }

  return {
    id,
    ownerId,
    name,
    authType,
    enabled,
    server: server.replace(/\/$/, ''),
    username: authType === 'basic' ? input.username?.trim() : undefined,
    encryptedToken: authType === 'token' ? encryptedToken : undefined,
    encryptedPassword: authType === 'basic' ? encryptedPassword : undefined,
    caData: input.caData?.trim() || existing?.caData,
    skipTlsVerify: Boolean(input.skipTlsVerify),
    createdAt: existing?.createdAt ?? now,
    updatedAt: now
  };
}

export function createCluster(input: ClusterInputDto): ClusterDto {
  const store = readStore();
  const cluster = normalizeInput(input);
  if (store.clusters.some((item) => item.ownerId === cluster.ownerId && item.name.toLocaleLowerCase() === cluster.name.toLocaleLowerCase())) throw new Error(`Cluster '${cluster.name}' already exists.`);
  store.clusters.push(cluster);
  writeStore(store);
  return toClusterDto({ ...cluster, managedBy: 'user' });
}

export function resolveClusterInput(input: ClusterInputDto, existingId?: string): ResolvedCluster {
  const ownerId = currentUser().userId;
  const existing = existingId ? readStore().clusters.find((cluster) => cluster.id === existingId && cluster.ownerId === ownerId) : undefined;
  if (existingId && !existing) throw new Error(`Cluster '${existingId}' not found.`);
  return { ...normalizeInput(input, existing), managedBy: 'user' };
}

export function updateCluster(id: string, input: ClusterInputDto): ClusterDto {
  const store = readStore();
  const ownerId = currentUser().userId;
  const index = store.clusters.findIndex((cluster) => cluster.id === id && cluster.ownerId === ownerId);
  if (index < 0) throw new Error(`Cluster '${id}' not found.`);
  const cluster = normalizeInput(input, store.clusters[index]);
  if (store.clusters.some((item, itemIndex) => itemIndex !== index && item.ownerId === ownerId && item.name.toLocaleLowerCase() === cluster.name.toLocaleLowerCase())) throw new Error(`Cluster '${cluster.name}' already exists.`);
  store.clusters[index] = cluster;
  writeStore(store);
  return toClusterDto({ ...cluster, managedBy: 'user' });
}

export function deleteCluster(id: string): void {
  const store = readStore();
  const ownerId = currentUser().userId;
  const next = store.clusters.filter((cluster) => cluster.id !== id || cluster.ownerId !== ownerId);
  if (next.length === store.clusters.length) throw new Error(`Cluster '${id}' not found.`);
  writeStore({ ...store, clusters: next });
}

export function getDecryptedCredentials(cluster: ResolvedCluster): { token?: string; password?: string; kubeconfig?: string } {
  return {
    token: decryptSecret(cluster.encryptedToken),
    password: decryptSecret(cluster.encryptedPassword),
    kubeconfig: decryptSecret(cluster.encryptedKubeconfig)
  };
}
