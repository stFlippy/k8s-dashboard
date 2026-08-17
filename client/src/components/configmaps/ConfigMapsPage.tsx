import { useEffect, useMemo, useRef, useState } from 'react';
import { useMutation, useQueries, useQueryClient } from '@tanstack/react-query';
import type { UseQueryResult } from '@tanstack/react-query';
import { getConfigMaps, replaceConfigMap } from '../../api/kubernetes';
import { useClusterNamespaces } from '../../hooks/useClusterNamespaces';
import type { ConfigMap } from '../../types/kubernetes';
import { formatDate } from '../../utils/formatDate';
import { ActionConfirmModal } from '../common/ActionConfirmModal';
import type { ActionConfirmRequest } from '../common/ActionConfirmModal';
import styles from './ConfigMapsPage.module.css';
import { CodeEditor } from '../common/CodeEditor';

interface Selected { clusterId: string; clusterName: string; configMap: ConfigMap; }
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

export function ConfigMapsPage() {
  const queryClient = useQueryClient();
  const { clustersLoading, clustersError, groups } = useClusterNamespaces();
  const filterAreaRef = useRef<HTMLDivElement | null>(null);
  const [selected, setSelected] = useState<Selected | null>(null);
  const [resourceText, setResourceText] = useState('');
  const [message, setMessage] = useState('');
  const [confirmRequest, setConfirmRequest] = useState<ActionConfirmRequest | null>(null);
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
  const replace = useMutation({
    mutationFn: async () => {
      if (!selected) throw new Error('ConfigMap не выбран.');
      let resource: unknown;
      try { resource = JSON.parse(resourceText); } catch { throw new Error('Редактор использует JSON-синтаксис. Не удалось распознать JSON.'); }
      return replaceConfigMap(selected.clusterId, selected.configMap.namespace, selected.configMap.name, resource);
    },
    onSuccess: async (updated) => {
      if (!selected) return;
      setSelected({ ...selected, configMap: updated });
      setResourceText(JSON.stringify(updated.resource, null, 2));
      setMessage('ConfigMap сохранён.');
      await queryClient.invalidateQueries({ queryKey: ['configmaps', selected.clusterId, selected.configMap.namespace] });
    }
  });

  const openConfigMap = (clusterId: string, clusterName: string, configMap: ConfigMap) => {
    setSelected({ clusterId, clusterName, configMap });
    setResourceText(JSON.stringify(configMap.resource, null, 2));
    setMessage('');
  };

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
          <tbody>{rows.map(({ target, configMap }) => <tr key={`${target.clusterId}/${configMap.namespace}/${configMap.name}`} onClick={() => openConfigMap(target.clusterId, target.clusterName, configMap)}><td><span className={`${styles.clusterBadge} ${styles[`tone${groups.findIndex((group) => group.cluster.id === target.clusterId) % 4}`]}`}>{target.clusterName}</span></td><td className={styles.name}>{configMap.name}</td><td>{configMap.namespace}</td><td>{formatDate(configMap.creationTimestamp)}</td></tr>)}</tbody>
        </table>
        {!rows.length && <div className={styles.empty}>Подходящие ConfigMaps не найдены.</div>}
      </div>

      {selected && <div className={styles.modalOverlay} onMouseDown={(event) => event.target === event.currentTarget && setSelected(null)}><div className={styles.detail} style={{ width: 'min(1320px, calc(100vw - 32px))', height: 'calc(100vh - var(--topbar-height) - 24px)', maxHeight: 'none', overflow: 'hidden', display: 'flex', flexDirection: 'column' }}><div className={styles.detailHeader}><div><h3>{selected.configMap.name}</h3><span>{selected.clusterName} / {selected.configMap.namespace}</span></div><div><button onClick={() => setConfirmRequest({ title: 'Сохранение ConfigMap', description: `Изменения ConfigMap ${selected.configMap.name} будут отправлены в кластер ${selected.clusterName}.`, confirmLabel: 'Сохранить ConfigMap', onConfirm: async () => { await replace.mutateAsync(); } })}>Сохранить</button><button className={styles.close} onClick={() => setSelected(null)} aria-label="Закрыть" title="Закрыть">×</button></div></div>{message && <div className={styles.message}>{message}</div>}<div style={{ display: 'flex', flex: 1, minHeight: 0, margin: 16 }}><CodeEditor value={resourceText} onChange={(value) => { setResourceText(value); setMessage(''); }} /></div></div></div>}
      <ActionConfirmModal request={confirmRequest} onClose={() => setConfirmRequest(null)} />
    </section>
  );
}
