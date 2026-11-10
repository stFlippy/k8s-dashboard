import { readFileSync } from 'node:fs';
import { scryptSync, timingSafeEqual } from 'node:crypto';

const usersPath = new URL('../users.json', import.meta.url);

export function verifyCredentials(username, password, path = usersPath) {
  const document = JSON.parse(readFileSync(path, 'utf8'));
  if (!Array.isArray(document?.users)) throw new Error('users.json must contain a users array.');
  const user = document.users.find((item) => typeof item.username === 'string' && item.username === username);
  if (!user || typeof user.passwordHash !== 'string') return false;
  const [scheme, encodedSalt, encodedHash] = user.passwordHash.split('$');
  if (scheme !== 'scrypt' || !encodedSalt || !encodedHash) throw new Error('Invalid password hash in users.json.');
  const expected = Buffer.from(encodedHash, 'base64');
  if (expected.length !== 64) throw new Error('Invalid password hash length in users.json.');
  const actual = scryptSync(password, Buffer.from(encodedSalt, 'base64'), expected.length);
  return timingSafeEqual(actual, expected);
}

export async function authMiddleware(request, response, next) {
  const path = new URL(request.url ?? '/', 'http://localhost').pathname;
  if (path !== '/api/auth/verify') { next(); return; }
  response.setHeader('Content-Type', 'application/json; charset=utf-8');
  response.setHeader('Cache-Control', 'no-store');
  if (request.method !== 'POST') { response.statusCode = 405; response.end('false'); return; }
  if (!request.headers['content-type']?.toLowerCase().startsWith('application/json')) { response.statusCode = 415; response.end('false'); return; }
  try {
    const chunks = [];
    let size = 0;
    for await (const chunk of request) {
      size += chunk.length;
      if (size > 8192) { response.statusCode = 413; response.end('false'); return; }
      chunks.push(chunk);
    }
    const input = JSON.parse(Buffer.concat(chunks).toString('utf8'));
    if (typeof input?.username !== 'string' || typeof input?.password !== 'string') { response.statusCode = 400; response.end('false'); return; }
    response.statusCode = 200;
    response.end(JSON.stringify(verifyCredentials(input.username, input.password)));
  } catch (error) {
    response.statusCode = 500;
    response.end('false');
    console.error(`Mock authentication error: ${error instanceof Error ? error.message : String(error)}`);
  }
}
