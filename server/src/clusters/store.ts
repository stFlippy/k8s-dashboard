import { chmodSync, existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs';
import { dirname } from 'node:path';
import { randomUUID } from 'node:crypto';
import { environment } from '../config/environment.ts';
import { decryptSecret, encryptSecret, secretsAvailable } from '../security/crypto.ts';
import type { AuthenticatedClusterInput, ClusterDto } from '../types/api.ts';
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
    throw new Error('Legacy cluster store v1 requires explicit owner mapping. Run scripts/migrate-cluster-owners.mjs before starting the dashboard.');
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

function normalizeServer(value: string): string {
  let url: URL;
  try { url = new URL(value); } catch { throw new Error('API server URL is invalid.'); }
  if (url.protocol !== 'https:' || url.username || url.password || url.search || url.hash || url.pathname !== '/') {
    throw new Error('API server must be a plain HTTPS origin without credentials or path.');
  }
  return url.origin;
}

/** Accept only the token and inline CA returned by kubectl login; never pass plugins or paths to kubectl proxy. */
export function sanitizeLoginKubeconfig(raw: string, server: string): string {
  if (!raw || raw.length > 1_000_000) throw new Error('kubectl login returned an invalid kubeconfig.');
  const parsed = new k8s.KubeConfig();
  parsed.loadFromString(raw);
  const cluster = parsed.getCurrentCluster();
  const user = parsed.getCurrentUser();
  if (!cluster || !user || normalizeServer(cluster.server) !== normalizeServer(server)) {
    throw new Error('kubectl login kubeconfig API server does not match the selected cluster.');
  }
  if (!user.token || typeof user.token !== 'string' || user.token.length > 100_000) {
    throw new Error('kubectl login must return a bearer token.');
  }
  if (cluster.caFile || cluster.proxyUrl || user.exec || user.authProvider || user.certFile || user.keyFile || user.certData || user.keyData || user.username || user.password || user.impersonateUser) {
    throw new Error('kubectl login returned unsupported or unsafe kubeconfig fields.');
  }
  if (cluster.caData && (cluster.caData.length > 100_000 || !/^[A-Za-z0-9+/=]+$/.test(cluster.caData))) {
    throw new Error('kubectl login returned invalid inline CA data.');
  }
  const safe = new k8s.KubeConfig();
  safe.loadFromOptions({
    clusters: [{ name: 'quaber-cluster', server: normalizeServer(server), caData: cluster.caData, skipTLSVerify: cluster.skipTLSVerify === true }],
    users: [{ name: 'quaber-user', token: user.token }],
    contexts: [{ name: 'quaber-context', cluster: 'quaber-cluster', user: 'quaber-user' }],
    currentContext: 'quaber-context'
  });
  return safe.exportConfig();
}

export function getResolvedClusters(): ResolvedCluster[] {
  const ownerId = currentUser().userId;
  return readStore().clusters
    .filter((cluster) => cluster.ownerId === ownerId && cluster.authType === 'kubectl-login')
    .map((cluster) => ({ ...cluster, managedBy: 'user' as const }));
}

export function getResolvedCluster(id: string): ResolvedCluster {
  const cluster = getResolvedClusters().find((item) => item.id === id);
  if (!cluster) throw new Error(`Cluster '${id}' not found.`);
  if (!cluster.enabled) throw new Error(`Cluster '${cluster.name}' is disabled.`);
  return cluster;
}

function loginTokenExpired(cluster: ResolvedCluster): boolean {
  if (cluster.authType !== 'kubectl-login') return true;
  try {
    const kubeconfig = decryptSecret(cluster.encryptedKubeconfig);
    if (!kubeconfig) return true;
    const parsedConfig = new k8s.KubeConfig();
    parsedConfig.loadFromString(sanitizeLoginKubeconfig(kubeconfig, cluster.server!));
    const value = parsedConfig.getCurrentUser()?.token;
    const payload = value?.split('.')[1];
    if (!payload) return true;
    const parsed = JSON.parse(Buffer.from(payload, 'base64url').toString('utf8')) as { exp?: unknown };
    return typeof parsed.exp !== 'number' || parsed.exp * 1000 <= Date.now() + 30_000;
  } catch {
    return true;
  }
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
    loginRequired: loginTokenExpired(cluster),
    secretsLocked: !secretsAvailable()
  };
}

