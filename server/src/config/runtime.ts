import { existsSync, readFileSync } from 'node:fs';
import { isSea } from 'node:sea';
import { dirname, resolve } from 'node:path';

function isProjectRoot(path: string): boolean {
  try {
    const parsed = JSON.parse(readFileSync(resolve(path, 'package.json'), 'utf8')) as { name?: unknown };
    return parsed.name === 'k8s-dashboard';
  } catch {
    return false;
  }
}

export const standaloneRuntime = isSea();

export const runtimeRoot = standaloneRuntime
  ? dirname(process.execPath)
  : [process.cwd(), resolve(process.cwd(), '..')].find(isProjectRoot) ?? process.cwd();

export function runtimeFile(...parts: string[]): string {
  return resolve(runtimeRoot, ...parts);
}

export function existingRuntimeFile(...parts: string[]): string | undefined {
  const path = runtimeFile(...parts);
  return existsSync(path) ? path : undefined;
}
