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

function readStoredChoice(key: string): string | null {
  const raw = window.localStorage.getItem(key);
  if (!raw) return null;
  try {
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed === 'string') return parsed;
    return Array.isArray(parsed) && typeof parsed[0] === 'string' ? parsed[0] : null;
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
  const [selectedClusterName, setSelectedClusterName] = useState<string | null>(() => readStoredChoice(clusterStorageKey));
  const [selectedNamespaceName, setSelectedNamespaceName] = useState<string | null>(() => readStoredChoice(namespaceStorageKey));
  const activeGroup = groups.find((group) => group.cluster.name === selectedClusterName) ?? groups[0];
  const namespaceNames = useMemo(() => activeGroup?.namespaces.map((namespace) => namespace.name).sort() ?? [], [activeGroup]);
  const activeNamespace = selectedNamespaceName && namespaceNames.includes(selectedNamespaceName) ? selectedNamespaceName : namespaceNames[0];
  useEffect(() => {
    if (!activeGroup) return;
    if (selectedClusterName !== activeGroup.cluster.name) setSelectedClusterName(activeGroup.cluster.name);
    window.localStorage.setItem(clusterStorageKey, JSON.stringify(activeGroup.cluster.name));
  }, [activeGroup, selectedClusterName]);
  useEffect(() => {
    if (!activeNamespace) return;
    if (selectedNamespaceName !== activeNamespace) setSelectedNamespaceName(activeNamespace);
    window.localStorage.setItem(namespaceStorageKey, JSON.stringify(activeNamespace));
  }, [activeNamespace, selectedNamespaceName]);
  useEffect(() => {
    const closeOnOutside = (event: MouseEvent) => {
      if (openFilter && filterAreaRef.current && !filterAreaRef.current.contains(event.target as Node)) setOpenFilter(null);
    };
    document.addEventListener('mousedown', closeOnOutside);
    return () => document.removeEventListener('mousedown', closeOnOutside);
  }, [openFilter]);

  const targets = useMemo(() => activeGroup && activeNamespace ? [{ clusterId: activeGroup.cluster.id, clusterName: activeGroup.cluster.name, namespace: activeNamespace }] : [], [activeGroup, activeNamespace]);
  const queries = useQueries({ queries: targets.map((target) => ({ queryKey: ['configmaps', target.clusterId, target.namespace], queryFn: () => getConfigMaps(target.clusterId, target.namespace), refetchInterval: 10_000, refetchOnWindowFocus: true })) }) as UseQueryResult<ConfigMap[], Error>[];
  const visibleRequests = targets.map((target, index) => ({ target, query: queries[index] }));
  const readErrors = visibleRequests.filter(({ query }) => query?.isError);
  const reading = visibleRequests.some(({ query }) => query?.isPending);
  const stale = visibleRequests.some(({ query }) => query?.isError && query.data !== undefined);

  const rows = useMemo(() => targets
    .flatMap((target, index) => (queries[index]?.data ?? []).map((configMap) => ({ target, configMap })))
    .sort((left, right) => left.configMap.name.localeCompare(right.configMap.name)), [targets, queries]);

  const chooseCluster = (value: string) => { setSelectedClusterName(value); setSelectedNamespaceName(null); setSelected(null); setOpenFilter(null); };
  const chooseNamespace = (value: string) => { setSelectedNamespaceName(value); setSelected(null); setOpenFilter(null); };
  const replace = useMutation({
    mutationFn: (target: { clusterId: string; namespace: string; name: string; resource: unknown }) => replaceConfigMap(target.clusterId, target.namespace, target.name, target.resource),
    onSuccess: async (updated, target) => {
      await queryClient.invalidateQueries({ queryKey: ['configmaps', target.clusterId, target.namespace] });
      if (!selected || selected.clusterId !== target.clusterId || selected.configMap.namespace !== target.namespace || selected.configMap.name !== target.name) return;
      setSelected({ ...selected, configMap: updated });
      setResourceText(JSON.stringify(updated.resource, null, 2));
      setMessage('ConfigMap сохранён.');
    }
  });

  const openConfigMap = (clusterId: string, clusterName: string, configMap: ConfigMap) => {
    setConfirmRequest(null);
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
            <button className={styles.filterButton} onClick={() => setOpenFilter((current) => current === 'clusters' ? null : 'clusters')}>Кластер: {activeGroup?.cluster.name ?? '—'}<span className={styles.chevron}>▾</span></button>
            {openFilter === 'clusters' && <div className={styles.filterList}>{groups.map((group) => <label key={group.cluster.id}><input type="radio" name="configmap-cluster" checked={activeGroup?.cluster.id === group.cluster.id} onChange={() => chooseCluster(group.cluster.name)} /><span>{group.cluster.name}</span></label>)}</div>}
          </div>
          <div className={styles.filterDropdown}>
            <button className={styles.filterButton} onClick={() => setOpenFilter((current) => current === 'namespaces' ? null : 'namespaces')}>Namespace: {activeNamespace ?? '—'}<span className={styles.chevron}>▾</span></button>
            {openFilter === 'namespaces' && <div className={styles.filterList}>{namespaceNames.map((namespaceName) => <label key={namespaceName}><input type="radio" name="configmap-namespace" checked={activeNamespace === namespaceName} onChange={() => chooseNamespace(namespaceName)} /><span>{namespaceName}</span></label>)}</div>}
          </div>
        </div>
      </div>
      <div className={styles.content} onMouseDown={() => setOpenFilter(null)}>
        {activeGroup?.error && <div className={styles.error} role="alert">{activeGroup.cluster.name}: не удалось получить namespaces — {activeGroup.error.message}</div>}
        {readErrors.map(({ target, query }) => <div className={styles.error} key={`${target.clusterId}/${target.namespace}`} role="alert">{target.clusterName} / {target.namespace}: ошибка чтения ConfigMaps — {query.error?.message}{query.data !== undefined ? ' (показаны ранее загруженные данные)' : ''}</div>)}
        {reading && <div className={styles.state}>Загрузка ConfigMaps…</div>}
        {stale && <div className={styles.state}>Часть данных устарела из-за ошибки обновления.</div>}
        <table className={styles.table}>
          <thead><tr><th>Кластер</th><th>Имя</th><th>Namespace</th><th>Создан</th></tr></thead>
          <tbody>{rows.map(({ target, configMap }) => <tr key={`${target.clusterId}/${configMap.namespace}/${configMap.name}`} onClick={() => openConfigMap(target.clusterId, target.clusterName, configMap)}><td><span className={`${styles.clusterBadge} ${styles[`tone${groups.findIndex((group) => group.cluster.id === target.clusterId) % 4}`]}`}>{target.clusterName}</span></td><td className={styles.name}>{configMap.name}</td><td>{configMap.namespace}</td><td>{formatDate(configMap.creationTimestamp)}</td></tr>)}</tbody>
        </table>
        {!rows.length && !reading && !readErrors.length && !activeGroup?.error && <div className={styles.empty}>{activeNamespace ? 'ConfigMaps в выбранном namespace не найдены.' : 'Выберите кластер и namespace.'}</div>}
      </div>

      {selected && <div className={styles.modalOverlay} onMouseDown={(event) => event.target === event.currentTarget && setSelected(null)}><div className={styles.detail} style={{ width: 'min(1320px, calc(100vw - 32px))', height: 'calc(100vh - var(--topbar-height) - 24px)', maxHeight: 'none', overflow: 'hidden', display: 'flex', flexDirection: 'column' }}><div className={styles.detailHeader}><div className={styles.detailTitle}><h3>{selected.configMap.name}</h3><span>{selected.clusterName} / {selected.configMap.namespace}</span></div><div className={styles.detailActions}><button onClick={() => {
        let resource: unknown;
        try { resource = JSON.parse(resourceText); } catch { setMessage('Не удалось распознать JSON документа ConfigMap.'); return; }
        const target = { clusterId: selected.clusterId, namespace: selected.configMap.namespace, name: selected.configMap.name, resource };
        setConfirmRequest({ title: 'Сохранение ConfigMap', description: `Изменения ConfigMap ${target.name} будут отправлены в кластер ${selected.clusterName}, namespace ${target.namespace}. Это может повлиять на потребляющие его приложения; перезапуск Pods не гарантируется.`, confirmLabel: 'Сохранить ConfigMap', onConfirm: async () => { await replace.mutateAsync(target); } });
      }}>Сохранить</button><button className={styles.close} onClick={() => setSelected(null)} aria-label="Закрыть" title="Закрыть">×</button></div></div>{message && <div className={styles.message}>{message}</div>}<div style={{ display: 'flex', flex: 1, minHeight: 0, margin: 16 }}><CodeEditor value={resourceText} onChange={(value) => { setResourceText(value); setMessage(''); }} /></div></div></div>}
      <ActionConfirmModal request={confirmRequest} onClose={() => setConfirmRequest(null)} />
    </section>
  );
}
