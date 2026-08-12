import { existsSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';

function loadEnvFile(): void {
  const candidates = [resolve(process.cwd(), '../.env'), resolve(process.cwd(), '.env')];
  const envFile = candidates.find((candidate) => existsSync(candidate));
  if (!envFile) return;

  for (const rawLine of readFileSync(envFile, 'utf8').split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith('#')) continue;
    const separator = line.indexOf('=');
    if (separator <= 0) continue;
    const key = line.slice(0, separator).trim();
    let value = line.slice(separator + 1).trim();
    if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
      value = value.slice(1, -1);
    }
    if (!(key in process.env)) process.env[key] = value;
  }
}

loadEnvFile();

const port = Number(process.env.PORT ?? 3001);
if (!Number.isInteger(port) || port <= 0 || port > 65535) {
  throw new Error('PORT must be a valid TCP port.');
}

export const environment = {
  port,
  kubeconfig: process.env.KUBECONFIG || undefined,
  kubeContext: process.env.KUBE_CONTEXT || undefined,
  masterKey: process.env.DASHBOARD_MASTER_KEY || undefined,
  clusterStorePath: process.env.CLUSTER_STORE_PATH || resolve(process.cwd(), 'data/clusters.json')
};
