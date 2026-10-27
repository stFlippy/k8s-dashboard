import { readFileSync } from 'node:fs';
import { runtimeFile } from './runtime.ts';

interface ApplicationConfig {
  logging: {
    enabled: boolean;
    level: 'info';
  };
}

const defaults: ApplicationConfig = { logging: { enabled: true, level: 'info' } };
const configPath = runtimeFile('quaber.config.json');

function loadConfig(): ApplicationConfig {
  try {
    const parsed = JSON.parse(readFileSync(configPath, 'utf8')) as Partial<ApplicationConfig>;
    if (parsed.logging?.enabled !== undefined && typeof parsed.logging.enabled !== 'boolean') {
      throw new Error('logging.enabled must be true or false.');
    }
    if (parsed.logging?.level !== undefined && parsed.logging.level !== 'info') {
      throw new Error("logging.level must be 'info'.");
    }
    return {
      logging: {
        enabled: parsed.logging?.enabled ?? defaults.logging.enabled,
        level: parsed.logging?.level ?? defaults.logging.level
      }
    };
  } catch (error) {
    const message = error instanceof Error ? error.message : 'unknown configuration error';
    console.error(`[ERROR] configuration file=${configPath} message=${message}`);
    return defaults;
  }
}

export const applicationConfig = loadConfig();
