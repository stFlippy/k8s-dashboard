import { useEffect, useMemo, useRef, useState } from 'react';
import type { CSSProperties, PointerEvent as ReactPointerEvent } from 'react';
import { useMutation, useQueries, useQueryClient } from '@tanstack/react-query';
import type { UseQueryResult } from '@tanstack/react-query';
import { getDeployments, getPods, removePod } from '../../api/kubernetes';
import { useClusterNamespaces } from '../../hooks/useClusterNamespaces';
import type { Deployment, Pod, PodRef, PodStatus, WorkloadRef } from '../../types/kubernetes';
import { ActionConfirmModal } from '../common/ActionConfirmModal';
import type { ActionConfirmRequest } from '../common/ActionConfirmModal';
import { formatDate } from '../../utils/formatDate';
import {
  loadRestartHistory,
  restartCountLastHour,
  saveRestartHistory,
  updateRestartHistory
} from '../../utils/restartHistory';
import type { PodRestartObservation, RestartHistory } from '../../utils/restartHistory';
import styles from './Matrix.module.css';

interface Props {
  onOpenDeployment: (workload: WorkloadRef) => void;
  onOpenPod: (pod: PodRef) => void;
}

interface Column {
  clusterId: string;
  clusterName: string;
  clusterIndex: number;
  namespace: string;
  lastInCluster: boolean;
}

interface MatrixTableStyle extends CSSProperties {
  '--deployment-column-width': string;
}

type FilterStatus = 'Running' | 'Pending' | 'Failed' | 'Unknown' | 'Terminated';
const filteredStatuses: FilterStatus[] = ['Running', 'Pending', 'Failed', 'Unknown', 'Terminated'];
const refreshOptions: Array<{ label: string; value: number | false }> = [
  { label: 'Выкл', value: false },
  { label: '5 сек', value: 5_000 },
  { label: '10 сек', value: 10_000 },
  { label: '30 сек', value: 30_000 },
  { label: '1 мин', value: 60_000 },
  { label: '5 мин', value: 5 * 60_000 },
  { label: '10 мин', value: 10 * 60_000 },
  { label: '30 мин', value: 30 * 60_000 },
  { label: '60 мин', value: 60 * 60_000 }
];

const easterEggQuery = 'йцукенqwerty';
const dancingCatFrames = [
  [' /\\_/\\', '( o.o )', ' > ^ <', '  /|\\', ' / | \\'].join('\n'),
  [' /\\_/\\', '( o.o )', ' > ^ <', '  /|\\ ', ' / |  '].join('\n'),
  [' /\\_/\\', '( ^.^ )', ' > ^ <', ' /|   ', '/ |\\  '].join('\n'),
  [' /\\_/\\', '( ^.^ )', ' < ^ >', '/|    ', ' |\\   '].join('\n'),
  [' /\\_/\\', '( o.o )', ' < ^ >', '   |\\', '  /| \\'].join('\n'),
  [' /\\_/\\', '( o.o )', ' < ^ >', '  \\|/ ', '   |\\ '].join('\n'),
  [' /\\_/\\', '( -.- )', ' > ^ <', ' \\|/  ', ' / |  '].join('\n'),
  [' /\\_/\\', '( -.- )', ' > ^ <', '  \\|/ ', '   | \\'].join('\n'),
  [' /\\_/\\', '( o.o )', ' < ^ >', '  \\|/', '   | \\'].join('\n'),
  [' /\\_/\\', '( ^o^ )', ' > ^ <', ' /|\\  ', '/ |   '].join('\n'),
  [' /\\_/\\', '( ^o^ )', ' > ^ <', ' /|\\  ', '  | \\ '].join('\n'),
  [' /\\_/\\', '( o.o )', ' < ^ >', '  /|\\ ', '   | \\'].join('\n')
] as const;

const storageKeys = {
  running: 'quaber.matrix.filter.running',
  pending: 'quaber.matrix.filter.pending',
  failed: 'quaber.matrix.filter.failed',
  unknown: 'quaber.matrix.filter.unknown',
  terminated: 'quaber.matrix.filter.terminated',
  problemOnly: 'quaber.matrix.filter.problemOnly',
  hideNamespaces: 'quaber.matrix.hideNamespaces',
  refresh: 'quaber.matrix.refresh'
} as const;

