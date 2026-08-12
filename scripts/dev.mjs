import { spawn } from 'node:child_process';

const processes = [
  spawn('npm', ['--prefix', 'client', 'run', 'dev'], { stdio: 'inherit', shell: true }),
  spawn('npm', ['--prefix', 'server', 'run', 'dev'], { stdio: 'inherit', shell: true })
];

const shutdown = () => {
  for (const child of processes) {
    if (!child.killed) child.kill('SIGTERM');
  }
};

process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);

for (const child of processes) {
  child.on('exit', (code) => {
    if (code !== 0 && code !== null) process.exitCode = code;
  });
}
