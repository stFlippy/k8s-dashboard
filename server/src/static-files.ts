import type { IncomingMessage, ServerResponse } from 'node:http';
import { createReadStream } from 'node:fs';
import { stat } from 'node:fs/promises';
import { extname, resolve, sep } from 'node:path';
import { environment } from './config/environment.ts';

const contentTypes: Record<string, string> = {
  '.css': 'text/css; charset=utf-8',
  '.html': 'text/html; charset=utf-8',
  '.ico': 'image/x-icon',
  '.js': 'text/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.png': 'image/png',
  '.svg': 'image/svg+xml',
  '.webp': 'image/webp',
  '.woff': 'font/woff',
  '.woff2': 'font/woff2'
};

async function regularFile(path: string): Promise<boolean> {
  try { return (await stat(path)).isFile(); } catch { return false; }
}

export async function serveStaticFile(request: IncomingMessage, response: ServerResponse, url: URL): Promise<boolean> {
  const root = environment.staticDirectory;
  if (!root || (request.method !== 'GET' && request.method !== 'HEAD')) return false;

  let pathname: string;
  try { pathname = decodeURIComponent(url.pathname); } catch { return false; }
  if (pathname.includes('\0')) return false;

  const relative = pathname === '/' ? 'index.html' : pathname.replace(/^\/+/, '');
  let file = resolve(root, relative);
  const normalizedRoot = resolve(root);
  if (file !== normalizedRoot && !file.startsWith(`${normalizedRoot}${sep}`)) return false;
  if (!(await regularFile(file))) file = resolve(normalizedRoot, 'index.html');
  if (!(await regularFile(file))) return false;

  const extension = extname(file).toLowerCase();
  response.statusCode = 200;
  response.setHeader('Content-Type', contentTypes[extension] ?? 'application/octet-stream');
  response.setHeader('Cache-Control', extension === '.html' ? 'no-cache' : 'public, max-age=31536000, immutable');
  if (request.method === 'HEAD') {
    response.end();
    return true;
  }
  await new Promise<void>((resolveStream, reject) => {
    const stream = createReadStream(file);
    stream.on('error', reject);
    stream.on('end', resolveStream);
    stream.pipe(response);
  });
  return true;
}
