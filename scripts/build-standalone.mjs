import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { copyFile, cp, chmod, mkdir, readFile, rename, rm, writeFile } from 'node:fs/promises';
import { accessSync, constants, statSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, isAbsolute, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';

const require = createRequire(import.meta.url);
const { inject } = require('postject');
const scriptsDirectory = dirname(fileURLToPath(import.meta.url));
const root = dirname(scriptsDirectory);
const buildDirectory = join(root, '.build', 'standalone');
const packageName = 'quaber';
const releaseRoot = join(root, 'release');
// Never build into release/quaber: it may be an installed instance containing
// .env, user databases and credentials. Only .build/standalone is disposable.
const outputDirectory = join(buildDirectory, packageName);
const bundlePath = join(buildDirectory, 'quaber.cjs');
const seaConfigPath = join(buildDirectory, 'sea-config.json');
const seaBlobPath = join(buildDirectory, 'quaber.blob');
const executablePath = join(outputDirectory, 'quaber');

function verifyExecutable(path, label) {
  try {
    accessSync(path, constants.X_OK);
    if (!statSync(path).isFile()) throw new Error('not a regular file');
  } catch {
    throw new Error(`${label} is not an executable regular file: ${path}`);
  }
}

function run(command, args, cwd = root) {
  execFileSync(command, args, { cwd, stdio: 'inherit' });
}

if (process.platform !== 'linux' || process.arch !== 'x64') {
  throw new Error(`Standalone build currently supports linux-x64, received ${process.platform}-${process.arch}.`);
}

const loginKubectlSource = process.env.QUABER_LOGIN_KUBECTL
  ? (isAbsolute(process.env.QUABER_LOGIN_KUBECTL)
    ? process.env.QUABER_LOGIN_KUBECTL
    : resolve(root, process.env.QUABER_LOGIN_KUBECTL))
  : undefined;
if (loginKubectlSource) verifyExecutable(loginKubectlSource, 'Login-capable kubectl');
if (loginKubectlSource?.startsWith(`${buildDirectory}/`)) {
  throw new Error('Build inputs must not be inside the temporary build directory.');
}

await rm(buildDirectory, { recursive: true, force: true });
await mkdir(buildDirectory, { recursive: true });
await mkdir(join(outputDirectory, 'addons'), { recursive: true });
await mkdir(join(outputDirectory, 'data'), { recursive: true });

run('npm', ['--prefix', 'client', 'run', 'build']);
run('npm', ['--prefix', 'server', 'run', 'typecheck']);

await build({
  entryPoints: [join(root, 'server', 'src', 'index.ts')],
  outfile: bundlePath,
  bundle: true,
  platform: 'node',
  format: 'cjs',
  target: 'node22',
  sourcemap: false,
  minify: false,
  logLevel: 'info'
});

await writeFile(seaConfigPath, `${JSON.stringify({
  main: bundlePath,
  output: seaBlobPath,
  disableExperimentalSEAWarning: true,
  useSnapshot: false,
  useCodeCache: false
}, null, 2)}\n`);

run(process.execPath, ['--experimental-sea-config', seaConfigPath]);
await copyFile(process.execPath, executablePath);
await inject(executablePath, 'NODE_SEA_BLOB', await readFile(seaBlobPath), {
  sentinelFuse: 'NODE_SEA_FUSE_fce680ab2cc467b6e072b8b5df1996b2'
});
await chmod(executablePath, 0o755);

await cp(join(root, 'client', 'dist'), join(outputDirectory, 'public'), { recursive: true });
if (loginKubectlSource) {
  await copyFile(loginKubectlSource, join(outputDirectory, 'addons', 'kubectl'));
  await chmod(join(outputDirectory, 'addons', 'kubectl'), 0o755);
}
await copyFile(join(root, 'check-runtime.sh'), join(outputDirectory, 'check-runtime.sh'));
await chmod(join(outputDirectory, 'check-runtime.sh'), 0o755);
await copyFile(join(root, 'quaber.config.json'), join(outputDirectory, 'quaber.config.json'));
await copyFile(join(root, '.env.example'), join(outputDirectory, '.env.example'));
await copyFile(join(root, 'server', 'bootstrap-user.example.json'), join(outputDirectory, 'data', 'bootstrap-user.example.json'));
await copyFile(join(root, 'STANDALONE.md'), join(outputDirectory, 'README.md'));

try {
  execFileSync('xz', ['--version'], { stdio: 'ignore' });
} catch (error) {
  if ((error?.code ?? '') === 'ENOENT') throw new Error('xz is required to build the standalone archive.');
  throw error;
}

const stagedArchivePath = join(buildDirectory, `${packageName}.tar.xz`);
run('tar', ['-cJf', stagedArchivePath, packageName], buildDirectory);
const archivePath = join(releaseRoot, `${packageName}.tar.xz`);
const digest = createHash('sha256').update(await readFile(stagedArchivePath)).digest('hex');
await mkdir(releaseRoot, { recursive: true });
await rename(stagedArchivePath, archivePath);
await writeFile(`${archivePath}.sha256`, `${digest}  ${packageName}.tar.xz\n`);
console.log(`\nStandalone package: ${outputDirectory}`);
console.log(`Archive: ${archivePath}`);
console.log(`SHA-256: ${digest}`);
