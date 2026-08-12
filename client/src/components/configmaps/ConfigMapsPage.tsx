import { useEffect, useMemo, useRef, useState } from 'react';
import { useQueries } from '@tanstack/react-query';
import type { UseQueryResult } from '@tanstack/react-query';
import { getConfigMaps } from '../../api/kubernetes';
import { useClusterNamespaces } from '../../hooks/useClusterNamespaces';
import type { ConfigMap } from '../../types/kubernetes';
import { formatDate } from '../../utils/formatDate';
import styles from './ConfigMapsPage.module.css';

interface Selected { clusterName: string; configMap: ConfigMap; }
type OpenFilter = 'clusters' | 'namespaces' | null;

const clusterStorageKey = 'quaber.configmaps.selectedClusters';
const namespaceStorageKey = 'quaber.configmaps.selectedNamespaces';

function readStoredList(key: string): string[] | null {
  const raw = window.localStorage.getItem(key);
  if (!raw) return null;
  try {
    const parsed: unknown = JSON.parse(raw);
    return Array.isArray(parsed) && parsed.every((item) => typeof item === 'string') ? parsed : null;
  } catch {
    return null;
  }
}

function yamlScalar(value: string): string {
  if (value.includes('\n')) return `|\n${value.split('\n').map((line) => `    ${line}`).join('\n')}`;
  return JSON.stringify(value);
}

function toYaml(configMap: ConfigMap): string {
  const lines = ['apiVersion: v1', 'kind: ConfigMap', 'metadata:', `  name: ${configMap.name}`, `  namespace: ${configMap.namespace}`, 'data:'];
  const entries = Object.entries(configMap.data);
  if (!entries.length) lines.push('  {}');
  for (const [key, value] of entries) {
    if (value.includes('\n')) {
      lines.push(`  ${key}: |`);
      lines.push(...value.split('\n').map((line) => `    ${line}`));
    } else {
      lines.push(`  ${key}: ${yamlScalar(value)}`);
    }
  }
  return lines.join('\n');
}

