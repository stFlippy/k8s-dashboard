import { AsyncLocalStorage } from 'node:async_hooks';

export interface RequestIdentity {
  userId: string;
  username: string;
}

const requestIdentity = new AsyncLocalStorage<RequestIdentity>();

export function runAsUser<T>(identity: RequestIdentity, action: () => T): T {
  return requestIdentity.run(identity, action);
}

export function currentUser(): RequestIdentity {
  const identity = requestIdentity.getStore();
  if (!identity) throw new Error('Authenticated user context is missing.');
  return identity;
}