function normalizeInput(input: AuthenticatedClusterInput, existing?: StoredCluster): StoredCluster {
  if (!input || typeof input !== 'object' || typeof input.name !== 'string' || input.authType !== 'kubectl-login') throw new Error('Only kubectl login through the per-user proxy is supported.');
  const name = input.name.trim();
  if (!name) throw new Error('Cluster name is required.');
  if (name.length > 80) throw new Error('Cluster name must not exceed 80 characters.');
  if (/[\u0000-\u001f\u007f]/.test(name)) throw new Error('Cluster name contains control characters.');
  const enabled = input.enabled ?? true;
  const now = new Date().toISOString();
  const id = existing?.id ?? randomUUID();
  const ownerId = currentUser().userId;
  const authType = 'kubectl-login' as const;

  const server = input.server?.trim() || existing?.server;
  if (!server) throw new Error('API server URL is required for this authentication type.');
  const normalizedServer = normalizeServer(server);

  const username = input.username?.trim() || existing?.username;
  const kubeconfigData = input.kubeconfigData?.trim();
  if ((kubeconfigData?.length ?? 0) > 1_000_000 || (input.password?.length ?? 0) > 1024 || (username?.length ?? 0) > 256) throw new Error('Cluster credentials are too large.');
  if (!username) throw new Error('Username is required.');
  if (existing && !kubeconfigData && (normalizedServer !== existing.server || username !== existing.username)) {
    throw new Error('Changing API server or username requires a new kubectl login.');
  }
  const encryptedKubeconfig = kubeconfigData ? encryptSecret(sanitizeLoginKubeconfig(kubeconfigData, normalizedServer)) : existing?.encryptedKubeconfig;
  if (!encryptedKubeconfig) throw new Error('A successful kubectl login is required.');

  return {
    id,
    ownerId,
    name,
    authType,
    enabled,
    server: normalizedServer,
    username,
    encryptedKubeconfig,
    createdAt: existing?.createdAt ?? now,
    updatedAt: now
  };
}

export function createCluster(input: AuthenticatedClusterInput): ClusterDto {
  const store = readStore();
  const cluster = normalizeInput(input);
  if (store.clusters.some((item) => item.ownerId === cluster.ownerId && item.name.toLocaleLowerCase() === cluster.name.toLocaleLowerCase())) throw new Error(`Cluster '${cluster.name}' already exists.`);
  store.clusters.push(cluster);
  writeStore(store);
  return toClusterDto({ ...cluster, managedBy: 'user' });
}

export function resolveClusterInput(input: AuthenticatedClusterInput, existingId?: string): ResolvedCluster {
  const ownerId = currentUser().userId;
  const existing = existingId ? readStore().clusters.find((cluster) => cluster.id === existingId && cluster.ownerId === ownerId) : undefined;
  if (existingId && !existing) throw new Error(`Cluster '${existingId}' not found.`);
  return { ...normalizeInput(input, existing), managedBy: 'user' };
}

export function getOwnedClusterSettings(id: string): StoredCluster {
  const ownerId = currentUser().userId;
  const cluster = readStore().clusters.find((item) => item.id === id && item.ownerId === ownerId);
  if (!cluster) throw new Error(`Cluster '${id}' not found.`);
  return cluster;
}

export function updateCluster(id: string, input: AuthenticatedClusterInput): ClusterDto {
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

export function updateClusterLoginKubeconfigs(updates: Array<{ id: string; kubeconfig: string }>): ClusterDto[] {
  if (!updates.length) throw new Error('At least one cluster is required.');
  const store = readStore();
  const ownerId = currentUser().userId;
  const ids = new Set(updates.map((item) => item.id));
  if (ids.size !== updates.length) throw new Error('Cluster list contains duplicates.');

  const selected = updates.map(({ id }) => {
    const cluster = store.clusters.find((item) => item.id === id && item.ownerId === ownerId);
    if (!cluster) throw new Error(`Cluster '${id}' not found.`);
    if (cluster.authType !== 'kubectl-login' || !cluster.username) throw new Error(`Cluster '${cluster.name}' does not use kubectl login.`);
    return cluster;
  });
  const login = selected[0].username!.trim().toLocaleLowerCase();
  if (selected.some((cluster) => cluster.username!.trim().toLocaleLowerCase() !== login)) {
    throw new Error('All clusters in one login request must use the same username.');
  }

  const now = new Date().toISOString();
  for (const update of updates) {
    if (!update.kubeconfig || update.kubeconfig.length > 1_000_000) throw new Error('kubectl login returned an invalid kubeconfig.');
    const index = store.clusters.findIndex((item) => item.id === update.id && item.ownerId === ownerId);
    store.clusters[index] = { ...store.clusters[index], encryptedKubeconfig: encryptSecret(sanitizeLoginKubeconfig(update.kubeconfig, store.clusters[index].server!)), updatedAt: now };
  }
  writeStore(store);
  return updates.map(({ id }) => toClusterDto({ ...store.clusters.find((item) => item.id === id && item.ownerId === ownerId)!, managedBy: 'user' }));
}

export function deleteCluster(id: string): void {
  const store = readStore();
  const ownerId = currentUser().userId;
  const next = store.clusters.filter((cluster) => cluster.id !== id || cluster.ownerId !== ownerId);
  if (next.length === store.clusters.length) throw new Error(`Cluster '${id}' not found.`);
  writeStore({ ...store, clusters: next });
}

export function getDecryptedCredentials(cluster: ResolvedCluster): { kubeconfig?: string } {
  const raw = decryptSecret(cluster.encryptedKubeconfig);
  return { kubeconfig: raw ? sanitizeLoginKubeconfig(raw, cluster.server!) : undefined };
}
