import { existsSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { runtimeFile, standaloneRuntime } from './runtime.ts';

function loadEnvFile(): void {
  const candidates = [runtimeFile('.env'), resolve(process.cwd(), '../.env'), resolve(process.cwd(), '.env')];
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
  masterKey: process.env.DASHBOARD_MASTER_KEY || undefined,
  clusterStorePath: process.env.CLUSTER_STORE_PATH || (standaloneRuntime ? runtimeFile('data/clusters.json') : resolve(process.cwd(), 'data/clusters.json')),
  userDatabasePath: process.env.USER_DATABASE_PATH || (standaloneRuntime ? runtimeFile('data/users.sqlite') : resolve(process.cwd(), 'data/users.sqlite')),
  bootstrapUserPath: process.env.BOOTSTRAP_USER_PATH || (standaloneRuntime ? runtimeFile('data/bootstrap-user.json') : resolve(process.cwd(), 'data/bootstrap-user.json')),
  staticDirectory: process.env.STATIC_DIR || (standaloneRuntime ? runtimeFile('public') : undefined)
};
