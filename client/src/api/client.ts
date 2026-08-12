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

export async function apiGet<T>(path: string): Promise<T> {
  const response = await fetch(path, { headers: { Accept: 'application/json' } });
  if (!response.ok) throw await parseError(response);
  return response.json() as Promise<T>;
}

export async function apiSend<T>(path: string, method: 'POST' | 'PUT', body?: unknown): Promise<T> {
  const response = await fetch(path, {
    method,
    headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body)
  });
  if (!response.ok) throw await parseError(response);
  return response.json() as Promise<T>;
}

export async function apiDelete(path: string): Promise<void> {
  const response = await fetch(path, { method: 'DELETE' });
  if (!response.ok) throw await parseError(response);
}
