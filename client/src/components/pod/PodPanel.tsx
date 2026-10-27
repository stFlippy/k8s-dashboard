import { useEffect, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  execPod,
  getPod,
  getPodEvents,
  getPodLogs,
  getPodMetrics,
  removePod,
  replacePodResource
} from '../../api/kubernetes';
import type { Pod, PodMetricContainer, PodRef } from '../../types/kubernetes';
import { ActionConfirmModal } from '../common/ActionConfirmModal';
import type { ActionConfirmRequest } from '../common/ActionConfirmModal';
import { formatDate } from '../../utils/formatDate';
import styles from './PodPanel.module.css';
import { CodeEditor } from '../common/CodeEditor';

type Tab = 'logs' | 'exec' | 'describe' | 'yaml' | 'metrics' | 'events' | 'containers';
interface Props { pod: PodRef | null; onClose: () => void; }

function age(timestamp: string | null): string {
  if (!timestamp) return '—';
  const minutes = Math.floor(Math.max(0, Date.now() - new Date(timestamp).getTime()) / 60_000);
  if (minutes < 60) return `${minutes} мин`;
  const hours = Math.floor(minutes / 60);
  return hours < 48 ? `${hours} ч ${minutes % 60} мин` : `${Math.floor(hours / 24)} д`;
}

interface MetricSample { at: number; containers: PodMetricContainer[]; }

function formatCpu(value: number | null): string {
  if (value === null) return '—';
  const millicores = value * 1000;
  return `${millicores < 10 ? millicores.toFixed(2) : millicores.toFixed(1)}m`;
}

function formatMemory(value: number | null): string {
  if (value === null) return '—';
  const mib = value / 1024 / 1024;
  return `${mib < 10 ? mib.toFixed(2) : mib.toFixed(1)} MiB`;
}

function formatPercent(value: number | null): string {
  return value === null ? '—' : `${value.toFixed(value < 10 ? 1 : 0)}%`;
}

interface MetricChartProps {
  title: string;
  history: MetricSample[];
  containerName: string;
  value: (container: PodMetricContainer) => number | null;
  request?: number | null;
  limit?: number | null;
  mode: 'percent' | 'actual';
  formatActual: (value: number | null) => string;
  alwaysPercent?: boolean;
}

function MetricChart({ title, history, containerName, value, request = null, limit = null, mode, formatActual, alwaysPercent = false }: MetricChartProps) {
  const width = 520; const height = 154; const left = 78; const labelLeft = 8; const right = 12; const top = 12; const bottom = 24;
  const plotWidth = width - left - right; const plotHeight = height - top - bottom;
  const usePercent = alwaysPercent || mode === 'percent';
  const basis = limit ?? request;
  const convert = (item: number | null): number | null => item === null ? null : usePercent && !alwaysPercent ? (basis && basis > 0 ? item / basis * 100 : null) : item;
  const values = history.map((sample) => { const container = sample.containers.find((item) => item.name === containerName); return convert(container ? value(container) : null); });
  const requestValue = convert(request); const limitValue = convert(limit);
  const finite = [...values, requestValue, limitValue].filter((item): item is number => item !== null && Number.isFinite(item));
  const maximum = Math.max(usePercent ? 100 : 0, ...finite, 0.000001) * 1.08;
  const x = (index: number) => left + (values.length <= 1 ? plotWidth / 2 : index / (values.length - 1) * plotWidth);
  const y = (item: number) => top + plotHeight - Math.min(maximum, Math.max(0, item)) / maximum * plotHeight;
  const points = values.map((item, index) => item === null ? null : `${x(index)},${y(item)}`).filter(Boolean).join(' ');
  const current = values.at(-1) ?? null;
  const format = (item: number | null) => usePercent ? formatPercent(item) : formatActual(item);
  return <div className={styles.metricChart}>
    <div className={styles.metricChartTitle}><b>{title}</b><strong>{format(current)}</strong></div>
    <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`График ${title}`}>
      {[0, .5, 1].map((part) => <line key={part} className={styles.metricGridLine} x1={left} x2={width - right} y1={top + plotHeight * part} y2={top + plotHeight * part} />)}
      <text className={styles.metricAxisLabel} x={labelLeft} y={top + 4} textAnchor="start">{format(maximum)}</text><text className={styles.metricAxisLabel} x={labelLeft} y={top + plotHeight + 4} textAnchor="start">0</text>
      {requestValue !== null && <line className={styles.metricRequestLine} x1={left} x2={width - right} y1={y(requestValue)} y2={y(requestValue)} />}
      {limitValue !== null && <line className={styles.metricLimitLine} x1={left} x2={width - right} y1={y(limitValue)} y2={y(limitValue)} />}
      {points && <polyline className={styles.metricUsageLine} points={points} />}
      {values.length === 1 && current !== null && <circle className={styles.metricUsagePoint} cx={x(0)} cy={y(current)} r="3" />}
      <text x={left} y={height - 5}>{values.length > 1 ? `${Math.max(1, Math.round((history.at(-1)!.at - history[0].at) / 1000))} сек истории` : 'первый замер'}</text>
    </svg>
    <div className={styles.metricLegend}><span className={styles.legendUsage}>Текущее: {format(current)}</span>{request !== null && <span className={styles.legendRequest}>Request: {format(requestValue)}</span>}{limit !== null && <span className={styles.legendLimit}>Limit: {format(limitValue)}</span>}{usePercent && !alwaysPercent && <em>база: {limit !== null ? 'limit' : request !== null ? 'request' : 'не задана'}</em>}</div>
  </div>;
}

