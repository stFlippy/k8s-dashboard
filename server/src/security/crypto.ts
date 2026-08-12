import { createCipheriv, createDecipheriv, createHash, randomBytes } from 'node:crypto';
import { environment } from '../config/environment.ts';

function key(): Buffer {
  if (!environment.masterKey) {
    throw new Error('DASHBOARD_MASTER_KEY is required before saving token/password credentials.');
  }
  if (environment.masterKey.length < 32) {
    throw new Error('DASHBOARD_MASTER_KEY must contain at least 32 characters.');
  }
  return createHash('sha256').update(environment.masterKey, 'utf8').digest();
}

export function secretsAvailable(): boolean {
  return Boolean(environment.masterKey && environment.masterKey.length >= 32);
}

export function encryptSecret(value: string): string {
  const iv = randomBytes(12);
  const cipher = createCipheriv('aes-256-gcm', key(), iv);
  const encrypted = Buffer.concat([cipher.update(value, 'utf8'), cipher.final()]);
  const tag = cipher.getAuthTag();
  return ['v1', iv.toString('base64'), tag.toString('base64'), encrypted.toString('base64')].join(':');
}

export function decryptSecret(value: string | undefined): string | undefined {
  if (!value) return undefined;
  const [version, ivEncoded, tagEncoded, payloadEncoded] = value.split(':');
  if (version !== 'v1' || !ivEncoded || !tagEncoded || !payloadEncoded) {
    throw new Error('Unsupported encrypted credential format.');
  }
  const decipher = createDecipheriv('aes-256-gcm', key(), Buffer.from(ivEncoded, 'base64'));
  decipher.setAuthTag(Buffer.from(tagEncoded, 'base64'));
  return Buffer.concat([
    decipher.update(Buffer.from(payloadEncoded, 'base64')),
    decipher.final()
  ]).toString('utf8');
}