function readStoredBoolean(key: string, fallback: boolean): boolean {
  const value = window.localStorage.getItem(key);
  if (value === 'true') return true;
  if (value === 'false') return false;
  return fallback;
}

function readStoredRefresh(): number | false {
  const value = window.localStorage.getItem(storageKeys.refresh);
  if (value === 'off') return false;
  const parsed = Number(value);
  return refreshOptions.some((option) => option.value === parsed) ? parsed : 10_000;
}

function statusLabel(status: FilterStatus): string {
  if (status === 'Failed') return 'Error';
  return status;
}

function statusMatches(pod: Pod, filters: Record<FilterStatus, boolean>): boolean {
  if (pod.status === 'Succeeded') return filters.Terminated;
  if (pod.status === 'Running') return filters.Running;
  if (pod.status === 'Pending') return filters.Pending;
  if (pod.status === 'Failed') return filters.Failed;
  if (pod.status === 'Unknown') return filters.Unknown;
  if (pod.status === 'Terminated') return filters.Terminated;
  return false;
}

function matchesSelectedFilters(
  pod: Pod,
  recentRestarts: number,
  filters: Record<FilterStatus, boolean>,
  problemOnly: boolean
): boolean {
  if (problemOnly) {
    return pod.status === 'Pending'
      || pod.status === 'Failed'
      || pod.status === 'Unknown'
      || pod.status === 'Terminated'
      || pod.status === 'Succeeded'
      || recentRestarts > 0;
  }

  /*
   * Базовая логика оставлена отдельно намеренно для быстрого отката
   * экспериментального режима «Только проблемные».
   */
  return statusMatches(pod, filters);
}

function styleStatus(status: PodStatus): string {
  if (status === 'Failed') return 'failed';
  if (status === 'Succeeded') return 'terminated';
  return status.toLowerCase();
}

