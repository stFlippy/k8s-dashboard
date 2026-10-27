import { applicationConfig } from '../config/application.ts';
import { currentUser } from '../auth/request-context.ts';

type Fields = Record<string, string | number | boolean | null | undefined>;

const forbiddenKey = /password|token|secret|authorization|cookie|kubeconfig|command|body/i;

function clean(value: string | number | boolean | null | undefined): string {
  const text = String(value ?? '-').replace(/[\r\n\t]+/g, ' ').replace(/\s{2,}/g, ' ').trim();
  return JSON.stringify(text.slice(0, 160));
}

function suffix(fields: Fields): string {
  return Object.entries(fields)
    .filter(([key, value]) => !forbiddenKey.test(key) && value !== undefined)
    .map(([key, value]) => `${key}=${clean(value)}`)
    .join(' ');
}

function actor(): Fields {
  try {
    const identity = currentUser();
    return { user: identity.username, userId: identity.userId };
  } catch {
    return { user: 'anonymous' };
  }
}

export function logInfo(action: string, fields: Fields = {}): void {
  if (!applicationConfig.logging.enabled) return;
  console.info(`${new Date().toISOString()} INFO action=${clean(action)} ${suffix({ ...actor(), ...fields })}`.trim());
}

export function logError(action: string, error: unknown, fields: Fields = {}): void {
  const message = error instanceof Error ? error.message : typeof error === 'string' ? error : 'Unknown error';
  console.error(`${new Date().toISOString()} ERROR action=${clean(action)} ${suffix({ ...actor(), ...fields, message })}`.trim());
}

export function logExternalCommandFailure(action: string, exitCode: number | null, stderr: string, stdout: string): void {
  // JSON quoting keeps multiline kubectl output intact without letting it forge
  // additional log records. The caller must mask credentials before logging.
  console.error(`${new Date().toISOString()} ERROR action=${clean(action)} ${suffix(actor())} exitCode=${JSON.stringify(exitCode)} stderr=${JSON.stringify(stderr)} stdout=${JSON.stringify(stdout)}`.trim());
}

export function requestAction(method: string | undefined, pathname: string): string | null {
  const verb = method ?? 'UNKNOWN';
  if (pathname === '/api/clusters/login') return 'cluster login';
  if (pathname === '/api/clusters/test-input' || /\/clusters\/[^/]+\/test$/.test(pathname)) return 'cluster connection test';
  if (pathname === '/api/clusters' && verb === 'POST') return 'cluster create';
  if (/\/clusters\/[^/]+$/.test(pathname) && verb === 'PUT') return 'cluster update';
  if (/\/clusters\/[^/]+$/.test(pathname) && verb === 'DELETE') return 'cluster delete';
  if (/\/deployments\/[^/]+\/restart$/.test(pathname)) return 'deployment restart';
  if (/\/deployments\//.test(pathname) && verb !== 'GET') return `deployment ${verb.toLowerCase()}`;
  if (/\/pods\/[^/]+\/exec$/.test(pathname)) return 'Pod console command';
  if (/\/pods\/[^/]+\/logs$/.test(pathname)) return 'Pod logs view';
  if (/\/pods\/[^/]+\/metrics$/.test(pathname)) return 'Pod metrics view';
  if (/\/pods\/[^/]+\/events$/.test(pathname)) return 'Pod events view';
  if (/\/pods\/[^/]+$/.test(pathname) && verb === 'GET') return 'Pod details view';
  if (/\/pods\//.test(pathname) && verb !== 'GET') return `Pod ${verb.toLowerCase()}`;
  if (/\/deployments\/[^/]+\/events$/.test(pathname)) return 'deployment events view';
  if (/\/deployments\/[^/]+$/.test(pathname) && verb === 'GET') return 'deployment details view';
  if (/\/configmaps\//.test(pathname) && verb !== 'GET') return `ConfigMap ${verb.toLowerCase()}`;
  if (pathname === '/api/resource-quotas') return 'resource quotas view';
  if (pathname === '/api/cluster-health') return 'cluster health view';
  return null;
}