export function ConfigMapsPage() {
  const { clustersLoading, clustersError, groups } = useClusterNamespaces();
  const filterAreaRef = useRef<HTMLDivElement | null>(null);
  const [selected, setSelected] = useState<Selected | null>(null);
  const [openFilter, setOpenFilter] = useState<OpenFilter>(null);
  const clusterNames = useMemo(() => groups.map((group) => group.cluster.name), [groups]);
  const namespaceNames = useMemo(() => [...new Set(groups.flatMap((group) => group.namespaces.map((namespace) => namespace.name)))].sort(), [groups]);
  const [selectedClusters, setSelectedClusters] = useState<string[] | null>(() => readStoredList(clusterStorageKey));
  const [selectedNamespaces, setSelectedNamespaces] = useState<string[] | null>(() => readStoredList(namespaceStorageKey));

  useEffect(() => {
    if (selectedClusters === null && clusterNames.length > 0) setSelectedClusters(clusterNames);
  }, [clusterNames, selectedClusters]);
  useEffect(() => {
    if (selectedNamespaces === null && namespaceNames.length > 0) setSelectedNamespaces(namespaceNames);
  }, [namespaceNames, selectedNamespaces]);
  useEffect(() => {
    if (selectedClusters === null) return;
    const filtered = selectedClusters.filter((clusterName) => clusterNames.includes(clusterName));
    if (filtered.length !== selectedClusters.length) setSelectedClusters(filtered);
  }, [clusterNames, selectedClusters]);
  useEffect(() => {
    if (selectedNamespaces === null) return;
    const filtered = selectedNamespaces.filter((namespaceName) => namespaceNames.includes(namespaceName));
    if (filtered.length !== selectedNamespaces.length) setSelectedNamespaces(filtered);
  }, [namespaceNames, selectedNamespaces]);
  useEffect(() => {
    if (selectedClusters !== null) window.localStorage.setItem(clusterStorageKey, JSON.stringify(selectedClusters));
  }, [selectedClusters]);
  useEffect(() => {
    if (selectedNamespaces !== null) window.localStorage.setItem(namespaceStorageKey, JSON.stringify(selectedNamespaces));
  }, [selectedNamespaces]);
  useEffect(() => {
    const closeOnOutside = (event: MouseEvent) => {
      if (openFilter && filterAreaRef.current && !filterAreaRef.current.contains(event.target as Node)) setOpenFilter(null);
    };
    document.addEventListener('mousedown', closeOnOutside);
    return () => document.removeEventListener('mousedown', closeOnOutside);
  }, [openFilter]);

  const activeClusters = selectedClusters ?? clusterNames;
  const activeNamespaces = selectedNamespaces ?? namespaceNames;
  const targets = useMemo(() => groups.flatMap((group) => group.namespaces.map((namespace) => ({ clusterId: group.cluster.id, clusterName: group.cluster.name, namespace: namespace.name }))), [groups]);
  const queries = useQueries({ queries: targets.map((target) => ({ queryKey: ['configmaps', target.clusterId, target.namespace], queryFn: () => getConfigMaps(target.clusterId, target.namespace), refetchInterval: 10_000, refetchOnWindowFocus: true })) }) as UseQueryResult<ConfigMap[], Error>[];

  const rows = useMemo(() => targets
    .flatMap((target, index) => (queries[index]?.data ?? []).map((configMap) => ({ target, configMap })))
    .filter(({ target }) => activeClusters.includes(target.clusterName) && activeNamespaces.includes(target.namespace))
    .sort((left, right) => left.target.clusterName.localeCompare(right.target.clusterName) || left.target.namespace.localeCompare(right.target.namespace) || left.configMap.name.localeCompare(right.configMap.name)), [targets, queries, activeClusters, activeNamespaces]);

  const toggleCluster = (value: string) => setSelectedClusters((current) => {
    const source = current ?? clusterNames;
    return source.includes(value) ? source.filter((item) => item !== value) : [...source, value];
  });
  const toggleNamespace = (value: string) => setSelectedNamespaces((current) => {
    const source = current ?? namespaceNames;
    return source.includes(value) ? source.filter((item) => item !== value) : [...source, value];
  });

  if (clustersLoading) return <div className={styles.state}>Загрузка кластеров…</div>;
  if (clustersError) return <div className={styles.error}>{clustersError.message}</div>;

  return (
    <section className={styles.root}>
      <div className={styles.header}>
        <div><h2>ConfigMaps</h2><p>Рабочие нагрузки → ConfigMaps</p></div>
        <div className={styles.filterRow} ref={filterAreaRef}>
          <div className={styles.filterDropdown}>
            <button className={styles.filterButton} onClick={() => setOpenFilter((current) => current === 'clusters' ? null : 'clusters')}>Кластеры ({activeClusters.length})<span className={styles.chevron}>▾</span></button>
            {openFilter === 'clusters' && <div className={styles.filterList}>{clusterNames.map((clusterName) => <label key={clusterName}><input type="checkbox" checked={activeClusters.includes(clusterName)} onChange={() => toggleCluster(clusterName)} /><span>{clusterName}</span></label>)}</div>}
          </div>
          <div className={styles.filterDropdown}>
            <button className={styles.filterButton} onClick={() => setOpenFilter((current) => current === 'namespaces' ? null : 'namespaces')}>Namespaces ({activeNamespaces.length})<span className={styles.chevron}>▾</span></button>
            {openFilter === 'namespaces' && <div className={styles.filterList}>{namespaceNames.map((namespaceName) => <label key={namespaceName}><input type="checkbox" checked={activeNamespaces.includes(namespaceName)} onChange={() => toggleNamespace(namespaceName)} /><span>{namespaceName}</span></label>)}</div>}
          </div>
        </div>
      </div>
      <div className={styles.content} onMouseDown={() => setOpenFilter(null)}>
        <table className={styles.table}>
          <thead><tr><th>Кластер</th><th>Имя</th><th>Namespace</th><th>Создан</th></tr></thead>
          <tbody>{rows.map(({ target, configMap }) => <tr key={`${target.clusterId}/${configMap.namespace}/${configMap.name}`} onClick={() => setSelected({ clusterName: target.clusterName, configMap })}><td><span className={`${styles.clusterBadge} ${styles[`tone${groups.findIndex((group) => group.cluster.id === target.clusterId) % 4}`]}`}>{target.clusterName}</span></td><td className={styles.name}>{configMap.name}</td><td>{configMap.namespace}</td><td>{formatDate(configMap.creationTimestamp)}</td></tr>)}</tbody>
        </table>
        {!rows.length && <div className={styles.empty}>Подходящие ConfigMaps не найдены.</div>}
      </div>

      {selected && <div className={styles.modalOverlay} onMouseDown={(event) => event.target === event.currentTarget && setSelected(null)}><div className={styles.detail}><div className={styles.detailHeader}><div><h3>{selected.configMap.name}</h3><span>{selected.clusterName} / {selected.configMap.namespace}</span></div><button onClick={() => setSelected(null)}>Закрыть</button></div><h4>Содержимое</h4><table className={styles.dataTable}><thead><tr><th>Ключ</th><th>Значение</th></tr></thead><tbody>{Object.entries(selected.configMap.data).map(([key, value]) => <tr key={key}><td>{key}</td><td><pre>{value}</pre></td></tr>)}</tbody></table><h4>YAML</h4><pre className={styles.yaml}>{toYaml(selected.configMap)}</pre></div></div>}
    </section>
  );
}