export function PodPanel({ pod, onClose }: Props) {
  const queryClient = useQueryClient();
  const [tab, setTab] = useState<Tab>('logs');
  const [container, setContainer] = useState('');
  const [tailLines, setTailLines] = useState(500);
  const [showPreviousLogs, setShowPreviousLogs] = useState(false);
  const [autoLogs, setAutoLogs] = useState(true);
  const [command, setCommand] = useState('');
  const [terminal, setTerminal] = useState<Array<{ command: string; output: string; error?: boolean }>>([]);
  const [resourceText, setResourceText] = useState('');
  const [message, setMessage] = useState('');
  const [metricMode, setMetricMode] = useState<'percent' | 'actual'>('percent');
  const [metricHistory, setMetricHistory] = useState<MetricSample[]>([]);
  const [consoleUnlockedUntil, setConsoleUnlockedUntil] = useState(0);
  const [confirmRequest, setConfirmRequest] = useState<ActionConfirmRequest | null>(null);
  const consoleInputRef = useRef<HTMLInputElement>(null);
  const terminalBodyRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const terminalBody = terminalBodyRef.current;
    if (!terminalBody) return;
    terminalBody.scrollTop = terminalBody.scrollHeight;
    consoleInputRef.current?.focus();
  }, [terminal]);

  const key = pod ? [pod.clusterId, pod.namespace, pod.podName] as const : ['', '', ''] as const;
  const detail = useQuery({
    queryKey: ['pod-detail', ...key],
    queryFn: () => getPod(key[0], key[1], key[2]),
    enabled: Boolean(pod),
    refetchInterval: 5_000,
    refetchOnWindowFocus: true
  });

  useEffect(() => {
    if (pod) {
      setTab(pod.initialTab ?? 'logs');
      setMessage('');
      setTerminal([]);
      setConsoleUnlockedUntil(0);
      setShowPreviousLogs(false);
      setMetricHistory([]);
    } else {
      setConsoleUnlockedUntil(0);
    }
  }, [pod]);

  useEffect(() => {
    if (tab !== 'exec') setConsoleUnlockedUntil(0);
  }, [tab]);

  useEffect(() => {
    const currentDetail = detail.data;
    if (currentDetail) {
      setContainer((current) => current && currentDetail.containers.some((item) => item.name === current) ? current : currentDetail.containers[0]?.name ?? '');
      setResourceText(JSON.stringify(currentDetail.resource, null, 2));
    }
  }, [detail.dataUpdatedAt, key[0], key[1], key[2]]);

  const logs = useQuery({
    queryKey: ['pod-logs', ...key, container, tailLines, showPreviousLogs],
    queryFn: () => getPodLogs(key[0], key[1], key[2], container, tailLines, showPreviousLogs),
    enabled: Boolean(pod && container && tab === 'logs'),
    refetchInterval: autoLogs ? 5_000 : false,
    refetchOnWindowFocus: true
  });
  const metrics = useQuery({ queryKey: ['pod-metrics', ...key], queryFn: () => getPodMetrics(key[0], key[1], key[2]), enabled: Boolean(pod && tab === 'metrics'), refetchInterval: 5_000 });
  const events = useQuery({ queryKey: ['pod-events', ...key], queryFn: () => getPodEvents(key[0], key[1], key[2]), enabled: Boolean(pod && tab === 'events'), refetchInterval: 5_000 });

  const remove = useMutation({ mutationFn: () => removePod(key[0], key[1], key[2]) });
  const replace = useMutation({ mutationFn: async () => {
    let parsed: unknown;
    try { parsed = JSON.parse(resourceText); } catch { throw new Error('Редактор использует JSON-синтаксис. Не удалось распознать JSON.'); }
    return replacePodResource(key[0], key[1], key[2], parsed);
  }});
  const runExec = useMutation({ mutationFn: (value: string) => execPod(key[0], key[1], key[2], container, value) });
  const consoleUnlocked = consoleUnlockedUntil > Date.now();

  useEffect(() => {
    if (!metrics.data?.available) return;
    const at = metrics.data.timestamp ? new Date(metrics.data.timestamp).getTime() : Date.now();
    const safeAt = Number.isFinite(at) ? at : Date.now();
    setMetricHistory((current) => {
      const sample = { at: safeAt, containers: metrics.data!.containers };
      if (current.at(-1)?.at === safeAt) return [...current.slice(0, -1), sample];
      return [...current, sample].slice(-60);
    });
  }, [metrics.dataUpdatedAt]);

  useEffect(() => {
    if (!consoleUnlockedUntil) return;
    const remaining = consoleUnlockedUntil - Date.now();
    if (remaining <= 0) {
      setConsoleUnlockedUntil(0);
      return;
    }
    const timer = window.setTimeout(() => setConsoleUnlockedUntil(0), remaining);
    return () => window.clearTimeout(timer);
  }, [consoleUnlockedUntil]);

  if (!pod) return null;
  const data = detail.data;
  const statusClass = data ? styles[data.status.toLowerCase()] ?? styles.unknown : styles.unknown;

  const executeNow = async (value: string) => {
    setCommand('');
    setConsoleUnlockedUntil(Date.now() + 5 * 60_000);
    try {
      const result = await runExec.mutateAsync(value);
      const output = [result.stdout, result.stderr].filter(Boolean).join('\n') || '(нет вывода)';
      setTerminal((items) => [...items, { command: value, output, error: Boolean(result.stderr && !result.stdout) }]);
    } catch (error) {
      setTerminal((items) => [...items, { command: value, output: error instanceof Error ? error.message : 'Exec завершился с ошибкой.', error: true }]);
    }
  };

  const requestExecute = () => {
    const value = command.trim();
    if (!value || !container) return;
    if (consoleUnlocked) {
      void executeNow(value);
      return;
    }
    setConfirmRequest({
      title: 'Разблокировка консоли Pod',
      description: `После проверки команды можно выполнять без повторного подтверждения. Консоль автоматически заблокируется после 5 минут простоя. Первая команда: «${value}».`,
      confirmLabel: 'Разблокировать и выполнить',
      onConfirm: async () => { setConsoleUnlockedUntil(Date.now() + 5 * 60_000); await executeNow(value); }
    });
  };
  const exportLog = () => {
    const now = new Date(); const two = (value: number) => String(value).padStart(2, '0');
    const filename = `${pod.podName}-${container}-${two(now.getDate())}.${two(now.getMonth() + 1)}.${now.getFullYear()} ${two(now.getHours())}.${two(now.getMinutes())}.log`;
    const link = document.createElement('a'); link.href = URL.createObjectURL(new Blob([logs.data ?? ''], { type: 'text/plain;charset=utf-8' })); link.download = filename; link.click(); URL.revokeObjectURL(link.href);
  };

  return (
    <>
      <div className={styles.overlay} onClick={onClose} />
      <aside className={styles.panel}>
        <div className={styles.header}>
          <div className={styles.title}><h2>{pod.podName}</h2></div>
          <button className={styles.close} onClick={onClose} aria-label="Закрыть панель pod" title="Закрыть">×</button>
        </div>

        <div className={styles.summary}>
          {detail.isLoading && <div className={styles.loading}>Загрузка Pod…</div>}
          {detail.isError && <div className={styles.error}>Не удалось загрузить Pod: {detail.error?.message}</div>}
          {data && <>
            <div className={styles.badges}><span className={`${styles.statusBadge} ${statusClass}`}><i />{data.status}</span></div>
            <div className={styles.infoGrid}>
              <div><span>Кластер</span><b>{pod.clusterName}</b></div><div><span>Namespace</span><b>{pod.namespace}</b></div><div className={styles.ageInfo}><span>Возраст</span><b>{age(data.creationTimestamp)}</b></div>
            </div>
            <div className={styles.actions}>
              <button onClick={() => setConfirmRequest({
                title: 'Перезапуск Pod',
                description: `Pod ${pod.podName} будет удалён, после чего контроллер сможет создать новый экземпляр.`,
                confirmLabel: 'Перезапустить Pod',
                onConfirm: async () => {
                  await remove.mutateAsync();
                  queryClient.setQueryData<Pod[]>(['pods', pod.clusterId, pod.namespace], (current) =>
                    current?.map((item) => item.name === pod.podName ? { ...item, status: 'Failed', terminating: true } : item)
                  );
                  await queryClient.invalidateQueries({ queryKey: ['pods', pod.clusterId, pod.namespace] });
                  onClose();
                }
              })}>Перезапустить</button>
            </div>
          </>}
          {message && <div className={styles.message}>{message}</div>}
        </div>

        <nav className={styles.tabs}>
          {(['logs','exec','describe','yaml','metrics','events','containers'] as Tab[]).map((item) => (
            <button key={item} className={tab === item ? styles.active : ''} onClick={() => setTab(item)}>
              {item === 'logs' ? 'Логи' : item === 'exec' ? 'Консоль Pod' : item === 'describe' ? 'Статус Pod' : item === 'yaml' ? 'YAML' : item === 'metrics' ? 'Метрики' : item === 'events' ? `События${events.data?.length ? ` ${events.data.length}` : ''}` : 'Контейнеры'}
            </button>
          ))}
        </nav>

        <div className={styles.content}>
          {tab === 'logs' && <div className={styles.tabFill}>
            <div className={styles.toolbar}>
              <div>
                <select value={container} onChange={(event) => setContainer(event.target.value)}>{data?.containers.map((item) => <option key={item.name}>{item.name}</option>)}</select>
                <select value={tailLines} onChange={(event) => setTailLines(Number(event.target.value))}><option value={100}>100 строк</option><option value={500}>500 строк</option><option value={1000}>1000 строк</option><option value={5000}>5000 строк</option></select>
                <label><input type="checkbox" checked={autoLogs} onChange={(event) => setAutoLogs(event.target.checked)} /> Автообновление</label>
                <button
                  type="button"
                  className={showPreviousLogs ? styles.previousLogsActive : styles.previousLogsButton}
                  aria-pressed={showPreviousLogs}
                  onClick={() => setShowPreviousLogs((current) => !current)}
                >{showPreviousLogs ? 'Показать текущий лог' : 'Лог предыдущего запуска'}</button>
              </div>
              <div><button onClick={() => navigator.clipboard.writeText(logs.data ?? '')}>Копировать</button><button onClick={exportLog} disabled={!logs.data}>Экспорт</button></div>
            </div>
            {showPreviousLogs && <div className={styles.previousLogsBanner}>Лог предыдущего запуска</div>}
            <pre className={styles.logs}>{logs.isLoading ? 'Загрузка логов…' : logs.isError ? `Ошибка: ${logs.error?.message}` : logs.data || '(лог пуст)'}</pre>
          </div>}

          {tab === 'exec' && <div className={styles.terminal}><div className={styles.terminalToolbar}><div><select value={container} onChange={(event) => setContainer(event.target.value)}>{data?.containers.map((item) => <option key={item.name}>{item.name}</option>)}</select><span>/bin/sh -lc</span></div><button className={consoleUnlocked ? styles.consoleUnlocked : styles.consoleLocked} title={consoleUnlocked ? 'Консоль разблокирована. Нажмите, чтобы заблокировать.' : 'Консоль заблокирована'} aria-label={consoleUnlocked ? 'Заблокировать консоль' : 'Консоль заблокирована'} onClick={() => setConsoleUnlockedUntil(0)}>{consoleUnlocked ? '🔓' : '🔒'}</button></div><div ref={terminalBodyRef} className={styles.terminalBody} onClick={() => { if (window.getSelection()?.isCollapsed !== false) consoleInputRef.current?.focus(); }}>{terminal.map((item, index) => <div key={index}><div><span className={styles.prompt}>{container}@{pod.podName}:~$</span> {item.command}</div><pre className={item.error ? styles.terminalError : ''}>{item.output}</pre></div>)}<div className={styles.commandLine}><span className={styles.prompt}>{container || 'container'}@{pod.podName}:~$</span><input ref={consoleInputRef} value={command} onChange={(event) => setCommand(event.target.value)} onKeyDown={(event) => event.key === 'Enter' && requestExecute()} autoFocus /></div></div></div>}

          {tab === 'describe' && data && <div>
            <section className={styles.describe}><h3>Статус Pod</h3><dl><dt>Имя</dt><dd>{data.name}</dd><dt>Namespace</dt><dd>{data.namespace}</dd><dt>Кластер</dt><dd>{pod.clusterName}</dd><dt>Статус</dt><dd>{data.status}</dd><dt>IP</dt><dd>{data.ip ?? '—'}</dd></dl></section>
            <section className={styles.describe}><h3>Состояния</h3>{data.conditions.map((item) => <div className={styles.condition} key={item.type}><b>{item.type}:</b><span className={item.status === 'True' ? styles.good : ''}>{item.status}</span><span>{item.reason ?? ''}</span><span>{item.message ?? ''}</span></div>)}</section>
          </div>}

          {tab === 'yaml' && <div className={styles.tabFill}><div className={styles.yamlToolbar}><span /><button onClick={() => setConfirmRequest({
            title: 'Сохранение YAML',
            description: `Изменения YAML Pod ${pod.podName} будут отправлены в кластер. Часть полей Kubernetes может отклонить как неизменяемые.`,
            confirmLabel: 'Сохранить YAML',
            onConfirm: async () => {
              await replace.mutateAsync();
              setMessage('Ресурс Pod сохранён. Kubernetes может отклонить изменения immutable-полей.');
              await detail.refetch();
            }
          })}>Сохранить</button></div><CodeEditor value={resourceText} onChange={setResourceText} /></div>}

          {tab === 'metrics' && <div className={styles.metricsPane}>
            <div className={styles.metricsToolbar}><div><b>Метрики Pod</b><div className={styles.metricSources}><span className={`${styles.metricSource} ${metrics.data?.source === 'metrics-api' ? styles.sourcePrimary : metrics.data?.source === 'kubelet' ? styles.sourceFallback : styles.sourceUnavailable}`}>CPU/RAM: {metrics.data?.source === 'metrics-api' ? 'Metrics API' : metrics.data?.source === 'kubelet' ? 'kubelet fallback' : 'нет данных'}</span><span className={`${styles.metricSource} ${metrics.data?.throttlingAvailable ? styles.sourceSecondary : styles.sourceUnavailable}`}>Throttling: {metrics.data?.throttlingAvailable ? 'cAdvisor' : 'нет данных'}</span>{metrics.data?.window && <span className={styles.metricWindow}>Окно: {metrics.data.window}</span>}</div></div><div className={styles.metricMode}><button className={metricMode === 'percent' ? styles.activeMode : ''} onClick={() => setMetricMode('percent')}>%</button><button className={metricMode === 'actual' ? styles.activeMode : ''} onClick={() => setMetricMode('actual')}>Фактические</button></div></div>
            {metrics.isLoading && <div className={styles.loading}>Загрузка метрик…</div>}
            {!metrics.isLoading && metrics.data?.available && <div className={styles.metrics}>{metrics.data.containers.map((item) => <div className={styles.metricCard} key={item.name}>
              <div className={styles.metricContainerTitle}><b>{item.name}</b><span>{metrics.data?.timestamp ? new Date(metrics.data.timestamp).toLocaleTimeString() : ''}</span></div>
              <MetricChart title="CPU" history={metricHistory} containerName={item.name} value={(containerMetric) => containerMetric.cpu.usageCores} request={item.cpu.requestCores} limit={item.cpu.limitCores} mode={metricMode} formatActual={formatCpu} />
              <MetricChart title="RAM" history={metricHistory} containerName={item.name} value={(containerMetric) => containerMetric.memory.usageBytes} request={item.memory.requestBytes} limit={item.memory.limitBytes} mode={metricMode} formatActual={formatMemory} />
              <MetricChart title="CPU throttling" history={metricHistory} containerName={item.name} value={(containerMetric) => containerMetric.cpuThrottlingPercent} mode={metricMode} formatActual={formatPercent} alwaysPercent />
              {metrics.data?.throttlingAvailable && item.cpuThrottlingPercent === null && <div className={styles.metricNote}>CPU throttling будет рассчитан после следующего обновления метрик.</div>}
              {!metrics.data?.throttlingAvailable && <div className={styles.metricNote}>CPU throttling недоступен: {metrics.data?.throttlingError ?? 'cAdvisor не вернул данные.'}</div>}
            </div>)}</div>}
            {!metrics.isLoading && !metrics.data?.available && <div className={styles.metricUnavailable}>Метрики недоступны: {metrics.data?.error ?? metrics.error?.message ?? 'Нет Metrics API и доступа к kubelet stats/summary.'}</div>}
          </div>}

          {tab === 'events' && <div className={styles.eventsWrap}><table className={styles.events}><thead><tr><th>Тип</th><th>Причина</th><th>Сообщение</th><th>Источник</th><th>Дата</th><th>Кол-во</th></tr></thead><tbody>{events.data?.map((item, index) => <tr key={`${item.reason}-${item.time}-${index}`}><td><span className={`${styles.eventType} ${item.type.toLowerCase() === 'warning' ? styles.eventWarning : styles.eventNormal}`}>{item.type}</span></td><td>{item.reason}</td><td>{item.message}</td><td>{item.source}</td><td>{item.time ? formatDate(item.time) : '—'}</td><td>{item.count}</td></tr>)}</tbody></table>{events.isLoading && <div className={styles.loading}>Загрузка событий…</div>}</div>}

          {tab === 'containers' && <div className={styles.containers}>{data?.containers.map((item) => <div className={styles.containerCard} key={item.name}><div className={styles.containerHeader}><b>{item.name}</b><span className={`${styles.statusBadge} ${item.ready ? styles.running : styles.pending}`}><i />{item.state}</span></div><dl><dt>Образ</dt><dd>{item.image}</dd><dt>Порты</dt><dd>{item.ports.join(', ') || '—'}</dd><dt>CPU</dt><dd>{item.cpuRequest ?? '—'} / {item.cpuLimit ?? '—'}</dd><dt>Память</dt><dd>{item.memoryRequest ?? '—'} / {item.memoryLimit ?? '—'}</dd><dt>Рестарты</dt><dd>{item.restartCount}</dd></dl><div className={styles.containerActions}><button onClick={() => { setContainer(item.name); setTab('logs'); }}>Логи</button><button onClick={() => { setContainer(item.name); setTab('exec'); }}>Консоль Pod</button></div></div>)}</div>}
        </div>
      </aside>
      <ActionConfirmModal request={confirmRequest} onClose={() => setConfirmRequest(null)} />
    </>
  );
}
