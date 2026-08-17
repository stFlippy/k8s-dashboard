const key = 'quaber.cluster.aliases';

export function readClusterAliases(): Record<string, string> {
  try {
    const parsed = JSON.parse(window.localStorage.getItem(key) ?? '{}') as unknown;
    return parsed && typeof parsed === 'object' ? parsed as Record<string, string> : {};
  } catch {
    return {};
  }
}

export function clusterAlias(clusterId: string, clusterName: string, aliases = readClusterAliases()): string {
  return (aliases[clusterId]?.trim() || clusterName.trim()).slice(0, 5);
}

export function writeClusterAlias(clusterId: string, value: string): void {
  const aliases = readClusterAliases();
  const normalized = value.trim().slice(0, 5);
  if (normalized) aliases[clusterId] = normalized;
  else delete aliases[clusterId];
  window.localStorage.setItem(key, JSON.stringify(aliases));
  window.dispatchEvent(new CustomEvent('quaber-cluster-aliases'));
}
