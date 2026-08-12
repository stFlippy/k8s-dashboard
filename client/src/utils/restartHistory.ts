import type { Pod } from '../types/kubernetes';

export interface RestartEvent {
  at: number;
  count: number;
}

export interface RestartHistoryEntry {
  observedTotal: number;
  lastObservedAt: number;
  events: RestartEvent[];
}

export type RestartHistory = Record<string, RestartHistoryEntry>;

// v4 also treats recently created replacement pods as restarted. A new key
// prevents an older empty baseline from hiding that signal.
const STORAGE_KEY = 'quaber.restartHistory.v4';
const ONE_HOUR_MS = 60 * 60 * 1000;

function timestampWithinHour(value: string | null, cutoff: number, now: number): number | null {
  if (!value) return null;
  const timestamp = new Date(value).getTime();
  if (!Number.isFinite(timestamp) || timestamp < cutoff || timestamp > now) return null;
  return timestamp;
}

function recentRestartAt(pod: Pod, cutoff: number, now: number): number | null {
  const containerRestartAt = pod.restartCount > 0
    ? timestampWithinHour(pod.lastRestartAt, cutoff, now)
    : null;
  if (containerRestartAt !== null) return containerRestartAt;

  // A controller restart recreates a pod with a new name and restartCount=0.
  // Treat a pod created during the last hour as one recent restart as well.
  return timestampWithinHour(pod.creationTimestamp, cutoff, now);
}

export function restartHistoryKey(clusterId: string, namespace: string, podName: string): string {
  return `${clusterId}/${namespace}/${podName}`;
}

export function loadRestartHistory(): RestartHistory {
  try {
    const stored = window.localStorage.getItem(STORAGE_KEY);
    if (!stored) return {};
    const parsed: unknown = JSON.parse(stored);
    if (!parsed || typeof parsed !== 'object') return {};
    return parsed as RestartHistory;
  } catch {
    return {};
  }
}

export function saveRestartHistory(history: RestartHistory): void {
  window.localStorage.setItem(STORAGE_KEY, JSON.stringify(history));
}

export interface PodRestartObservation {
  clusterId: string;
  namespace: string;
  pod: Pod;
}

export function updateRestartHistory(
  current: RestartHistory,
  observations: PodRestartObservation[],
  now = Date.now()
): RestartHistory {
  const cutoff = now - ONE_HOUR_MS;
  const next: RestartHistory = {};

  for (const [key, entry] of Object.entries(current)) {
    const events = entry.events.filter((event) => event.at >= cutoff);
    if (events.length > 0) {
      next[key] = { ...entry, events };
    }
  }

  for (const observation of observations) {
    const key = restartHistoryKey(observation.clusterId, observation.namespace, observation.pod.name);
    const existing = current[key];
    const currentTotal = observation.pod.restartCount;
    const events = (existing?.events ?? []).filter((event) => event.at >= cutoff);
    const latestRestartAt = recentRestartAt(observation.pod, cutoff, now);

    if (!existing) {
      // Kubernetes exposes only the latest container restart timestamp. A
      // controller-driven restart creates a new pod, so its recent creation time
      // is also recorded as one restart.
      next[key] = {
        observedTotal: currentTotal,
        lastObservedAt: now,
        events: latestRestartAt === null ? [] : [{ at: latestRestartAt, count: 1 }]
      };
      continue;
    }

    const observationGap = now - existing.lastObservedAt;
    if (currentTotal > existing.observedTotal && observationGap <= ONE_HOUR_MS) {
      events.push({
        at: latestRestartAt !== null && latestRestartAt > existing.lastObservedAt ? latestRestartAt : now,
        count: currentTotal - existing.observedTotal
      });
    } else if (currentTotal < existing.observedTotal || observationGap > ONE_HOUR_MS) {
      events.length = 0;
      if (latestRestartAt !== null) events.push({ at: latestRestartAt, count: 1 });
    }

    next[key] = { observedTotal: currentTotal, lastObservedAt: now, events };
  }

  return next;
}

export function restartCountLastHour(
  history: RestartHistory,
  clusterId: string,
  namespace: string,
  podName: string,
  now = Date.now()
): number {
  const entry = history[restartHistoryKey(clusterId, namespace, podName)];
  if (!entry) return 0;
  const cutoff = now - ONE_HOUR_MS;
  return entry.events.filter((event) => event.at >= cutoff).reduce((sum, event) => sum + event.count, 0);
}
