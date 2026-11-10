import { readFileSync } from 'node:fs';
import { runtimeFile } from './runtime.ts';

interface ApplicationConfig {
  authentication: { url: string | null; timeoutMs: number };
  logging: {
    enabled: boolean;
    level: 'debug' | 'info' | 'error';
  };
  mutationAudit: { retentionDays: number };
}

const defaults: ApplicationConfig = { authentication: { url: null, timeoutMs: 5000 }, logging: { enabled: true, level: 'info' }, mutationAudit: { retentionDays: 30 } };
const configPath = runtimeFile('quaber.config.json');

function loadConfig(): ApplicationConfig {
  try {
    const parsed = JSON.parse(readFileSync(configPath, 'utf8')) as Partial<ApplicationConfig>;
    if (parsed.logging?.enabled !== undefined && typeof parsed.logging.enabled !== 'boolean') {
      throw new Error('logging.enabled must be true or false.');
    }
    if (parsed.logging?.level !== undefined && !['debug', 'info', 'error'].includes(parsed.logging.level)) {
      throw new Error("logging.level must be 'debug', 'info' or 'error'.");
    }
    const retentionDays = parsed.mutationAudit?.retentionDays ?? defaults.mutationAudit.retentionDays;
    if (!Number.isInteger(retentionDays) || retentionDays < 1 || retentionDays > 3650) throw new Error('mutationAudit.retentionDays must be an integer between 1 and 3650.');
    const authUrl = parsed.authentication?.url ?? defaults.authentication.url;
    if (authUrl !== null) {
      if (typeof authUrl !== 'string') throw new Error('authentication.url must be a URL.');
      const url = new URL(authUrl);
      if (url.username || url.password || url.hash || url.search) throw new Error('authentication.url must not contain credentials, query or fragment.');
      if (url.protocol !== 'https:' && !(url.protocol === 'http:' && ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname))) {
        throw new Error('authentication.url must use HTTPS, except for a loopback mock server.');
      }
    }
    const timeoutMs = parsed.authentication?.timeoutMs ?? defaults.authentication.timeoutMs;
    if (!Number.isInteger(timeoutMs) || timeoutMs < 1000 || timeoutMs > 30000) throw new Error('authentication.timeoutMs must be 1000–30000.');
    return {
      authentication: { url: authUrl, timeoutMs },
      logging: {
        enabled: parsed.logging?.enabled ?? defaults.logging.enabled,
        level: parsed.logging?.level ?? defaults.logging.level
      },
      mutationAudit: { retentionDays }
    };
  } catch (error) {
    const message = error instanceof Error ? error.message : 'unknown configuration error';
    console.error(`[ERROR] configuration file=${configPath} message=${message}`);
    return defaults;
  }
}

export const applicationConfig = loadConfig();
