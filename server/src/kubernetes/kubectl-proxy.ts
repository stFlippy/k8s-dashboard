import { createHash, randomBytes } from 'node:crypto';
import { spawn, type ChildProcess } from 'node:child_process';
import { accessSync, constants, existsSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import * as k8s from '@kubernetes/client-node';
import type { ResolvedCluster } from '../clusters/types.ts';
import { getDecryptedCredentials } from '../clusters/store.ts';
import { currentUser } from '../auth/request-context.ts';
import { logError, logInfo } from '../logging/logger.ts';
import { runtimeFile } from '../config/runtime.ts';

const executable = runtimeFile('addons/kubectl');
const idleTimeoutMs = 15 * 60_000;
const startupTimeoutMs = 15_000;

interface ProxyEntry {
  key: string;
  clusterId: string;
  clusterName: string;
  username: string;
  fingerprint: string;
  child: ChildProcess;
  directory: string;
  server: string;
  idleTimer?: NodeJS.Timeout;
  stopped: boolean;
}

const proxies = new Map<string, ProxyEntry>();
const pending = new Map<string, Promise<ProxyEntry>>();

function sourceKubeconfig(cluster: ResolvedCluster): string {
  const credentials = getDecryptedCredentials(cluster);
  if (credentials.kubeconfig) return credentials.kubeconfig;
  throw new Error(`Cluster '${cluster.name}' requires login.`);
}

function localKubeconfig(entry: ProxyEntry): k8s.KubeConfig {
  const config = new k8s.KubeConfig();
  config.loadFromOptions({
    // The proxy is intentionally plain HTTP on loopback. client-node rejects
    // every http:// server unless skipTLSVerify is explicit, even though no
    // remote TLS verification is involved on this local hop.
    clusters: [{ name: `proxy-${entry.clusterId}`, server: entry.server, skipTLSVerify: true }],
    users: [{ name: `proxy-user-${entry.clusterId}` }],
    contexts: [{ name: `proxy-context-${entry.clusterId}`, cluster: `proxy-${entry.clusterId}`, user: `proxy-user-${entry.clusterId}` }],
    currentContext: `proxy-context-${entry.clusterId}`
  });
  return config;
}

function stopEntry(entry: ProxyEntry, reason: string): void {
  if (entry.stopped) return;
  entry.stopped = true;
  if (entry.idleTimer) clearTimeout(entry.idleTimer);
  if (proxies.get(entry.key) === entry) proxies.delete(entry.key);
  if (entry.child.exitCode === null && !entry.child.killed) entry.child.kill('SIGTERM');
  rmSync(entry.directory, { recursive: true, force: true });
  logInfo('kubectl proxy stop', { user: entry.username, cluster: entry.clusterName, reason });
}

function touch(entry: ProxyEntry): void {
  if (entry.idleTimer) clearTimeout(entry.idleTimer);
  entry.idleTimer = setTimeout(() => stopEntry(entry, 'idle timeout'), idleTimeoutMs);
  entry.idleTimer.unref();
}

async function startProxy(cluster: ResolvedCluster, key: string, kubeconfig: string, fingerprint: string, username: string): Promise<ProxyEntry> {
  if (!existsSync(executable)) throw new Error('addons/kubectl was not found on the dashboard server.');
  try { accessSync(executable, constants.X_OK); } catch { throw new Error('addons/kubectl is not executable.'); }

  const directory = mkdtempSync(join(tmpdir(), 'quaber-proxy-'));
  const kubeconfigPath = join(directory, 'config');
  writeFileSync(kubeconfigPath, kubeconfig, { mode: 0o600 });
  const prefix = `/quaber-${randomBytes(18).toString('hex')}`;
  const args = [
    'proxy', '--kubeconfig', kubeconfigPath, '--address=127.0.0.1', '--port=0',
    `--api-prefix=${prefix}`, '--reject-paths=^$', '--accept-hosts=^127\\.0\\.0\\.1$'
  ];
  const child = spawn(executable, args, { shell: false, stdio: ['ignore', 'pipe', 'pipe'] });

  return new Promise((resolve, reject) => {
    let stdout = '';
    let stderr = '';
    let settled = false;
    const fail = (error: Error) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (child.exitCode === null && !child.killed) child.kill('SIGTERM');
      rmSync(directory, { recursive: true, force: true });
      logError('kubectl proxy start', error, { user: username, cluster: cluster.name });
      reject(error);
    };
    const ready = (port: string) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      const entry: ProxyEntry = {
        key, clusterId: cluster.id, clusterName: cluster.name, username, fingerprint,
        child, directory, server: `http://127.0.0.1:${port}${prefix}`, stopped: false
      };
      proxies.set(key, entry);
      touch(entry);
      child.once('exit', (code, signal) => {
        if (!entry.stopped) {
          entry.stopped = true;
          if (entry.idleTimer) clearTimeout(entry.idleTimer);
          if (proxies.get(key) === entry) proxies.delete(key);
          rmSync(directory, { recursive: true, force: true });
          if (code !== 0 && signal !== 'SIGTERM') logError('kubectl proxy exit', `exit=${code ?? signal ?? 'unknown'}`, { user: username, cluster: cluster.name });
        }
      });
      logInfo('kubectl proxy start', { user: username, cluster: cluster.name });
      resolve(entry);
    };
    const inspect = () => {
      const match = stdout.match(/Starting to serve on 127\.0\.0\.1:(\d+)/);
      if (match) ready(match[1]);
    };
    child.stdout.on('data', (chunk: Buffer) => { stdout = `${stdout}${chunk.toString('utf8')}`.slice(-4_000); inspect(); });
    child.stderr.on('data', (chunk: Buffer) => { stderr = `${stderr}${chunk.toString('utf8')}`.slice(-4_000); });
    child.once('error', () => fail(new Error('Unable to start addons/kubectl proxy.')));
    child.once('exit', (code) => {
      if (!settled) fail(new Error(stderr.trim().split(/\r?\n/).at(-1)?.slice(0, 500) || `kubectl proxy exited with code ${code ?? 'unknown'}.`));
    });
    const timer = setTimeout(() => fail(new Error('kubectl proxy did not start within 15 seconds.')), startupTimeoutMs);
    timer.unref();
  });
}

