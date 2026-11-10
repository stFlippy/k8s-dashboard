import { randomBytes, scryptSync } from 'node:crypto';
import { existsSync, readFileSync, renameSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const path = fileURLToPath(new URL('../users.json', import.meta.url));
const username = process.argv[2]?.trim();
if (!username || username.length > 128 || /[\u0000-\u001f\u007f]/u.test(username) || process.argv.length !== 3) {
  console.error('Использование: npm run add-user -- <имя>');
  process.exit(1);
}
const document = existsSync(path) ? JSON.parse(readFileSync(path, 'utf8')) : { users: [] };
if (!Array.isArray(document.users)) throw new Error('users.json must contain a users array.');
if (document.users.some((item) => typeof item.username === 'string' && item.username.toLocaleLowerCase() === username.toLocaleLowerCase())) throw new Error('User already exists.');
const password = `${randomBytes(18).toString('base64url')}!7a`;
const salt = randomBytes(16);
const hash = scryptSync(password, salt, 64);
document.users.push({ username, passwordHash: `scrypt$${salt.toString('base64')}$${hash.toString('base64')}` });
const temporary = `${path}.${randomBytes(4).toString('hex')}.tmp`;
writeFileSync(temporary, `${JSON.stringify(document, null, 2)}\n`, { mode: 0o600, flag: 'wx' });
renameSync(temporary, path);
console.log(`Пользователь ${username} добавлен в users.json.`);
console.log(`Пароль (показывается один раз): ${password}`);
