import { createServer, type IncomingMessage, type ServerResponse } from 'node:http';
import { environment } from './config/environment.ts';
import { handleApiRequest } from './api/router.ts';

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
    response.setHeader('Access-Control-Allow-Origin', 'http://localhost:5173');
    response.setHeader('Access-Control-Allow-Headers', 'Content-Type');
    response.setHeader('Access-Control-Allow-Methods', 'GET,POST,PUT,PATCH,DELETE,OPTIONS');

    if (request.method === 'OPTIONS') {
      response.statusCode = 204;
      response.end();
      return;
    }

    const handled = await handleApiRequest(request, response, url);
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
