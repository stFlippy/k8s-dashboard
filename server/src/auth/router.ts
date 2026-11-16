import type { IncomingMessage, ServerResponse } from 'node:http';
import { changeLocalPassword, createLocalUser, generateTemporaryPassword, getPreferences, listUsers, loginLocalUser, loginVerifiedUser, logout, resetLocalPassword, session, setAdministrator, setPreferences, type AuthSession } from './store.ts';
import { AuthProviderError, verifyExternalCredentials } from './external.ts';
import { applicationConfig } from '../config/application.ts';
import { logError, logInfo } from '../logging/logger.ts';
import { stopUserClusterProxies } from '../kubernetes/kubectl-proxy.ts';

const cookie = (request: IncomingMessage) => request.headers.cookie?.split(';').map((value) => value.trim()).find((value) => value.startsWith('quaber_session='))?.slice(15);
const attempts = new Map<string, { count: number; until: number }>();
const json = (response: ServerResponse, status: number, data: unknown) => { response.statusCode = status; response.setHeader('Content-Type', 'application/json; charset=utf-8'); response.end(JSON.stringify(data)); };
const safeUser = (value: AuthSession) => ({ ...value.user, csrf: value.csrf, authMode: applicationConfig.authentication.mode });

async function body<T>(request: IncomingMessage): Promise<T> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of request) {
    const value = Buffer.from(chunk);
    size += value.length;
    if (size > 1_000_000) throw new Error('Request too large.');
    chunks.push(value);
  }
  return JSON.parse(Buffer.concat(chunks).toString('utf8')) as T;
}

export const getRequestSession = (request: IncomingMessage) => session(cookie(request));

