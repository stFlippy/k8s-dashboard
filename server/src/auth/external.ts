import { applicationConfig } from '../config/application.ts';

export class AuthProviderError extends Error {
  constructor(message: string) { super(message); this.name = 'AuthProviderError'; }
}

export async function verifyExternalCredentials(username: string, password: string): Promise<boolean> {
  const { url, timeoutMs } = applicationConfig.authentication;
  if (!url) throw new AuthProviderError('Адрес сервера авторизации не настроен.');
  let response: Response;
  try {
    response = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify({ username, password }),
      redirect: 'error',
      cache: 'no-store',
      signal: AbortSignal.timeout(timeoutMs)
    });
  } catch {
    throw new AuthProviderError('Сервер авторизации недоступен.');
  }
  if (!response.ok) throw new AuthProviderError(`Сервер авторизации вернул HTTP ${response.status}.`);
  if (!response.headers.get('content-type')?.toLowerCase().includes('application/json')) {
    throw new AuthProviderError('Сервер авторизации вернул не JSON.');
  }
  try {
    const result: unknown = await response.json();
    if (typeof result !== 'boolean') throw new Error('Expected JSON boolean.');
    return result;
  } catch {
    throw new AuthProviderError('Сервер авторизации вернул некорректный ответ.');
  }
}
