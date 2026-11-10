import { spawn } from 'node:child_process';
import { accessSync, constants, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import type { AuthenticatedClusterInput, ClusterInputDto } from '../types/api.ts';
import { getOwnedClusterSettings } from '../clusters/store.ts';
import { runtimeFile } from '../config/runtime.ts';
import { logExternalCommandFailure } from '../logging/logger.ts';

const executable = runtimeFile('addons/kubectl');
const outputLimit = 1_000_000;

export async function runLogin(server: string, username: string, password: string, kubeconfigPath: string, environment: NodeJS.ProcessEnv, clientExecutable = executable): Promise<string> {
  if (!existsSync(clientExecutable)) throw new Error('addons/kubectl was not found on the dashboard server.');
  try { accessSync(clientExecutable, constants.X_OK); } catch { throw new Error('addons/kubectl is not executable.'); }

  return new Promise((resolve, reject) => {
    const args = ['login', server, '--username', username, '--password', password];
    const child = spawn(clientExecutable, args, { env: environment, stdio: ['pipe', 'pipe', 'pipe'], shell: false });
    const stdout: Buffer[] = [];
    const stderr: Buffer[] = [];
    let size = 0;
    let settled = false;
    let timer: NodeJS.Timeout | undefined;
    const finish = (error?: Error, kubeconfig?: string) => {
      if (settled) return;
      settled = true;
      if (timer) clearTimeout(timer);
      if (error) reject(error); else resolve(kubeconfig!);
    };
    const collect = (target: Buffer[]) => (chunk: Buffer) => {
      size += chunk.length;
      if (size > outputLimit) {
        child.kill('SIGKILL');
        finish(new Error('kubectl login returned too much data.'));
        return;
      }
      target.push(chunk);
    };
    child.stdout.on('data', collect(stdout));
    child.stderr.on('data', collect(stderr));
    child.on('error', () => finish(new Error('Unable to start addons/kubectl. Check executable permissions.')));
    child.on('close', (code) => {
      if (settled) return;
      const errorText = Buffer.concat(stderr).toString('utf8');
      const outputText = Buffer.concat(stdout).toString('utf8');
      if (code !== 0) {
        // Preserve kubectl's actual diagnostics. Only mask the supplied password
        // in case a client echoes its own arguments or request data on failure.
        const mask = (value: string) => password ? value.replaceAll(password, '[REDACTED]') : value;
        const safeStderr = mask(errorText);
        const safeStdout = mask(outputText);
        logExternalCommandFailure('kubectl login', code, safeStderr, safeStdout);
        finish(new Error([safeStderr, safeStdout].filter(Boolean).join('\n').trim() || `kubectl login exited with code ${code ?? 'unknown'} without output.`));
        return;
      }
      try {
        const value = readFileSync(kubeconfigPath, 'utf8');
        if (!value || value.length > 1_000_000) throw new Error('Generated kubeconfig is empty or too large.');
        finish(undefined, value);
      } catch (error) {
        finish(error instanceof Error ? error : new Error('kubectl login did not create a kubeconfig.'));
      }
    });
    timer = setTimeout(() => {
      child.kill('SIGKILL');
      finish(new Error('kubectl login timed out after 45 seconds.'));
    }, 45_000);
    timer.unref();
    child.stdin.end();
  });
}

export async function authenticateKubectlLoginInput(input: ClusterInputDto, existingId?: string): Promise<AuthenticatedClusterInput> {
  if (input.authType !== 'kubectl-login') throw new Error('Only kubectl login through the per-user proxy is supported.');
  const submitted = input as ClusterInputDto & Record<string, unknown>;
  if (submitted.kubeconfigData !== undefined || submitted.token !== undefined || submitted.kubeconfigPath !== undefined || submitted.context !== undefined) {
    throw new Error('Credentials must be obtained through kubectl login.');
  }
  const existing = existingId ? getOwnedClusterSettings(existingId) : undefined;
  const server = input.server?.trim() || existing?.server;
  const username = input.username?.trim() || existing?.username;
  if (!server || !username) throw new Error('API server and username are required for kubectl login.');
  if (!input.password) {
    if (!existing) throw new Error('Password is required for a new cluster.');
    if (server.replace(/\/$/, '') !== existing.server || username !== existing.username) {
      throw new Error('Changing API server or username requires a new kubectl login and password.');
    }
    return input;
  }

  const temporaryDirectory = mkdtempSync(join(tmpdir(), 'quaber-login-'));
  const kubeconfigPath = join(temporaryDirectory, '.kube', 'config');
  const helperEnvironment: NodeJS.ProcessEnv = {
    ...Object.fromEntries(Object.entries(process.env).filter(([key]) => !key.startsWith('QUABER_'))),
    HOME: temporaryDirectory,
    KUBECONFIG: kubeconfigPath
  };

  try {
    mkdirSync(join(temporaryDirectory, '.kube'), { mode: 0o700 });
    const kubeconfigData = await runLogin(server, username, input.password, kubeconfigPath, helperEnvironment);
    return { ...input, username, kubeconfigData, password: undefined };
  } finally {
    rmSync(temporaryDirectory, { recursive: true, force: true });
  }
}

export async function authenticateKubectlLoginClusters(clusterIds: string[], password: string): Promise<Array<{ id: string; kubeconfig: string }>> {
  if (!Array.isArray(clusterIds) || clusterIds.length < 1 || clusterIds.length > 100) throw new Error('Choose between 1 and 100 clusters.');
  if (typeof password !== 'string' || password.length < 1 || password.length > 1024) throw new Error('Password is required.');
  if (new Set(clusterIds).size !== clusterIds.length || clusterIds.some((id) => !/^[0-9a-f-]{36}$/i.test(id))) throw new Error('Cluster list is invalid.');

  const clusters = clusterIds.map(getOwnedClusterSettings);
  if (clusters.some((cluster) => cluster.authType !== 'kubectl-login' || !cluster.username)) throw new Error('All selected clusters must use kubectl login.');
  const login = clusters[0].username!.trim().toLocaleLowerCase();
  if (clusters.some((cluster) => cluster.username!.trim().toLocaleLowerCase() !== login)) throw new Error('All selected clusters must use the same username.');

  return Promise.all(clusters.map(async (cluster) => {
    const authenticated = await authenticateKubectlLoginInput({
      name: cluster.name,
      authType: 'kubectl-login',
      enabled: cluster.enabled,
      server: cluster.server,
      username: cluster.username,
      password
    }, cluster.id);
    if (!authenticated.kubeconfigData) throw new Error(`Cluster '${cluster.name}' did not return a kubeconfig.`);
    return { id: cluster.id, kubeconfig: authenticated.kubeconfigData };
  }));
}