export function Matrix({ onOpenDeployment, onOpenPod }: Props) {
  const queryClient = useQueryClient();
  const { clustersLoading, clustersError, groups } = useClusterNamespaces();
  const [search, setSearch] = useState('');
  const [deploymentColumnWidth, setDeploymentColumnWidth] = useState(150);
  const [selectedDeployments, setSelectedDeployments] = useState<string[] | null>(null);
  const [catFrame, setCatFrame] = useState(0);
  const [hovered, setHovered] = useState<{ row: string; column: string } | null>(null);
  const [context, setContext] = useState<{ x: number; y: number; pod: Pod; column: Column; deploymentName: string } | null>(null);
  const [filters, setFilters] = useState<Record<FilterStatus, boolean>>(() => ({
    Running: readStoredBoolean(storageKeys.running, true),
    Pending: readStoredBoolean(storageKeys.pending, true),
    Failed: readStoredBoolean(storageKeys.failed, true),
    Unknown: readStoredBoolean(storageKeys.unknown, true),
    Terminated: readStoredBoolean(storageKeys.terminated, true)
  }));
  const [problemOnly, setProblemOnly] = useState(() => readStoredBoolean(storageKeys.problemOnly, false));
  const [hideFilteredNamespaces, setHideFilteredNamespaces] = useState(() => readStoredBoolean(storageKeys.hideNamespaces, true));
  const [refreshInterval, setRefreshInterval] = useState<number | false>(() => readStoredRefresh());
  const [confirmRequest, setConfirmRequest] = useState<ActionConfirmRequest | null>(null);
  const [restartHistory, setRestartHistory] = useState<RestartHistory>(() => loadRestartHistory());
  const deploymentFilterRef = useRef<HTMLDetailsElement>(null);
  const easterEggActive = search.trim().toLocaleLowerCase() === easterEggQuery;

  useEffect(() => {
    const closeOnOutsidePointer = (event: PointerEvent) => {
      const filter = deploymentFilterRef.current;
      if (filter?.open && event.target instanceof Node && !filter.contains(event.target)) {
        filter.open = false;
      }
    };
    document.addEventListener('pointerdown', closeOnOutsidePointer);
    return () => document.removeEventListener('pointerdown', closeOnOutsidePointer);
  }, []);

  useEffect(() => {
    if (!easterEggActive) {
      setCatFrame(0);
      return;
    }
    const timer = window.setInterval(() => {
      setCatFrame((current) => (current + 1) % dancingCatFrames.length);
    }, 220);
    return () => window.clearInterval(timer);
  }, [easterEggActive]);

  useEffect(() => {
    window.localStorage.setItem(storageKeys.running, String(filters.Running));
    window.localStorage.setItem(storageKeys.pending, String(filters.Pending));
    window.localStorage.setItem(storageKeys.failed, String(filters.Failed));
    window.localStorage.setItem(storageKeys.unknown, String(filters.Unknown));
    window.localStorage.setItem(storageKeys.terminated, String(filters.Terminated));
    window.localStorage.setItem(storageKeys.problemOnly, String(problemOnly));
    window.localStorage.setItem(storageKeys.hideNamespaces, String(hideFilteredNamespaces));
    window.localStorage.setItem(storageKeys.refresh, refreshInterval === false ? 'off' : String(refreshInterval));
  }, [filters, problemOnly, hideFilteredNamespaces, refreshInterval]);

  const readyGroups = groups.filter((group) => group.namespaces.length > 0);
  const allColumns: Column[] = useMemo(
    () => readyGroups.flatMap((group, clusterIndex) => group.namespaces.map((namespace, index) => ({
      clusterId: group.cluster.id,
      clusterName: group.cluster.name,
      clusterIndex,
      namespace: namespace.name,
      lastInCluster: index === group.namespaces.length - 1
    }))),
    [readyGroups]
  );

  const deploymentQueries = useQueries({
    queries: allColumns.map((column) => ({
      queryKey: ['deployments', column.clusterId, column.namespace],
      queryFn: () => getDeployments(column.clusterId, column.namespace),
      refetchInterval: refreshInterval,
      refetchOnWindowFocus: true
    }))
  }) as UseQueryResult<Deployment[], Error>[];

  const podQueries = useQueries({
    queries: allColumns.map((column) => ({
      queryKey: ['pods', column.clusterId, column.namespace],
      queryFn: () => getPods(column.clusterId, column.namespace),
      refetchInterval: refreshInterval,
      refetchOnWindowFocus: true
    }))
  }) as UseQueryResult<Pod[], Error>[];

  const deploymentNames = useMemo(() => [...new Set(
    deploymentQueries.flatMap((query) => (query.data ?? []).map((deployment) => deployment.name))
  )].sort((left, right) => left.localeCompare(right)), [deploymentQueries]);
  const matchingDeploymentNames = useMemo(() => {
    const term = search.trim().toLocaleLowerCase();
    return term ? deploymentNames.filter((name) => name.toLocaleLowerCase().includes(term)) : deploymentNames;
  }, [deploymentNames, search]);

  const restartObservations: PodRestartObservation[] = allColumns.flatMap((column, index) => (podQueries[index]?.data ?? []).map((pod) => ({
    clusterId: column.clusterId,
    namespace: column.namespace,
    pod
  })));
  const restartObservationVersion = podQueries.map((query) => query.dataUpdatedAt).join('|');
  const restartColumnVersion = allColumns.map((column) => `${column.clusterId}/${column.namespace}`).join('|');

  useEffect(() => {
    setRestartHistory((current) => {
      const next = updateRestartHistory(current, restartObservations);
      if (JSON.stringify(current) === JSON.stringify(next)) return current;
      saveRestartHistory(next);
      return next;
    });
  }, [restartObservationVersion, restartColumnVersion]);

  const recentCountFor = (pod: Pod, column: Column): number => restartCountLastHour(
    restartHistory,
    column.clusterId,
    column.namespace,
    pod.name
  );

  const visibleColumns = useMemo(() => {
    if (!hideFilteredNamespaces) return allColumns;
    return allColumns.filter((column, index) => {
      const pods = podQueries[index]?.data ?? [];
      return pods.some((pod) => matchesSelectedFilters(
        pod,
        restartCountLastHour(restartHistory, column.clusterId, column.namespace, pod.name),
        filters,
        problemOnly
      ));
    });
  }, [allColumns, filters, hideFilteredNamespaces, podQueries, problemOnly, restartHistory]);

  const rows = useMemo(() => {
    const rowMap = new Map<string, Map<string, Pod[]>>();
    visibleColumns.forEach((column) => {
      const sourceIndex = allColumns.findIndex((item) => item.clusterId === column.clusterId && item.namespace === column.namespace);
      const deployments = deploymentQueries[sourceIndex]?.data ?? [];
      const pods = podQueries[sourceIndex]?.data ?? [];
      for (const deployment of deployments) {
        if (!rowMap.has(deployment.name)) rowMap.set(deployment.name, new Map());
        rowMap.get(deployment.name)?.set(
          `${column.clusterId}/${column.namespace}`,
          pods.filter((pod) => pod.name.startsWith(`${deployment.name}-`))
        );
      }
    });

    return [...rowMap.entries()]
      .map(([deploymentName, cells]) => ({
        deploymentName,
        cells,
        hasVisiblePods: visibleColumns.some((column) => {
          const pods = cells.get(`${column.clusterId}/${column.namespace}`) ?? [];
          return pods.some((pod) => matchesSelectedFilters(
            pod,
            restartCountLastHour(restartHistory, column.clusterId, column.namespace, pod.name),
            filters,
            problemOnly
          ));
        })
      }))
      .filter((row) => row.hasVisiblePods)
      .filter((row) => selectedDeployments === null || selectedDeployments.includes(row.deploymentName))
      .sort((left, right) => left.deploymentName.localeCompare(right.deploymentName));
  }, [allColumns, deploymentQueries, filters, podQueries, problemOnly, restartHistory, selectedDeployments, visibleColumns]);

  const totals = useMemo(() => {
    const counts: Record<PodStatus, number> = {
      Running: 0,
      Pending: 0,
      Failed: 0,
      Succeeded: 0,
      Unknown: 0,
      Terminated: 0
    };
    for (const query of podQueries) {
      for (const pod of query.data ?? []) counts[pod.status] += 1;
    }
    return counts;
  }, [podQueries]);

  const recentRestartTotal = useMemo(
    () => allColumns.reduce((total, column, index) => total + (podQueries[index]?.data ?? []).reduce(
      (sum, pod) => sum + restartCountLastHour(restartHistory, column.clusterId, column.namespace, pod.name),
      0
    ), 0),
    [allColumns, podQueries, restartHistory]
  );
  const lastUpdatedAt = Math.max(
    0,
    ...deploymentQueries.map((query) => query.dataUpdatedAt),
    ...podQueries.map((query) => query.dataUpdatedAt)
  );
  const lastUpdatedLabel = lastUpdatedAt > 0
    ? new Date(lastUpdatedAt).toLocaleTimeString('ru-RU', {
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit'
    })
    : '—';

  const deleteMutation = useMutation({
    mutationFn: ({ pod, column }: { pod: Pod; column: Column }) => removePod(column.clusterId, column.namespace, pod.name),
    onSuccess: async (_data, vars) => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['pods', vars.column.clusterId, vars.column.namespace] }),
        queryClient.invalidateQueries({ queryKey: ['deployments', vars.column.clusterId, vars.column.namespace] })
      ]);
    }
  });

  const namespaceErrors = groups.filter((group) => group.error);
  if (clustersLoading) return <div className={styles.state}>Загрузка кластеров Kubernetes…</div>;
  if (clustersError) return <div className={styles.stateError}>Не удалось загрузить кластеры: {clustersError.message}</div>;

  const openDeployment = (column: Column, deploymentName: string) => onOpenDeployment({
    clusterId: column.clusterId,
    clusterName: column.clusterName,
    namespace: column.namespace,
    deploymentName
  });

  const podRef = (pod: Pod, column: Column, deploymentName: string, initialTab?: PodRef['initialTab']): PodRef => ({
    clusterId: column.clusterId,
    clusterName: column.clusterName,
    namespace: column.namespace,
    podName: pod.name,
    deploymentName,
    initialTab
  });

  const refreshNow = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ['deployments'] }),
      queryClient.invalidateQueries({ queryKey: ['pods'] }),
      queryClient.invalidateQueries({ queryKey: ['namespaces'] })
    ]);
  };

  const rowsByCluster = readyGroups.map((group) => ({
    group,
    columns: visibleColumns.filter((column) => column.clusterId === group.cluster.id)
  })).filter((item) => item.columns.length > 0);

  const totalPods = totals.Running + totals.Pending + totals.Failed + totals.Unknown + totals.Terminated + totals.Succeeded;

  const startDeploymentColumnResize = (event: ReactPointerEvent<HTMLSpanElement>) => {
    event.preventDefault();
    event.stopPropagation();
    const startX = event.clientX;
    const startWidth = deploymentColumnWidth;
    const resize = (moveEvent: PointerEvent) => {
      setDeploymentColumnWidth(Math.max(110, Math.min(420, startWidth + moveEvent.clientX - startX)));
    };
    const stop = () => {
      document.removeEventListener('pointermove', resize);
      document.removeEventListener('pointerup', stop);
    };
    document.addEventListener('pointermove', resize);
    document.addEventListener('pointerup', stop, { once: true });
  };

  if (easterEggActive) {
    return (
      <section className={styles.easterEgg} aria-label="Танцующий ASCII-кот">
        <pre key={catFrame}>{dancingCatFrames[catFrame]}</pre>
      </section>
    );
  }

  return (
    <section className={styles.root} onClick={() => setContext(null)}>
      <div className={styles.stickyPanel}>
        <div className={styles.controls}>
          <div className={styles.filters}>
            {filteredStatuses.map((status) => (
              <label className={styles.filter} key={status}>
                <input
                  type="checkbox"
                  checked={filters[status]}
                  disabled={problemOnly}
                  onChange={(event) => setFilters((current) => ({ ...current, [status]: event.target.checked }))}
                />
                <span>{statusLabel(status)}</span>
                <b>{status === 'Terminated' ? totals.Terminated + totals.Succeeded : totals[status]}</b>
              </label>
            ))}
            <label className={`${styles.filterAlt} ${problemOnly ? styles.problemActive : ''}`}>
              <input type="checkbox" checked={problemOnly} onChange={(event) => setProblemOnly(event.target.checked)} />
              <span>Только проблемные</span>
            </label>
            <label className={styles.filterAlt}>
              <input type="checkbox" checked={hideFilteredNamespaces} onChange={(event) => setHideFilteredNamespaces(event.target.checked)} />
              <span>Скрывать namespace без выбранных pod</span>
            </label>
            <div className={`${styles.summaryCounter} ${styles.recentRestartStat}`}><b>{recentRestartTotal}</b><span>Restarts &lt; 1h</span></div>
            <div className={styles.summaryCounter}><b>{totalPods}</b><span>Total</span></div>
          </div>
        </div>
        <div className={styles.toolbar}>
          <details ref={deploymentFilterRef} className={styles.deploymentFilter}>
            <summary>
              <span>Deployment</span>
              <b>{selectedDeployments === null ? 'Все' : `${selectedDeployments.length}/${deploymentNames.length}`}</b>
            </summary>
            <div className={styles.deploymentFilterMenu}>
              <input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Найти deployment…" />
              <div className={styles.deploymentFilterActions}>
                <button type="button" onClick={() => setSelectedDeployments(null)}>Выбрать все</button>
                <button type="button" onClick={() => setSelectedDeployments([])}>Снять все</button>
              </div>
              <div className={styles.deploymentFilterOptions}>
                {matchingDeploymentNames.map((name) => {
                  const checked = selectedDeployments === null || selectedDeployments.includes(name);
                  return (
                    <label key={name}>
                      <input
                        type="checkbox"
                        checked={checked}
                        onChange={(event) => setSelectedDeployments((current) => {
                          const selected = current === null ? deploymentNames : current;
                          return event.target.checked
                            ? [...new Set([...selected, name])]
                            : selected.filter((item) => item !== name);
                        })}
                      />
                      <span>{name}</span>
                    </label>
                  );
                })}
                {matchingDeploymentNames.length === 0 && <div className={styles.deploymentFilterEmpty}>Ничего не найдено</div>}
              </div>
            </div>
          </details>
          <div className={styles.rightControls}>
            <div className={styles.lastUpdated}>Последнее обновление: <b>{lastUpdatedLabel}</b></div>
            <button className={styles.controlButton} onClick={() => { void refreshNow(); }}>Обновить</button>
            <label className={styles.refreshLabel}>
              <span>Автообновление</span>
              <select
                value={refreshInterval === false ? 'false' : String(refreshInterval)}
                onChange={(event) => {
                  if (event.target.value === 'false') {
                    setRefreshInterval(false);
                    return;
                  }
                  const next = Number(event.target.value);
                  setRefreshInterval(refreshOptions.some((option) => option.value === next) ? next : 10_000);
                }}
              >
                {refreshOptions.map((option) => <option key={String(option.value)} value={String(option.value)}>{option.label}</option>)}
              </select>
            </label>
          </div>
        </div>
        {namespaceErrors.length > 0 && <div className={styles.inlineError}>{namespaceErrors.map((group) => `${group.cluster.name}: ${group.error?.message}`).join(' · ')}</div>}
      </div>

      <div className={styles.tableWrap}>
        <table className={styles.table} style={{
          width: deploymentColumnWidth + visibleColumns.length * 24,
          '--deployment-column-width': `${deploymentColumnWidth}px`
        } as MatrixTableStyle}>
          <colgroup>
            <col style={{ width: deploymentColumnWidth }} />
            {visibleColumns.map((column) => <col key={`col-${column.clusterId}/${column.namespace}`} style={{ width: 24 }} />)}
          </colgroup>
          <thead>
            <tr className={styles.clusterRow}>
              <th className={styles.deploymentHeader} rowSpan={2}>
                <span>Deployment</span>
                <i className={styles.deploymentResizer} onPointerDown={startDeploymentColumnResize} title="Изменить ширину колонки" />
              </th>
              {rowsByCluster.map(({ group, columns }, index) => (
                <th key={group.cluster.id} colSpan={columns.length} className={`${styles.clusterHeader} ${styles[`clusterTone${index % 4}`]}`} title={group.cluster.server ?? group.cluster.context ?? group.cluster.name}>{group.cluster.name}</th>
              ))}
            </tr>
            <tr className={styles.namespaceRow}>
              {visibleColumns.map((column) => (
                <th key={`${column.clusterId}/${column.namespace}`} className={`${styles.namespaceHeader} ${styles[`clusterTone${column.clusterIndex % 4}`]} ${column.lastInCluster ? styles.clusterBoundary : ''}`} title={`${column.clusterName} / ${column.namespace}`}><span>{column.namespace}</span></th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              const rowHovered = hovered?.row === row.deploymentName;
              const firstColumn = visibleColumns.find((column) => row.cells.has(`${column.clusterId}/${column.namespace}`));
              return (
                <tr key={row.deploymentName} className={rowHovered ? styles.highlightRow : ''}>
                  <th title={row.deploymentName} onClick={() => firstColumn && openDeployment(firstColumn, row.deploymentName)}>{row.deploymentName}</th>
                  {visibleColumns.map((column) => {
                    const columnKey = `${column.clusterId}/${column.namespace}`;
                    const exists = row.cells.has(columnKey);
                    const pods = row.cells.get(columnKey) ?? [];
                    const visiblePods = pods.filter((pod) => matchesSelectedFilters(
                      pod,
                      recentCountFor(pod, column),
                      filters,
                      problemOnly
                    ));
                    const columnHovered = hovered?.column === columnKey;
                    const intersection = rowHovered && columnHovered;
                    return (
                      <td
                        key={`${row.deploymentName}-${columnKey}`}
                        className={`${columnHovered ? styles.highlightColumn : ''} ${intersection ? styles.highlightIntersection : ''} ${column.lastInCluster ? styles.clusterBoundary : ''} ${exists ? styles.hasDeployment : ''}`}
                        onMouseEnter={() => setHovered({ row: row.deploymentName, column: columnKey })}
                        onMouseLeave={() => setHovered(null)}
                        onClick={() => exists && openDeployment(column, row.deploymentName)}
                        title={`${column.clusterName} / ${column.namespace}`}
                      >
                        {visiblePods.length === 0 ? <span className={styles.empty}>—</span> : (
                          <div className={styles.pods}>
                            {visiblePods.map((pod) => {
                              const restartsLastHour = recentCountFor(pod, column);
                              const restartInfo = restartsLastHour > 0
                                ? `\nРестартов за последний час: ${restartsLastHour}\nПоследний известный рестарт: ${formatDate(pod.lastRestartAt)}`
                                : '\nРестартов за последний час: 0';
                              return (
                                <span
                                  className={`${styles.pod} ${styles[styleStatus(pod.status)] ?? styles.unknown} ${restartsLastHour > 0 ? styles.recentRestart : ''}`}
                                  title={`${pod.name}\n${column.clusterName} / ${column.namespace}${restartInfo}`}
                                  key={pod.name}
                                  onClick={(event) => { event.stopPropagation(); onOpenPod(podRef(pod, column, row.deploymentName)); }}
                                  onContextMenu={(event) => { event.preventDefault(); event.stopPropagation(); setContext({ x: event.clientX, y: event.clientY, pod, column, deploymentName: row.deploymentName }); }}
                                >
                                  {restartsLastHour > 0 ? restartsLastHour : ''}
                                </span>
                              );
                            })}
                          </div>
                        )}
                      </td>
                    );
                  })}
                </tr>
              );
            })}
          </tbody>
        </table>
        {visibleColumns.length === 0 && !groups.some((group) => group.isLoading) && <div className={styles.noResults}>Нет namespace, подходящих под выбранные фильтры</div>}
        {rows.length === 0 && visibleColumns.length > 0 && <div className={styles.noResults}>Ничего не найдено</div>}
      </div>

      {context && (
        <div className={styles.contextMenu} style={{ left: context.x, top: context.y }} onClick={(event) => event.stopPropagation()}>
          <button onClick={() => { onOpenPod(podRef(context.pod, context.column, context.deploymentName, 'describe')); setContext(null); }}>Открыть карточку pod</button>
          <button onClick={() => { onOpenPod(podRef(context.pod, context.column, context.deploymentName, 'logs')); setContext(null); }}>Открыть логи</button>
          <button onClick={() => { onOpenPod(podRef(context.pod, context.column, context.deploymentName, 'exec')); setContext(null); }}>Открыть консоль пода</button>
          <hr />
          <button onClick={() => {
            setConfirmRequest({
              title: 'Перезапуск pod',
              description: `Pod ${context.pod.name} будет удалён, после чего контроллер создаст новый экземпляр.`,
              confirmLabel: 'Перезапустить',
              onConfirm: async () => { await deleteMutation.mutateAsync({ pod: context.pod, column: context.column }); }
            });
            setContext(null);
          }}>Перезапустить pod</button>
          <button className={styles.contextDanger} onClick={() => {
            setConfirmRequest({
              title: 'Удаление pod',
              description: `Pod ${context.pod.name} будет удалён из namespace ${context.column.namespace}.`,
              confirmLabel: 'Удалить',
              onConfirm: async () => { await deleteMutation.mutateAsync({ pod: context.pod, column: context.column }); }
            });
            setContext(null);
          }}>Удалить pod</button>
        </div>
      )}

      <ActionConfirmModal request={confirmRequest} onClose={() => setConfirmRequest(null)} />
    </section>
  );
}
