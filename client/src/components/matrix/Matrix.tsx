import { useEffect, useMemo, useRef, useState } from 'react';
import type { ChangeEvent, CSSProperties, DragEvent, PointerEvent as ReactPointerEvent } from 'react';
import { useMutation, useQueries, useQueryClient } from '@tanstack/react-query';
import type { UseQueryResult } from '@tanstack/react-query';
import { getDeployments, getPods, removePod } from '../../api/kubernetes';
import { useClusterNamespaces } from '../../hooks/useClusterNamespaces';
import type { Deployment, Pod, PodRef, PodStatus, WorkloadRef } from '../../types/kubernetes';
import { ActionConfirmModal } from '../common/ActionConfirmModal';
import type { ActionConfirmRequest } from '../common/ActionConfirmModal';
import styles from './Matrix.module.css';
import { clusterAlias, readClusterAliases } from '../../utils/clusterAliases';
import { createLocalId } from '../../utils/localId';

interface Props {
  onOpenDeployment: (workload: WorkloadRef) => void;
  onOpenPod: (pod: PodRef) => void;
  onClustersUnauthorized?: (clusterIds: string[]) => void;
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
  '--namespace-header-height': string;
}

interface MatrixView {
  id: string;
  name: string;
  filters: Record<FilterStatus, boolean>;
  recentRestartFilter: boolean;
  problemOnly: boolean;
  hideFilteredNamespaces: boolean;
  selectedDeployments: string[] | null;
  selectedNamespaces: string[] | null;
  selectedClusterIds: string[] | null;
  namespaceOrder: string[];
  deploymentOrder: string[];
  color?: string;
}

type FilterStatus = 'Running' | 'Pending' | 'Failed' | 'Unknown';
const filteredStatuses: FilterStatus[] = ['Running', 'Pending', 'Failed', 'Unknown'];
const refreshOptions: Array<{ label: string; value: number | false }> = [
  { label: 'Выкл', value: false },
  { label: '5 сек', value: 5_000 },
  { label: '10 сек', value: 10_000 },
  { label: '30 сек', value: 30_000 },
  { label: '1 мин', value: 60_000 },
  { label: '5 мин', value: 5 * 60_000 },
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
  recentRestart: 'quaber.matrix.filter.recentRestart',
  problemOnly: 'quaber.matrix.filter.problemOnly',
  hideNamespaces: 'quaber.matrix.hideNamespaces',
  refresh: 'quaber.matrix.refresh'
  ,views: 'quaber.matrix.views'
  ,activeView: 'quaber.matrix.activeView'
} as const;

function defaultView(): MatrixView {
  return { id: 'default', name: 'Основная', filters: { Running: true, Pending: true, Failed: true, Unknown: true }, recentRestartFilter: true, problemOnly: false, hideFilteredNamespaces: true, selectedDeployments: null, selectedNamespaces: null, selectedClusterIds: null, namespaceOrder: [], deploymentOrder: [] };
}

function readViews(): MatrixView[] {
  try {
    const value = JSON.parse(window.localStorage.getItem(storageKeys.views) ?? '[]') as unknown;
    return Array.isArray(value) && value.length ? value as MatrixView[] : [defaultView()];
  } catch { return [defaultView()]; }
}

function readActiveView(): MatrixView | null {
  const views = readViews();
  const id = window.localStorage.getItem(storageKeys.activeView);
  return (id && views.find((view) => view.id === id)) || (views[0]?.id !== 'default' ? views[0] : null) || null;
}

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
  return Number.isFinite(parsed) && parsed >= 5_000 && parsed <= 24 * 60 * 60_000 ? parsed : 10_000;
}

function statusLabel(status: FilterStatus): string {
  if (status === 'Failed') return 'Error';
  return status;
}

function statusMatches(pod: Pod, filters: Record<FilterStatus, boolean>): boolean {
  if (pod.status === 'Running') return filters.Running;
  if (pod.status === 'Pending') return filters.Pending;
  if (pod.status === 'Failed') return filters.Failed;
  if (pod.status === 'Unknown') return filters.Unknown;
  return false;
}

function matchesSelectedFilters(
  pod: Pod,
  filters: Record<FilterStatus, boolean>,
  recentRestartFilter: boolean,
  problemOnly: boolean
): boolean {
  const restartedRecently = recentTime(pod.lastRestartAt) !== null;
  if (problemOnly) {
    return pod.status === 'Pending'
      || pod.status === 'Failed'
      || pod.status === 'Unknown'
      || restartedRecently;
  }

  /*
   * Базовая логика оставлена отдельно намеренно для быстрого отката
   * экспериментального режима «Только проблемные».
   */
  return statusMatches(pod, filters) || (recentRestartFilter && restartedRecently);
}

function styleStatus(status: PodStatus): string {
  if (status === 'Failed') return 'failed';
  return status.toLowerCase();
}

function recentTime(value: string | null): string | null {
  if (!value) return null;
  const timestamp = new Date(value).getTime();
  const now = Date.now();
  if (!Number.isFinite(timestamp) || timestamp < now - 60 * 60_000 || timestamp > now) return null;
  return new Date(timestamp).toLocaleTimeString('ru-RU', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit'
  });
}

