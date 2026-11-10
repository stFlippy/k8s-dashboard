import { DatabaseSync } from 'node:sqlite';
import { randomUUID } from 'node:crypto';
import { existsSync, mkdirSync, statSync } from 'node:fs';
import { cpus, freemem, totalmem } from 'node:os';
import { statfsSync } from 'node:fs';
import { dirname } from 'node:path';
import type { ServerResponse } from 'node:http';
import { applicationConfig } from '../config/application.ts';
import { environment } from '../config/environment.ts';
import { currentUser } from '../auth/request-context.ts';
import { logError, logInfo } from '../logging/logger.ts';

mkdirSync(dirname(environment.userDatabasePath), { recursive: true });
const db = new DatabaseSync(environment.userDatabasePath);
db.exec(`CREATE TABLE IF NOT EXISTS mutation_audit(
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  username TEXT NOT NULL,
  operation TEXT NOT NULL,
  cluster_id TEXT,
  namespace TEXT,
  kind TEXT NOT NULL,
  name TEXT,
  uid TEXT,
  state TEXT NOT NULL,
  http_status INTEGER,
  requested_at TEXT NOT NULL,
  completed_at TEXT
);
CREATE INDEX IF NOT EXISTS mutation_audit_requested ON mutation_audit(requested_at);
CREATE INDEX IF NOT EXISTS mutation_audit_user_requested ON mutation_audit(user_id,requested_at);`);

function target(pathname: string): { kind: string; name: string | null } | null {
  if (pathname === '/api/clusters/login' || pathname === '/api/clusters/test-input') return null;
  const match = pathname.match(/^\/api\/(pods|deployments|configmaps|clusters)\/([^/]+)(?:\/(restart|replicas|resources|exec))?$/);
  if (match) {
    const kind = { pods: 'Pod', deployments: 'Deployment', configmaps: 'ConfigMap', clusters: 'Cluster' }[match[1] as 'pods' | 'deployments' | 'configmaps' | 'clusters'];
    try { return { kind, name: decodeURIComponent(match[2]).slice(0, 253) }; } catch { return { kind, name: null }; }
  }
  if (pathname === '/api/clusters') return { kind: 'Cluster', name: null };
  return null;
}

export function auditMutation(method: string | undefined, url: URL, response: ServerResponse): void {
  if (!method || !['POST', 'PUT', 'PATCH', 'DELETE'].includes(method)) return;
  const resource = target(url.pathname);
  if (!resource) return;
  const actor = currentUser();
  const id = randomUUID();
  const operation = `${method} ${url.pathname.match(/\/(restart|replicas|resources|exec)$/)?.[1] ?? resource.kind}`;
  db.prepare('INSERT INTO mutation_audit(id,user_id,username,operation,cluster_id,namespace,kind,name,uid,state,requested_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)')
    .run(id, actor.userId, actor.username, operation, url.searchParams.get('clusterId'), url.searchParams.get('namespace'), resource.kind, resource.name, url.searchParams.get('uid'), 'requested', new Date().toISOString());
  response.setHeader('X-Operation-Id', id);
  let finished = false;
  const complete = (state: 'succeeded' | 'failed' | 'unknown', status: number | null) => {
    if (finished) return;
    finished = true;
    db.prepare('UPDATE mutation_audit SET state=?,http_status=?,completed_at=? WHERE id=?').run(state, status, new Date().toISOString(), id);
    const fields = { operationId: id, clusterId: url.searchParams.get('clusterId'), namespace: url.searchParams.get('namespace'), kind: resource.kind, name: resource.name, uid: url.searchParams.get('uid'), status };
    if (state === 'failed') logError('mutation failed', `HTTP ${status ?? 'unknown'}`, fields);
    else logInfo(`mutation ${state}`, fields);
  };
  response.once('finish', () => complete(response.getHeader('X-Mutation-Outcome') === 'unknown' ? 'unknown' : response.statusCode >= 200 && response.statusCode < 300 ? 'succeeded' : 'failed', response.statusCode));
  response.once('close', () => complete('unknown', null));
  const cutoff = new Date(Date.now() - applicationConfig.mutationAudit.retentionDays * 86_400_000).toISOString();
  db.prepare('DELETE FROM mutation_audit WHERE requested_at < ?').run(cutoff);
}

function cpuTimes(): { idle: number; total: number } {
  let idle = 0; let total = 0;
  for (const cpu of cpus()) { idle += cpu.times.idle; total += Object.values(cpu.times).reduce((sum, part) => sum + part, 0); }
  return { idle, total };
}

export async function systemStatus(userId: string) {
  const first = cpuTimes();
  await new Promise((resolve) => setTimeout(resolve, 150));
  const second = cpuTimes();
  const totalDelta = second.total - first.total;
  const cpuPercent = totalDelta > 0 ? Math.max(0, Math.min(100, (1 - (second.idle - first.idle) / totalDelta) * 100)) : null;
  const disk = statfsSync(dirname(environment.userDatabasePath));
  const dbBytes = [environment.userDatabasePath, `${environment.userDatabasePath}-wal`, `${environment.userDatabasePath}-shm`]
    .reduce((size, path) => size + (existsSync(path) ? statSync(path).size : 0), 0);
  const recent = db.prepare('SELECT id,operation,cluster_id,namespace,kind,name,uid,state,http_status,requested_at,completed_at FROM mutation_audit WHERE user_id=? ORDER BY requested_at DESC LIMIT 25').all(userId);
  return {
    databaseBytes: dbBytes,
    auditRetentionDays: applicationConfig.mutationAudit.retentionDays,
    cpuPercent,
    memoryTotalBytes: totalmem(),
    memoryUsedBytes: totalmem() - freemem(),
    diskTotalBytes: disk.blocks * disk.bsize,
    diskUsedBytes: (disk.blocks - disk.bfree) * disk.bsize,
    recent
  };
}
