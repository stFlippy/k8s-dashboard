import { createServer, type IncomingMessage, type ServerResponse } from 'node:http';
import { environment } from './config/environment.ts';
import { handleApiRequest } from './api/router.ts';
import { getRequestSession, handleAuthRequest } from './auth/router.ts';
import { runAsUser } from './auth/request-context.ts';

const server = createServer(async (request: IncomingMessage, response: ServerResponse) => {
  try {
    if (!request.url) {
      response.statusCode = 400;
      response.end('Bad Request');
      return;
    }

    const url = new URL(request.url, `http://${request.headers.host ?? 'localhost'}`);

    response.setHeader('Cache-Control', 'no-store');
    response.setHeader('X-Content-Type-Options', 'nosniff');
    response.setHeader('X-Frame-Options', 'DENY');
    response.setHeader('Referrer-Policy', 'no-referrer');
    response.setHeader('Permissions-Policy', 'camera=(), microphone=(), geolocation=()');
    response.setHeader('Content-Security-Policy', "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self' ws://localhost:5173 http://localhost:3001");
    response.setHeader('Access-Control-Allow-Origin', 'http://localhost:5173');
    response.setHeader('Access-Control-Allow-Headers', 'Content-Type,X-CSRF-Token');
    response.setHeader('Access-Control-Allow-Methods', 'GET,POST,PUT,PATCH,DELETE,OPTIONS');

    if (request.method === 'OPTIONS') {
      response.statusCode = 204;
      response.end();
      return;
    }

    if (await handleAuthRequest(request, response, url)) return;
    let auth = null;
    if (url.pathname.startsWith('/api/')) {
      auth = getRequestSession(request);
      if (!auth) { response.statusCode=401;response.setHeader('Content-Type','application/json; charset=utf-8');response.end(JSON.stringify({error:'Authentication required.'}));return; }
      if (auth.user.mustChangePassword) { response.statusCode=403;response.setHeader('Content-Type','application/json; charset=utf-8');response.end(JSON.stringify({error:'Password change required.'}));return; }
      if (request.method !== 'GET' && request.headers['x-csrf-token'] !== auth.csrf) { response.statusCode=403;response.setHeader('Content-Type','application/json; charset=utf-8');response.end(JSON.stringify({error:'Invalid CSRF token.'}));return; }
    }
    const handled = auth
      ? await runAsUser({ userId: auth.user.id, username: auth.user.username }, () => handleApiRequest(request, response, url))
      : await handleApiRequest(request, response, url);
    if (!handled) {
      response.statusCode = 404;
      response.end('Not Found');
    }
  } catch (error) {
    console.error('Unhandled request error:', error instanceof Error ? error.message : 'Unknown error');
    if (!response.headersSent) {
      response.statusCode = 500;
      response.setHeader('Content-Type', 'application/json; charset=utf-8');
    }
    if (!response.writableEnded) {
      response.end(JSON.stringify({ error: 'Internal server error' }));
    }
  }
});

server.on('error', (error: Error) => {
  console.error('Server error:', error.message);
  process.exitCode = 1;
});

server.listen(environment.port, () => {
  console.log(`K8s backend listening on http://localhost:${environment.port}`);
});