export function Matrix({ onOpenDeployment, onOpenPod, onClustersUnauthorized }: Props) {
  const queryClient = useQueryClient();
  const { clustersLoading, clustersError, groups } = useClusterNamespaces();
  const [search, setSearch] = useState('');
  const [deploymentColumnWidth, setDeploymentColumnWidth] = useState(150);
  const [namespaceHeaderHeight, setNamespaceHeaderHeight] = useState(160);
  const [selectedDeployments, setSelectedDeployments] = useState<string[] | null>(() => readActiveView()?.selectedDeployments ?? null);
  const [selectedNamespaces, setSelectedNamespaces] = useState<string[] | null>(() => readActiveView()?.selectedNamespaces ?? null);
  const [selectedClusters, setSelectedClusters] = useState<string[] | null>(() => readActiveView()?.selectedClusterIds ?? null);
  const [namespaceOrder, setNamespaceOrder] = useState<string[]>(() => readActiveView()?.namespaceOrder ?? []);
  const [deploymentOrder, setDeploymentOrder] = useState<string[]>(() => readActiveView()?.deploymentOrder ?? []);
  const [views, setViews] = useState<MatrixView[]>(() => readViews());
  const [activeViewId, setActiveViewId] = useState(() => readActiveView()?.id ?? 'default');
  const [viewMessage, setViewMessage] = useState('');
  const [tabMenu, setTabMenu] = useState<{ x: number; y: number; viewId: string } | null>(null);
  const [dragTarget, setDragTarget] = useState<{ type: 'namespace' | 'deployment'; target: string; side: 'before' | 'after' } | null>(null);
  const [draggedViewId, setDraggedViewId] = useState<string | null>(null);
  const [aliases, setAliases] = useState<Record<string, string>>(() => readClusterAliases());
  const [catFrame, setCatFrame] = useState(0);
  const [hovered, setHovered] = useState<{ row: string; column: string } | null>(null);
  const [context, setContext] = useState<{ x: number; y: number; pod: Pod; column: Column; deploymentName: string } | null>(null);
  const [filters, setFilters] = useState<Record<FilterStatus, boolean>>(() => readActiveView()?.filters ?? ({
    Running: readStoredBoolean(storageKeys.running, true),
    Pending: readStoredBoolean(storageKeys.pending, true),
    Failed: readStoredBoolean(storageKeys.failed, true),
    Unknown: readStoredBoolean(storageKeys.unknown, true)
  }));
  const [recentRestartFilter, setRecentRestartFilter] = useState(() => readActiveView()?.recentRestartFilter ?? readStoredBoolean(storageKeys.recentRestart, true));
  const [problemOnly, setProblemOnly] = useState(() => readActiveView()?.problemOnly ?? readStoredBoolean(storageKeys.problemOnly, false));
  const [hideFilteredNamespaces, setHideFilteredNamespaces] = useState(() => readActiveView()?.hideFilteredNamespaces ?? readStoredBoolean(storageKeys.hideNamespaces, true));
  const [refreshInterval, setRefreshInterval] = useState<number | false>(() => readStoredRefresh());
  const [customIntervalActive, setCustomIntervalActive] = useState(() => {
    const value = readStoredRefresh(); return value !== false && !refreshOptions.some((option) => option.value === value);
  });
  const [customMinutes, setCustomMinutes] = useState(() => {
    const value = readStoredRefresh(); return value !== false && !refreshOptions.some((option) => option.value === value) ? String(Math.round(value / 60_000)) : '';
  });
  const [confirmRequest, setConfirmRequest] = useState<ActionConfirmRequest | null>(null);
  const [dismissedNamespaceError, setDismissedNamespaceError] = useState('');
  const [deploymentChoices, setDeploymentChoices] = useState<{ name: string; columns: Column[] } | null>(null);
  const [openToolbarFilter, setOpenToolbarFilter] = useState<'deployments' | 'namespaces' | 'clusters' | null>(null);
  const [viewEditor, setViewEditor] = useState<{ mode: 'create' | 'rename'; viewId?: string; value: string; error: string } | null>(null);
  const toolbarFiltersRef = useRef<HTMLDivElement>(null);
  const importRef = useRef<HTMLInputElement>(null);
  const resizeGuardUntil = useRef(0);
  const easterEggActive = search.trim().toLocaleLowerCase() === easterEggQuery;

  useEffect(() => {
    const closeOnOutsidePointer = (event: PointerEvent) => {
      const filtersArea = toolbarFiltersRef.current;
      if (filtersArea && event.target instanceof Node && !filtersArea.contains(event.target)) {
        setOpenToolbarFilter(null);
      }
    };
    document.addEventListener('pointerdown', closeOnOutsidePointer);
    return () => document.removeEventListener('pointerdown', closeOnOutsidePointer);
  }, []);

  useEffect(() => {
    const updateAliases = () => setAliases(readClusterAliases());
    window.addEventListener('storage', updateAliases);
    window.addEventListener('quaber-cluster-aliases', updateAliases);
    return () => { window.removeEventListener('storage', updateAliases); window.removeEventListener('quaber-cluster-aliases', updateAliases); };
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
    window.localStorage.setItem(storageKeys.recentRestart, String(recentRestartFilter));
    window.localStorage.setItem(storageKeys.problemOnly, String(problemOnly));
    window.localStorage.setItem(storageKeys.hideNamespaces, String(hideFilteredNamespaces));
    window.localStorage.setItem(storageKeys.refresh, refreshInterval === false ? 'off' : String(refreshInterval));
  }, [filters, recentRestartFilter, problemOnly, hideFilteredNamespaces, refreshInterval]);

  useEffect(() => {
    setViews((current) => current.map((view) => view.id === activeViewId ? { ...view, filters, recentRestartFilter, problemOnly, hideFilteredNamespaces, selectedDeployments, selectedNamespaces, selectedClusterIds: selectedClusters, namespaceOrder, deploymentOrder } : view));
  }, [activeViewId, deploymentOrder, filters, hideFilteredNamespaces, namespaceOrder, problemOnly, recentRestartFilter, selectedClusters, selectedDeployments, selectedNamespaces]);

  useEffect(() => { window.localStorage.setItem(storageKeys.views, JSON.stringify(views)); }, [views]);
  useEffect(() => { window.localStorage.setItem(storageKeys.activeView, activeViewId); }, [activeViewId]);

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
  const namespaceNames = useMemo(() => [...new Set(allColumns.map((column) => column.namespace))].sort(), [allColumns]);
  const clusterNames = useMemo(() => [...new Map(allColumns.map((column) => [column.clusterId, column.clusterName])).entries()].sort((a, b) => a[1].localeCompare(b[1])), [allColumns]);

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
  const selectedDeploymentCount = selectedDeployments === null ? null : selectedDeployments.filter((name) => deploymentNames.includes(name)).length;
  const selectedNamespaceCount = selectedNamespaces === null ? null : selectedNamespaces.filter((name) => namespaceNames.includes(name)).length;
  const availableClusterIds = useMemo(() => new Set(clusterNames.map(([id]) => id)), [clusterNames]);
  const selectedClusterCount = selectedClusters === null ? null : selectedClusters.filter((id) => availableClusterIds.has(id)).length;

  const visibleColumns = useMemo(() => {
    const scopedColumns = allColumns.filter((column) =>
      (selectedNamespaces === null || selectedNamespaces.includes(column.namespace))
      && (selectedClusters === null || selectedClusters.includes(column.clusterId))
    );
    const filtered = !hideFilteredNamespaces ? scopedColumns : scopedColumns.filter((column) => {
      const index = allColumns.findIndex((item) => item.clusterId === column.clusterId && item.namespace === column.namespace);
      const pods = podQueries[index]?.data ?? [];
      return pods.some((pod) => matchesSelectedFilters(
        pod,
        filters,
        recentRestartFilter,
        problemOnly
      ));
    });
    return [...filtered].sort((left, right) => {
      const leftKey = `${left.clusterId}/${left.namespace}`; const rightKey = `${right.clusterId}/${right.namespace}`;
      const li = namespaceOrder.indexOf(leftKey); const ri = namespaceOrder.indexOf(rightKey);
      if (li < 0 && ri < 0) return leftKey.localeCompare(rightKey);
      if (li < 0) return 1; if (ri < 0) return -1; return li - ri;
    });
  }, [allColumns, filters, hideFilteredNamespaces, namespaceOrder, podQueries, problemOnly, recentRestartFilter, selectedClusters, selectedNamespaces]);

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
            filters,
            recentRestartFilter,
            problemOnly
          ));
        })
      }))
      .filter((row) => row.hasVisiblePods)
      .filter((row) => selectedDeployments === null || selectedDeployments.includes(row.deploymentName))
      .sort((left, right) => { const li = deploymentOrder.indexOf(left.deploymentName); const ri = deploymentOrder.indexOf(right.deploymentName); if (li < 0 && ri < 0) return left.deploymentName.localeCompare(right.deploymentName); if (li < 0) return 1; if (ri < 0) return -1; return li - ri; });
  }, [allColumns, deploymentOrder, deploymentQueries, filters, podQueries, problemOnly, recentRestartFilter, selectedDeployments, visibleColumns]);

  const totals = useMemo(() => {
    const counts: Record<PodStatus, number> = {
      Running: 0,
      Pending: 0,
      Failed: 0,
      Unknown: 0
    };
    for (const query of podQueries) {
      for (const pod of query.data ?? []) counts[pod.status] += 1;
    }
    return counts;
  }, [podQueries]);

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
  const unauthorizedClusterIds = namespaceErrors
    .filter((group) => group.cluster.authType === 'kubectl-login' && /(?:HTTP-Code:\s*401|\b401\b|Unauthorized)/i.test(group.error?.message ?? ''))
    .map((group) => group.cluster.id)
    .sort();
  const unauthorizedSignature = unauthorizedClusterIds.join(',');
  useEffect(() => {
    if (unauthorizedClusterIds.length) onClustersUnauthorized?.(unauthorizedClusterIds);
  }, [onClustersUnauthorized, unauthorizedSignature]);
  const namespaceErrorText = namespaceErrors
    .filter((group) => !unauthorizedClusterIds.includes(group.cluster.id))
    .map((group) => `${group.cluster.name}: ${group.error?.message}`)
    .join(' · ');
  if (clustersLoading) return <div className={styles.state}>Загрузка кластеров Kubernetes…</div>;
  if (clustersError) return <div className={styles.stateError}>Не удалось загрузить кластеры: {clustersError.message}</div>;

  const openDeployment = (column: Column, deploymentName: string) => onOpenDeployment({
    clusterId: column.clusterId,
    clusterName: column.clusterName,
    namespace: column.namespace,
    deploymentName
  });
  const openDeploymentRow = (deploymentName: string, cells: Map<string, Pod[]>) => {
    const columns = visibleColumns.filter((column) => cells.has(`${column.clusterId}/${column.namespace}`));
    if (columns.length === 1) openDeployment(columns[0], deploymentName); else if (columns.length > 1) setDeploymentChoices({ name: deploymentName, columns });
  };

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

  const totalPods = totals.Running + totals.Pending + totals.Failed + totals.Unknown;

  const startDeploymentColumnResize = (event: ReactPointerEvent<HTMLElement>) => {
    event.preventDefault();
    event.stopPropagation();
    resizeGuardUntil.current = Date.now() + 800;
    const startX = event.clientX;
    const startWidth = deploymentColumnWidth;
    const resize = (moveEvent: PointerEvent) => {
      setDeploymentColumnWidth(Math.max(110, Math.min(420, startWidth + moveEvent.clientX - startX)));
    };
    const stop = () => {
      resizeGuardUntil.current = Date.now() + 500;
      document.removeEventListener('pointermove', resize);
      document.removeEventListener('pointerup', stop);
    };
    document.addEventListener('pointermove', resize);
    document.addEventListener('pointerup', stop, { once: true });
  };
  const startNamespaceHeaderResize = (event: ReactPointerEvent<HTMLElement>) => {
    event.preventDefault(); event.stopPropagation();
    const startY = event.clientY; const startHeight = namespaceHeaderHeight;
    const resize = (moveEvent: PointerEvent) => setNamespaceHeaderHeight(Math.max(110, Math.min(360, startHeight + moveEvent.clientY - startY)));
    const stop = () => { document.removeEventListener('pointermove', resize); document.removeEventListener('pointerup', stop); };
    document.addEventListener('pointermove', resize); document.addEventListener('pointerup', stop, { once: true });
  };

  const applyView = (view: MatrixView) => {
    setActiveViewId(view.id); setFilters(view.filters); setRecentRestartFilter(view.recentRestartFilter);
    setProblemOnly(view.problemOnly); setHideFilteredNamespaces(view.hideFilteredNamespaces);
    setSelectedDeployments(view.selectedDeployments); setSelectedNamespaces(view.selectedNamespaces);
    setSelectedClusters(view.selectedClusterIds ?? null); setNamespaceOrder(view.namespaceOrder ?? []); setDeploymentOrder(view.deploymentOrder ?? []);
    setTabMenu(null); void refreshNow();
  };
  const addView = () => setViewEditor({ mode: 'create', value: 'Новая вкладка', error: '' });
  const renameView = (viewId: string) => { const current = views.find((view) => view.id === viewId); if (!current) return; setTabMenu(null); setViewEditor({ mode: 'rename', viewId, value: current.name, error: '' }); };
  const submitViewEditor = () => {
    if (!viewEditor) return;
    const name = viewEditor.value.trim();
    if (!name) { setViewEditor({ ...viewEditor, error: 'Введите название вкладки.' }); return; }
    if (views.some((view) => view.id !== viewEditor.viewId && view.name.toLocaleLowerCase() === name.toLocaleLowerCase())) { setViewEditor({ ...viewEditor, error: `Вкладка «${name}» уже существует.` }); return; }
    if (viewEditor.mode === 'rename' && viewEditor.viewId) setViews((items) => items.map((item) => item.id === viewEditor.viewId ? { ...item, name } : item));
    else { const view: MatrixView = { id: createLocalId(), name, filters: { ...filters }, recentRestartFilter, problemOnly, hideFilteredNamespaces, selectedDeployments, selectedNamespaces, selectedClusterIds: selectedClusters, namespaceOrder: [...namespaceOrder], deploymentOrder: [...deploymentOrder] }; setViews((current) => [...current, view]); applyView(view); }
    setViewMessage(''); setViewEditor(null);
  };
  const requestRemoveView = (viewId: string) => { const current = views.find((view) => view.id === viewId); if (!current || views.length <= 1) return; setTabMenu(null); setConfirmRequest({ title: 'Удаление вкладки', description: `Вкладка «${current.name}» и сохранённые в ней фильтры и порядок элементов будут удалены.`, confirmLabel: 'Удалить вкладку', onConfirm: () => { const next = views.filter((view) => view.id !== viewId); setViews(next); if (activeViewId === viewId) applyView(next[0]); } }); };
  const exportViews = (exportedViews: MatrixView[] = views) => {
    const requiredIds = new Set(exportedViews.flatMap((view) => view.selectedClusterIds ?? groups.map((group) => group.cluster.id)));
    const requiredClusters = groups.filter((group) => requiredIds.has(group.cluster.id)).map((group) => ({ id: group.cluster.id, name: group.cluster.name }));
    const blob = new Blob([JSON.stringify({ format: 'quaber-matrix-views', version: 1, requiredClusters, views: exportedViews }, null, 2)], { type: 'application/json' });
    const link = document.createElement('a'); link.href = URL.createObjectURL(blob); link.download = 'quaber-views.json'; link.click(); URL.revokeObjectURL(link.href);
  };
  const importViews = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0]; event.target.value = ''; if (!file) return;
    try {
      const payload = JSON.parse(await file.text()) as { format?: string; requiredClusters?: Array<{ id: string; name: string }>; views?: MatrixView[] };
      if (payload.format !== 'quaber-matrix-views' || !Array.isArray(payload.views) || !payload.views.length) throw new Error('Неверный формат файла.');
      const available = new Set(groups.map((group) => group.cluster.id));
      const required = payload.requiredClusters ?? [...new Set(payload.views.flatMap((view) => [...(view.selectedClusterIds ?? []), ...(view.namespaceOrder ?? []).map((key) => key.split('/')[0])]))].map((id) => ({ id, name: id }));
      const missing = required.filter((cluster) => !available.has(cluster.id));
      if (missing.length) throw new Error(`Импорт невозможен. Нет кластеров: ${missing.map((cluster) => cluster.name).join(', ')}`);
      const importedNames = payload.views.map((view) => view.name?.trim()).filter(Boolean) as string[]; const normalized = importedNames.map((name) => name.toLocaleLowerCase());
      if (importedNames.length !== payload.views.length || new Set(normalized).size !== normalized.length) throw new Error('В импортируемом файле есть пустые или одинаковые имена вкладок.');
      const duplicate = importedNames.find((name) => views.some((view) => view.name.toLocaleLowerCase() === name.toLocaleLowerCase())); if (duplicate) throw new Error(`Вкладка «${duplicate}» уже существует.`);
      const imported = payload.views.map((view) => ({ ...view, id: createLocalId(), name: view.name.trim(), namespaceOrder: (view.namespaceOrder ?? []).filter((key) => available.has(key.split('/')[0])), deploymentOrder: view.deploymentOrder ?? [] }));
      setViews((current) => [...current, ...imported]); applyView(imported[0]); setViewMessage(`Импортировано вкладок: ${imported.length}.`);
    } catch (error) { setViewMessage(`Импорт не выполнен: ${error instanceof Error ? error.message : 'не удалось прочитать вкладки.'}`); }
  };
  const moveView = (targetId: string) => { if (!draggedViewId || draggedViewId === targetId) return; setViews((items) => { const next=[...items],from=next.findIndex((item)=>item.id===draggedViewId),to=next.findIndex((item)=>item.id===targetId);if(from<0||to<0)return items;next.splice(to,0,next.splice(from,1)[0]);return next;});setDraggedViewId(null); };
  const showDropTarget = (event: DragEvent, target: string, type: 'namespace' | 'deployment') => {
    event.preventDefault(); event.dataTransfer.dropEffect = 'move'; const rect = event.currentTarget.getBoundingClientRect();
    const side = (type === 'namespace' ? event.clientX < rect.left + rect.width / 2 : event.clientY < rect.top + rect.height / 2) ? 'before' : 'after';
    setDragTarget({ type, target, side });
  };
  const moveItem = (event: DragEvent, target: string, type: 'namespace' | 'deployment') => {
    event.preventDefault(); const source = event.dataTransfer.getData(`application/x-quaber-${type}`); if (!source || source === target) return;
    const available = type === 'namespace' ? allColumns.map((column) => `${column.clusterId}/${column.namespace}`) : deploymentNames;
    const current = type === 'namespace' ? namespaceOrder : deploymentOrder;
    const ordered = [...current, ...available.filter((item) => !current.includes(item))].filter((item, index, array) => array.indexOf(item) === index);
    const from = ordered.indexOf(source); let to = ordered.indexOf(target); if (from < 0 || to < 0) return; const [item] = ordered.splice(from, 1); to = ordered.indexOf(target) + (dragTarget?.side === 'after' ? 1 : 0); ordered.splice(to, 0, item);
    if (type === 'namespace') setNamespaceOrder(ordered); else setDeploymentOrder(ordered);
    setDragTarget(null);
  };

  if (easterEggActive) {
    return (
      <section className={styles.easterEgg} aria-label="Танцующий ASCII-кот">
        <pre key={catFrame}>{dancingCatFrames[catFrame]}</pre>
      </section>
    );
  }

  return (
    <section className={styles.root} style={{ '--view-color': views.find((view) => view.id === activeViewId)?.color ?? 'transparent' } as CSSProperties} onClick={() => { setContext(null); setTabMenu(null); }} onDragOver={(event) => { if (!(event.target instanceof Element) || !event.target.closest('[data-drop-zone],button[draggable]')) event.dataTransfer.dropEffect = 'none'; }} onDrop={() => setDragTarget(null)}>
      <div className={styles.stickyPanel}>
        <div className={styles.viewTabs}>
          <div className={styles.viewTabList}>{views.map((view) => <button draggable type="button" key={view.id} style={{ '--tab-color': view.color ?? 'transparent' } as CSSProperties} className={`${view.id === activeViewId ? styles.activeViewTab : ''} ${draggedViewId === view.id ? styles.draggedViewTab : ''}`} onDragStart={(event) => { event.dataTransfer.effectAllowed='move';setDraggedViewId(view.id); }} onDragEnd={() => setDraggedViewId(null)} onDragOver={(event) => { event.preventDefault();event.dataTransfer.dropEffect='move'; }} onDrop={(event) => { event.preventDefault();moveView(view.id); }} onClick={() => applyView(view)} onContextMenu={(event) => { event.preventDefault(); event.stopPropagation(); setTabMenu({ x: event.clientX, y: event.clientY, viewId: view.id }); }}>{view.name}</button>)}<button type="button" className={styles.addViewTab} onClick={addView} title="Добавить вкладку">＋</button></div>
          {viewMessage && <div className={styles.viewMessage}><span>{viewMessage}</span><button type="button" onClick={() => setViewMessage('')} aria-label="Закрыть уведомление" title="Закрыть">×</button></div>}
          <div className={styles.viewActions}><button type="button" onClick={() => importRef.current?.click()}>Импорт</button><input ref={importRef} hidden type="file" accept="application/json,.json" onChange={(event) => { void importViews(event); }} /></div>
        </div>
        <div className={styles.controls}>
          <div className={styles.filters}>
            {filteredStatuses.slice(0, 1).map((status) => (
              <label className={`${styles.filter} ${styles[`filter${status}`]}`} key={status}>
                <input
                  type="checkbox"
                  checked={filters[status]}
                  disabled={problemOnly}
                  onChange={(event) => setFilters((current) => ({ ...current, [status]: event.target.checked }))}
                />
                <span>{statusLabel(status)}</span>
                <b>{totals[status]}</b>
              </label>
            ))}
            <label className={`${styles.filter} ${styles.recentRestartFilter}`}>
              <input type="checkbox" checked={recentRestartFilter} disabled={problemOnly || filters.Running} onChange={(event) => setRecentRestartFilter(event.target.checked)} />
              <span>Restart &lt; 1h</span>
            </label>
            {filteredStatuses.slice(1).map((status) => (
              <label className={`${styles.filter} ${styles[`filter${status}`]}`} key={status}>
                <input
                  type="checkbox"
                  checked={filters[status]}
                  disabled={problemOnly}
                  onChange={(event) => setFilters((current) => ({ ...current, [status]: event.target.checked }))}
                />
                <span>{statusLabel(status)}</span>
                <b>{totals[status]}</b>
              </label>
            ))}
            <label className={`${styles.filterAlt} ${problemOnly ? styles.problemActive : ''}`}>
              <input type="checkbox" checked={problemOnly} onChange={(event) => setProblemOnly(event.target.checked)} />
              <span>Issues</span>
            </label>
            <label className={styles.filterAlt}>
              <input type="checkbox" checked={hideFilteredNamespaces} onChange={(event) => setHideFilteredNamespaces(event.target.checked)} />
              <span>Hide empty</span>
            </label>
            <div className={styles.summaryCounter}><b>{totalPods}</b><span>Total</span></div>
          </div>
          <div className={styles.rightControls}>
            <div className={styles.lastUpdated}>Последнее обновление: <b>{lastUpdatedLabel}</b></div>
            <button className={styles.controlButton} onClick={() => { void refreshNow(); }}>Обновить</button>
            <label className={styles.refreshLabel}>
              <span>Автообновление</span>
              <select value={customIntervalActive ? 'custom' : String(refreshInterval)} onChange={(event) => { if (event.target.value === 'custom') { setCustomIntervalActive(true); setCustomMinutes(''); setRefreshInterval(false); return; } setCustomIntervalActive(false); setCustomMinutes(''); if (event.target.value === 'false') setRefreshInterval(false); else setRefreshInterval(Number(event.target.value)); }}>
                {refreshOptions.map((option) => <option key={String(option.value)} value={String(option.value)}>{option.label}</option>)}
                <option value="custom">Свой интервал</option>
              </select>
              {customIntervalActive && <input className={styles.customInterval} type="text" inputMode="numeric" pattern="[0-9]*" maxLength={4} title="Свой интервал в минутах" aria-label="Интервал автообновления в минутах" value={customMinutes} placeholder="мин" onChange={(event) => { const digits = event.target.value.replace(/\D/g, '').slice(0, 4); setCustomMinutes(digits); if (!digits) { setRefreshInterval(false); return; } const minutes = Math.min(1440, Math.max(1, Number(digits))); setCustomMinutes(String(minutes)); setRefreshInterval(minutes * 60_000); }} />}
            </label>
          </div>
        </div>
        <div className={styles.toolbar}>
          <div ref={toolbarFiltersRef} className={styles.toolbarFilters}>
          <details className={styles.deploymentFilter} open={openToolbarFilter === 'deployments'}>
            <summary onClick={(event) => { event.preventDefault(); setOpenToolbarFilter((current) => current === 'deployments' ? null : 'deployments'); }}>
              <span>Deployment</span>
              <b>{selectedDeploymentCount === null ? 'Все' : `${selectedDeploymentCount}/${deploymentNames.length}`}</b>
            </summary>
            <div className={styles.deploymentFilterMenu}>
              <input maxLength={200} value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Найти deployment…" />
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
          <details className={styles.deploymentFilter} open={openToolbarFilter === 'namespaces'}>
            <summary onClick={(event) => { event.preventDefault(); setOpenToolbarFilter((current) => current === 'namespaces' ? null : 'namespaces'); }}><span>Namespace</span><b>{selectedNamespaceCount === null ? 'Все' : `${selectedNamespaceCount}/${namespaceNames.length}`}</b></summary>
            <div className={styles.deploymentFilterMenu}>
              <div className={styles.deploymentFilterActions}><button type="button" onClick={() => setSelectedNamespaces(null)}>Выбрать все</button><button type="button" onClick={() => setSelectedNamespaces([])}>Снять все</button></div>
              <div className={styles.deploymentFilterOptions}>{namespaceNames.map((name) => <label key={name}><input type="checkbox" checked={selectedNamespaces === null || selectedNamespaces.includes(name)} onChange={(event) => setSelectedNamespaces((current) => { const selected = current === null ? namespaceNames : current; return event.target.checked ? [...new Set([...selected, name])] : selected.filter((item) => item !== name); })} /><span>{name}</span></label>)}</div>
            </div>
          </details>
          <details className={styles.deploymentFilter} open={openToolbarFilter === 'clusters'}>
            <summary onClick={(event) => { event.preventDefault(); setOpenToolbarFilter((current) => current === 'clusters' ? null : 'clusters'); }}><span>Кластер</span><b>{selectedClusterCount === null ? 'Все' : `${selectedClusterCount}/${clusterNames.length}`}</b></summary>
            <div className={styles.deploymentFilterMenu}>
              <div className={styles.deploymentFilterActions}><button type="button" onClick={() => setSelectedClusters(null)}>Выбрать все</button><button type="button" onClick={() => setSelectedClusters([])}>Снять все</button></div>
              <div className={styles.deploymentFilterOptions}>{clusterNames.map(([id, name]) => <label key={id}><input type="checkbox" checked={selectedClusters === null || selectedClusters.includes(id)} onChange={(event) => setSelectedClusters((current) => { const selected = current === null ? clusterNames.map(([clusterId]) => clusterId) : current; return event.target.checked ? [...new Set([...selected, id])] : selected.filter((item) => item !== id); })} /><span>{name}</span></label>)}</div>
            </div>
          </details>
          </div>
        </div>
        {namespaceErrorText && dismissedNamespaceError !== namespaceErrorText && <div className={styles.inlineError}><span>{namespaceErrorText}</span><button type="button" onClick={() => setDismissedNamespaceError(namespaceErrorText)} aria-label="Закрыть ошибку" title="Закрыть">×</button></div>}
      </div>

      <div className={styles.tableWrap}>
        <table className={styles.table} style={{
          width: deploymentColumnWidth + visibleColumns.length * 24,
          '--deployment-column-width': `${deploymentColumnWidth}px`,
          '--namespace-header-height': `${namespaceHeaderHeight}px`
        } as MatrixTableStyle}>
          <colgroup>
            <col style={{ width: deploymentColumnWidth }} />
            {visibleColumns.map((column) => <col key={`col-${column.clusterId}/${column.namespace}`} style={{ width: 24 }} />)}
          </colgroup>
          <thead>
            <tr className={styles.namespaceRow}>
              <th className={styles.deploymentHeader}>
                <span>Deployment</span>
                <i className={styles.deploymentResizer} onPointerDown={startDeploymentColumnResize} title="Изменить ширину колонки" />
                <i className={styles.cornerHeightResizer} onPointerDown={startNamespaceHeaderResize} title="Изменить высоту заголовков Namespace" />
              </th>
              {visibleColumns.map((column) => (
                <th data-drop-zone="namespace" draggable onDragStart={(event) => { event.dataTransfer.effectAllowed = 'move'; event.dataTransfer.setData('application/x-quaber-namespace', `${column.clusterId}/${column.namespace}`); }} onDragEnd={() => setDragTarget(null)} onDragOver={(event) => showDropTarget(event, `${column.clusterId}/${column.namespace}`, 'namespace')} onDrop={(event) => moveItem(event, `${column.clusterId}/${column.namespace}`, 'namespace')} key={`${column.clusterId}/${column.namespace}`} className={`${styles.namespaceHeader} ${styles[`clusterTone${column.clusterIndex % 4}`]} ${column.lastInCluster ? styles.clusterBoundary : ''} ${dragTarget?.type === 'namespace' && dragTarget.target === `${column.clusterId}/${column.namespace}` ? (dragTarget.side === 'before' ? styles.dropBefore : styles.dropAfter) : ''}`} title={`${column.clusterName} / ${column.namespace}`}><div className={styles.namespaceParts}><span className={styles.namespaceCluster}><em>{clusterAlias(column.clusterId, column.clusterName, aliases)}</em></span><span className={styles.namespaceName}>{column.namespace}</span></div><i className={styles.namespaceHeightResizer} onPointerDown={startNamespaceHeaderResize} title="Изменить высоту заголовков Namespace" /></th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              const rowHovered = hovered?.row === row.deploymentName;
              return (
                <tr key={row.deploymentName} className={rowHovered ? styles.highlightRow : ''}>
                  <th data-drop-zone="deployment" draggable onDragStart={(event) => { event.dataTransfer.effectAllowed = 'move'; event.dataTransfer.setData('application/x-quaber-deployment', row.deploymentName); }} onDragEnd={() => setDragTarget(null)} onDragOver={(event) => showDropTarget(event, row.deploymentName, 'deployment')} onDrop={(event) => moveItem(event, row.deploymentName, 'deployment')} className={dragTarget?.type === 'deployment' && dragTarget.target === row.deploymentName ? (dragTarget.side === 'before' ? styles.dropBefore : styles.dropAfter) : ''} title={`${row.deploymentName} — перетащите для изменения порядка`} onClick={() => { if (Date.now() > resizeGuardUntil.current) openDeploymentRow(row.deploymentName, row.cells); }}>{row.deploymentName}<i className={styles.rowDeploymentResizer} onPointerDown={startDeploymentColumnResize} title="Изменить ширину колонки" /></th>
                  {visibleColumns.map((column) => {
                    const columnKey = `${column.clusterId}/${column.namespace}`;
                    const sourceIndex = allColumns.findIndex((item) => item.clusterId === column.clusterId && item.namespace === column.namespace);
                    const exists = row.cells.has(columnKey);
                    const scaledToZero = (deploymentQueries[sourceIndex]?.data ?? []).some((deployment) => deployment.name === row.deploymentName && deployment.replicas === 0);
                    const pods = row.cells.get(columnKey) ?? [];
                    const visiblePods = pods.filter((pod) => matchesSelectedFilters(
                      pod,
                      filters,
                      recentRestartFilter,
                      problemOnly
                    ));
                    const columnHovered = hovered?.column === columnKey;
                    const intersection = rowHovered && columnHovered;
                    return (
                      <td
                        key={`${row.deploymentName}-${columnKey}`}
                        className={`${columnHovered ? styles.highlightColumn : ''} ${intersection ? styles.highlightIntersection : ''} ${exists ? styles.hasDeployment : ''}`}
                        onMouseEnter={() => setHovered({ row: row.deploymentName, column: columnKey })}
                        onMouseLeave={() => setHovered(null)}
                        onClick={() => exists && openDeployment(column, row.deploymentName)}
                        title={`${column.clusterName} / ${column.namespace}`}
                      >
                        {scaledToZero && <span className={styles.zeroReplicas} title="Для Deployment установлено 0 replicas">0</span>}
                        {visiblePods.length === 0 ? !scaledToZero && <span className={styles.empty}>—</span> : (
                          <div className={styles.pods}>
                            {visiblePods.map((pod) => {
                              const lastRestartTime = recentTime(pod.lastRestartAt);
                              const showRestartMarker = lastRestartTime !== null && pod.status === 'Running';
                              const restartInfo = lastRestartTime ? `\nПерезапущен после падения: ${lastRestartTime}` : '';
                              const recentPodStart = recentTime(pod.creationTimestamp);
                              return (
                                <span
                                  className={`${styles.pod} ${styles[styleStatus(pod.status)] ?? styles.unknown} ${showRestartMarker ? styles.recentRestart : ''} ${recentPodStart !== null && pod.status === 'Running' && !showRestartMarker ? styles.manualRestart : ''}`}
                                  title={`Name: ${pod.name}\nPath: ${column.clusterName} / ${column.namespace}\nStatus: ${pod.terminating ? 'Terminating' : pod.status}${restartInfo}${recentPodStart ? `\nЗапущен менее часа назад: ${recentPodStart}` : ''}`}
                                  key={pod.name}
                                  onClick={(event) => { event.stopPropagation(); onOpenPod(podRef(pod, column, row.deploymentName)); }}
                                  onContextMenu={(event) => { event.preventDefault(); event.stopPropagation(); setContext({ x: event.clientX, y: event.clientY, pod, column, deploymentName: row.deploymentName }); }}
                                >
                                  {showRestartMarker ? '↻' : ''}
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
          <button onClick={() => { onOpenPod(podRef(context.pod, context.column, context.deploymentName, 'describe')); setContext(null); }}>Открыть карточку Pod</button>
          <button onClick={() => { onOpenPod(podRef(context.pod, context.column, context.deploymentName, 'logs')); setContext(null); }}>Открыть логи</button>
          <button onClick={() => { onOpenPod(podRef(context.pod, context.column, context.deploymentName, 'exec')); setContext(null); }}>Открыть консоль Pod</button>
          <hr />
          <button onClick={() => {
            setConfirmRequest({
              title: 'Перезапуск Pod',
              description: `Pod ${context.pod.name} будет удалён, после чего контроллер создаст новый экземпляр.`,
              confirmLabel: 'Перезапустить',
              onConfirm: async () => { await deleteMutation.mutateAsync({ pod: context.pod, column: context.column }); }
            });
            setContext(null);
          }}>Перезапустить Pod</button>
        </div>
      )}

      {tabMenu && (() => { const view = views.find((item) => item.id === tabMenu.viewId); return view ? <div className={styles.tabContextMenu} style={{ left: tabMenu.x, top: tabMenu.y }} onClick={(event) => event.stopPropagation()}><button type="button" onClick={() => renameView(view.id)}>Переименовать</button><button type="button" onClick={() => { exportViews([view]); setTabMenu(null); }}>Экспортировать</button><div className={styles.colorMenu}><span>Цвет фона</span><div>{['transparent','#3b82f6','#10a87c','#d89516','#8b5cf6','#dc2626','#e2558f'].map((color) => <button key={color} aria-label={color === 'transparent' ? 'Без цвета' : `Цвет ${color}`} title={color === 'transparent' ? 'Без цвета' : color} style={{ background: color === 'transparent' ? 'var(--bg-input)' : color }} onClick={() => { setViews((items) => items.map((item) => item.id === view.id ? { ...item, color } : item)); setTabMenu(null); }} />)}</div></div><hr /><button type="button" className={styles.contextDanger} disabled={views.length <= 1} onClick={() => requestRemoveView(view.id)}>Удалить</button></div> : null; })()}

      {deploymentChoices && <div className={styles.choiceOverlay} onMouseDown={(event) => event.target === event.currentTarget && setDeploymentChoices(null)}><div className={styles.choiceModal}><div className={styles.choiceHeader}><div><h3>{deploymentChoices.name}</h3><span>Выберите точное расположение deployment</span></div><button onClick={() => setDeploymentChoices(null)} aria-label="Закрыть">×</button></div><div className={styles.choiceList}>{deploymentChoices.columns.map((column) => <button key={`${column.clusterId}/${column.namespace}`} onClick={() => { openDeployment(column, deploymentChoices.name); setDeploymentChoices(null); }}><b>{column.namespace}</b><span>{column.clusterName} / {column.namespace}</span></button>)}</div></div></div>}

      {viewEditor && <div className={styles.viewEditorOverlay}><form className={styles.viewEditorModal} onSubmit={(event) => { event.preventDefault(); submitViewEditor(); }}><div className={styles.viewEditorHeader}><h3>{viewEditor.mode === 'create' ? 'Новая вкладка' : 'Переименование вкладки'}</h3><button type="button" onClick={() => setViewEditor(null)} aria-label="Закрыть" title="Закрыть">×</button></div><label>Название<input autoFocus maxLength={80} className={viewEditor.error ? styles.viewNameInvalid : ''} value={viewEditor.value} onChange={(event) => setViewEditor({ ...viewEditor, value: event.target.value, error: '' })} onFocus={(event) => event.currentTarget.select()} /></label>{viewEditor.error && <div className={styles.viewEditorError}>{viewEditor.error}</div>}<div className={styles.viewEditorActions}><button type="button" onClick={() => setViewEditor(null)}>Отмена</button><button type="submit" className={styles.viewEditorPrimary}>{viewEditor.mode === 'create' ? 'Создать' : 'Сохранить'}</button></div></form></div>}

      <ActionConfirmModal request={confirmRequest} onClose={() => setConfirmRequest(null)} />
    </section>
  );
}
