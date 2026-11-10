import { access, readFile, readdir } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const scriptsDir = dirname(fileURLToPath(import.meta.url));
const root = dirname(scriptsDir);

const required = [
  'client/package.json',
  'client/vite.config.ts',
  'client/public/icon.webp',
  'client/public/logo.png',
  'client/src/main.tsx',
  'client/src/app/App.tsx',
  'client/src/components/matrix/Matrix.tsx',
  'client/src/components/quotas/QuotasPage.tsx',
  'client/src/components/configmaps/ConfigMapsPage.tsx',
  'client/src/components/clusters/ClusterPanel.tsx',
  'server/package.json',
  'server/tsconfig.json',
  'server/src/index.ts',
  'server/src/api/router.ts',
  'server/src/kubernetes/cluster-client.ts',
  'server/src/clusters/store.ts',
  'server/src/security/crypto.ts'
];

for (const file of required) await access(join(root, file));

const clientPackage = JSON.parse(await readFile(join(root, 'client/package.json'), 'utf8'));
if (!clientPackage.devDependencies?.['@types/node']) throw new Error('client needs @types/node.');
if (!clientPackage.dependencies?.['@tanstack/react-query']) throw new Error('client needs TanStack Query.');

const serverPackage = JSON.parse(await readFile(join(root, 'server/package.json'), 'utf8'));
if (!serverPackage.dependencies?.['@kubernetes/client-node']) throw new Error('server needs @kubernetes/client-node.');


async function collectTypeScriptFiles(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  const files = [];
  for (const entry of entries) {
    const fullPath = join(directory, entry.name);
    if (entry.isDirectory()) {
      files.push(...await collectTypeScriptFiles(fullPath));
    } else if (entry.isFile() && (entry.name.endsWith('.ts') || entry.name.endsWith('.tsx'))) {
      files.push(fullPath);
    }
  }
  return files;
}

const tsFiles = [
  ...await collectTypeScriptFiles(join(root, 'client/src')),
  ...await collectTypeScriptFiles(join(root, 'server/src')),
  join(root, 'client/vite.config.ts')
];

for (const file of tsFiles) {
  const source = await readFile(file, 'utf8');
  if (/\bany\b/.test(source)) {
    throw new Error(`Forbidden TypeScript 'any' token found in ${file.slice(root.length + 1)}.`);
  }
}

for (const configPath of ['client/tsconfig.app.json', 'client/tsconfig.node.json', 'server/tsconfig.json']) {
  const config = JSON.parse(await readFile(join(root, configPath), 'utf8'));
  if (config.compilerOptions?.strict !== true || config.compilerOptions?.noImplicitAny !== true) {
    throw new Error(`${configPath} must enable strict and noImplicitAny.`);
  }
}

const gitignore = await readFile(join(root, '.gitignore'), 'utf8');
if (!gitignore.includes('server/data/clusters.json')) throw new Error('Encrypted runtime cluster store must be ignored.');
if (existsSync(join(root, '.env'))) console.warn('Local .env exists; ensure release packaging excludes it.');
if (existsSync(join(root, 'server/data/clusters.json'))) console.warn('Local server/data/clusters.json exists; release packaging must exclude it.');

async function collectSourceFiles(directory, relative = '') {
  const files = [];
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    if (entry.isDirectory()) {
      if (['.git', '.build', 'node_modules', 'dist', 'release', 'send', 'temp', 'data'].includes(entry.name)) continue;
      files.push(...await collectSourceFiles(join(directory, entry.name), join(relative, entry.name)));
    } else if (entry.isFile()) files.push(join(relative, entry.name));
  }
  return files;
}

for (const path of await collectSourceFiles(root)) {
  if (/(?:^|\/)kubeconfig(?:\.[^/]+)?$/i.test(path) || /\.(?:sqlite|db|pem|p12|pfx|key)(?:-(?:wal|shm))?$/i.test(path)) {
    throw new Error(`Runtime data or credential in source tree: ${path}`);
  }
  if (!/\.(?:ya?ml|json|cfg)$/i.test(path)) continue;
  const contents = await readFile(join(root, path), 'utf8');
  if (/client-key-data\s*:\s*\S+|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----/.test(contents)) {
    throw new Error(`Private key material in source tree: ${path}`);
  }
}

console.log(`Project structure, credentials and no-any checks passed for ${tsFiles.length} TypeScript files.`);