export async function getProxyKubeConfig(cluster: ResolvedCluster): Promise<k8s.KubeConfig> {
  if (cluster.authType !== 'kubectl-login') throw new Error(`Cluster '${cluster.name}' uses an unsupported authentication method.`);
  const identity = currentUser();
  if (cluster.ownerId !== identity.userId) throw new Error('Cluster access is forbidden.');
  const kubeconfig = sourceKubeconfig(cluster);
  const fingerprint = createHash('sha256').update(kubeconfig).digest('hex');
  const key = `${identity.userId}:${cluster.id}`;
  const existing = proxies.get(key);
  if (existing && existing.fingerprint === fingerprint && existing.child.exitCode === null) {
    touch(existing);
    return localKubeconfig(existing);
  }
  if (existing) stopEntry(existing, 'credentials changed');

  let startup = pending.get(key);
  if (!startup) {
    startup = startProxy(cluster, key, kubeconfig, fingerprint, identity.username).finally(() => pending.delete(key));
    pending.set(key, startup);
  }
  const entry = await startup;
  touch(entry);
  return localKubeconfig(entry);
}

export function stopClusterProxy(clusterId: string, reason = 'requested'): void {
  const identity = currentUser();
  const entry = proxies.get(`${identity.userId}:${clusterId}`);
  if (entry) stopEntry(entry, reason);
}

export function stopUserClusterProxies(userId: string): void {
  for (const entry of [...proxies.values()]) {
    if (entry.key.startsWith(`${userId}:`)) stopEntry(entry, 'user logout');
  }
}

export function stopAllClusterProxies(): void {
  for (const entry of [...proxies.values()]) stopEntry(entry, 'server shutdown');
}
