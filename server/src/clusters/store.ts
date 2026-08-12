import { chmodSync, existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs';
import { dirname } from 'node:path';
import { randomUUID } from 'node:crypto';
import * as k8s from '@kubernetes/client-node';
import { environment } from '../config/environment.ts';
import { decryptSecret, encryptSecret, secretsAvailable } from '../security/crypto.ts';
import type { ClusterDto, ClusterInputDto } from '../types/api.ts';
import type { ResolvedCluster, StoredCluster } from './types.ts';

interface StoreFile {
  version: 1;
  clusters: StoredCluster[];
}

function readStore(): StoreFile {
  if (!existsSync(environment.clusterStorePath)) return { version: 1, clusters: [] };
  const parsed = JSON.parse(readFileSync(environment.clusterStorePath, 'utf8')) as StoreFile;
  if (parsed.version !== 1 || !Array.isArray(parsed.clusters)) throw new Error('Invalid cluster store format.');
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

function environmentCluster(): ResolvedCluster {
  const kc = new k8s.KubeConfig();
  if (environment.kubeconfig) kc.loadFromFile(environment.kubeconfig);
  else kc.loadFromDefault();
  if (environment.kubeContext) kc.setCurrentContext(environment.kubeContext);
  const context = kc.getCurrentContext();
  const current = kc.getCurrentCluster();
  const now = new Date().toISOString();
  return {
    id: 'local',
    name: context || current?.name || 'Local kubeconfig',
    authType: 'kubeconfig',
    enabled: true,
    kubeconfigPath: environment.kubeconfig,
    context: context || undefined,
    server: current?.server,
    skipTlsVerify: current?.skipTLSVerify ?? false,
    createdAt: now,
    updatedAt: now,
    managedBy: 'environment'
  };
}

export function getResolvedClusters(): ResolvedCluster[] {
  return [environmentCluster(), ...readStore().clusters.map((cluster) => ({ ...cluster, managedBy: 'user' as const }))];
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
  const name = input.name.trim();
  if (!name) throw new Error('Cluster name is required.');
  const enabled = input.enabled ?? true;
  const now = new Date().toISOString();
  const id = existing?.id ?? randomUUID();
  const authType = input.authType;

  if (authType === 'kubeconfig') {
    return {
      id,
      name,
      authType,
      enabled,
      kubeconfigPath: input.kubeconfigPath?.trim() || undefined,
      context: input.context?.trim() || undefined,
      skipTlsVerify: false,
      createdAt: existing?.createdAt ?? now,
      updatedAt: now
    };
  }

  const server = input.server?.trim();
  if (!server) throw new Error('API server URL is required for token/basic authentication.');
  if (!/^https?:\/\//i.test(server)) throw new Error('API server must start with http:// or https://.');

  const token = input.token?.trim();
  const password = input.password ?? undefined;
  const encryptedToken = token ? encryptSecret(token) : existing?.encryptedToken;
  const encryptedPassword = password ? encryptSecret(password) : existing?.encryptedPassword;

  if (authType === 'token' && !encryptedToken) throw new Error('Bearer token is required.');
  if (authType === 'basic' && (!input.username?.trim() || !encryptedPassword)) {
    throw new Error('Username and password are required.');
  }

  return {
    id,
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
  store.clusters.push(cluster);
  writeStore(store);
  return toClusterDto({ ...cluster, managedBy: 'user' });
}

export function updateCluster(id: string, input: ClusterInputDto): ClusterDto {
  if (id === 'local') throw new Error('The environment cluster is read-only.');
  const store = readStore();
  const index = store.clusters.findIndex((cluster) => cluster.id === id);
  if (index < 0) throw new Error(`Cluster '${id}' not found.`);
  const cluster = normalizeInput(input, store.clusters[index]);
  store.clusters[index] = cluster;
  writeStore(store);
  return toClusterDto({ ...cluster, managedBy: 'user' });
}

export function deleteCluster(id: string): void {
  if (id === 'local') throw new Error('The environment cluster cannot be deleted.');
  const store = readStore();
  const next = store.clusters.filter((cluster) => cluster.id !== id);
  if (next.length === store.clusters.length) throw new Error(`Cluster '${id}' not found.`);
  writeStore({ ...store, clusters: next });
}

export function getDecryptedCredentials(cluster: ResolvedCluster): { token?: string; password?: string } {
  return {
    token: decryptSecret(cluster.encryptedToken),
    password: decryptSecret(cluster.encryptedPassword)
  };
}