export async function handleAuthRequest(request: IncomingMessage, response: ServerResponse, url: URL): Promise<boolean> {
  if (!url.pathname.startsWith('/api/auth/') && !url.pathname.startsWith('/api/users') && !url.pathname.startsWith('/api/preferences')) return false;
  try {
    if (url.pathname === '/api/auth/login' && request.method === 'POST') {
      const input = await body<{ username?: unknown; password?: unknown }>(request);
      if (typeof input?.username !== 'string' || !input.username.trim() || input.username !== input.username.trim() || input.username.length > 128 || /[\u0000-\u001f\u007f]/u.test(input.username) || typeof input.password !== 'string' || !input.password || input.password.length > 1024) {
        json(response, 400, { error: 'Введите логин и пароль.' });
        return true;
      }
      const key = `${request.socket.remoteAddress ?? 'unknown'}:${input.username.trim().toLowerCase()}`;
      const attempt = attempts.get(key);
      if (attempt && attempt.count >= 5 && attempt.until > Date.now()) {
        logInfo('dashboard login blocked', { user: input.username });
        json(response, 429, { error: 'Слишком много попыток. Повторите через 15 минут.' });
        return true;
      }
      let result;
      if (applicationConfig.authentication.mode === 'local') {
        result = loginLocalUser(input.username, input.password);
      } else {
        const verified = await verifyExternalCredentials(input.username, input.password);
        result = verified ? loginVerifiedUser(input.username) : null;
      }
      if (!result) {
        attempts.set(key, { count: attempt && attempt.until > Date.now() ? attempt.count + 1 : 1, until: Date.now() + 15 * 60_000 });
        logInfo('dashboard login rejected', { user: input.username });
        json(response, 401, { error: 'Неверный логин или пароль.' });
        return true;
      }
      attempts.delete(key);
      logInfo('dashboard login', { user: result.user.username, userId: result.user.id, created: result.created, authMode: applicationConfig.authentication.mode });
      const secure = request.headers['x-forwarded-proto'] === 'https' ? '; Secure' : '';
      response.setHeader('Set-Cookie', `quaber_session=${result.token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200${secure}`);
      json(response, 200, { ...result.user, csrf: result.csrf, authMode: applicationConfig.authentication.mode });
      return true;
    }

    const auth = getRequestSession(request);
    if (!auth) { json(response, 401, { error: 'Authentication required.' }); return true; }
    if (request.method !== 'GET' && request.headers['x-csrf-token'] !== auth.csrf) { json(response, 403, { error: 'Invalid CSRF token.' }); return true; }
    if (url.pathname === '/api/auth/me' && request.method === 'GET') { json(response, 200, safeUser(auth)); return true; }
    if (url.pathname === '/api/auth/logout' && request.method === 'POST') {
      logInfo('dashboard logout', { user: auth.user.username, userId: auth.user.id });
      stopUserClusterProxies(auth.user.id);
      logout(cookie(request));
      response.setHeader('Set-Cookie', 'quaber_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0');
      json(response, 200, { ok: true });
      return true;
    }
    if (url.pathname === '/api/auth/change-password' && request.method === 'POST') {
      if (applicationConfig.authentication.mode !== 'local') { json(response, 410, { error: 'Пароли управляются внешним сервером авторизации.' }); return true; }
      const input = await body<{ currentPassword?: unknown; newPassword?: unknown; repeatPassword?: unknown }>(request);
      if (typeof input?.currentPassword !== 'string' || typeof input.newPassword !== 'string' || typeof input.repeatPassword !== 'string') throw new Error('Введите текущий и новый пароль дважды.');
      if (input.newPassword !== input.repeatPassword) throw new Error('Новые пароли не совпадают.');
      changeLocalPassword(auth.user.id, input.currentPassword, input.newPassword);
      logInfo('local password change', { user: auth.user.username, userId: auth.user.id });
      stopUserClusterProxies(auth.user.id);
      response.setHeader('Set-Cookie', 'quaber_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0');
      json(response, 200, { ok: true });
      return true;
    }
    if (auth.user.mustChangePassword) { json(response, 403, { error: 'Сначала смените временный пароль.' }); return true; }
    if (url.pathname.startsWith('/api/users') && !auth.user.isAdmin) { json(response, 403, { error: 'Administrator role required.' }); return true; }
    if (url.pathname === '/api/users' && request.method === 'GET') { json(response, 200, listUsers()); return true; }
    if (url.pathname === '/api/users' && request.method === 'POST') {
      if (applicationConfig.authentication.mode !== 'local') { json(response, 410, { error: 'Пользователи создаются на внешнем сервере авторизации.' }); return true; }
      const input = await body<{ username?: unknown }>(request);
      if (typeof input?.username !== 'string') throw new Error('Введите имя пользователя.');
      const temporaryPassword=generateTemporaryPassword(),user=createLocalUser(input.username,temporaryPassword);
      logInfo('local user create', { user: auth.user.username, targetUser: user.username });
      json(response, 201, { user, temporaryPassword });
      return true;
    }
    const reset = url.pathname.match(/^\/api\/users\/([^/]+)\/reset-password$/);
    if (reset && request.method === 'POST') {
      if (applicationConfig.authentication.mode !== 'local') { json(response, 410, { error: 'Пароли управляются внешним сервером авторизации.' }); return true; }
      const targetUserId=decodeURIComponent(reset[1]),temporaryPassword=generateTemporaryPassword();
      resetLocalPassword(targetUserId,temporaryPassword);
      stopUserClusterProxies(targetUserId);
      logInfo('local password reset', { user: auth.user.username, targetUserId });
      json(response, 200, { ok: true, temporaryPassword });
      return true;
    }
    const role = url.pathname.match(/^\/api\/users\/([^/]+)\/administrator$/);
    if (role && request.method === 'PUT') {
      const targetUserId = decodeURIComponent(role[1]);
      const input = await body<{ isAdmin: boolean }>(request);
      if (typeof input.isAdmin !== 'boolean') throw new Error('isAdmin must be boolean.');
      setAdministrator(targetUserId, input.isAdmin, auth.user.id);
      logInfo('user administrator role update', { user: auth.user.username, targetUserId, isAdmin: input.isAdmin });
      json(response, 200, { ok: true });
      return true;
    }
    if (url.pathname === '/api/preferences' && request.method === 'GET') { json(response, 200, getPreferences(auth.user.id)); return true; }
    if (url.pathname === '/api/preferences' && request.method === 'PUT') { setPreferences(auth.user.id, await body<unknown>(request)); json(response, 200, { ok: true }); return true; }
    json(response, 404, { error: 'Not found.' });
    return true;
  } catch (error) {
    if (error instanceof AuthProviderError) {
      logError('external authentication unavailable', error);
      json(response, 502, { error: error.message });
      return true;
    }
    logError('authentication API request failed', error, { path: url.pathname });
    json(response, 400, { error: error instanceof Error ? error.message : 'Request failed.' });
    return true;
  }
}
