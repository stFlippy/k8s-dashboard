async function parseError(response: Response): Promise<Error> {
  let message = `Request failed: ${response.status}`;
  try {
    const body = (await response.json()) as { error?: string };
    if (body.error) message = body.error;
  } catch {
    // Keep status fallback.
  }
  return new Error(message);
}
let csrfToken = '';
let sessionReloadStarted = false;
export function setCsrfToken(value: string): void { csrfToken = value; }
function reloadOnExpiredSession(response: Response, path: string): void {
  if (response.status !== 401 || path === '/api/auth/login' || path === '/api/auth/me' || sessionReloadStarted) return;
  sessionReloadStarted = true;
  csrfToken = '';
  window.location.reload();
}

export async function apiGet<T>(path: string): Promise<T> {
  const response = await fetch(path, { headers: { Accept: 'application/json' } });
  reloadOnExpiredSession(response, path);
  if (!response.ok) throw await parseError(response);
  return response.json() as Promise<T>;
}

export async function apiSend<T>(path: string, method: 'POST' | 'PUT', body?: unknown): Promise<T> {
  const response = await fetch(path, {
    method,
    headers: { Accept: 'application/json', 'Content-Type': 'application/json', ...(csrfToken ? { 'X-CSRF-Token': csrfToken } : {}) },
    body: body === undefined ? undefined : JSON.stringify(body)
  });
  reloadOnExpiredSession(response, path);
  if (!response.ok) throw await parseError(response);
  return response.json() as Promise<T>;
}

export async function apiDelete(path: string): Promise<void> {
  const response = await fetch(path, { method: 'DELETE', headers: csrfToken ? { 'X-CSRF-Token': csrfToken } : {} });
  reloadOnExpiredSession(response, path);
  if (!response.ok) throw await parseError(response);
}
