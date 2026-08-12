import { useEffect, useMemo, useRef, useState } from 'react';
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
import type { PodRef } from '../../types/kubernetes';
import { ActionConfirmModal } from '../common/ActionConfirmModal';
import type { ActionConfirmRequest } from '../common/ActionConfirmModal';
import { formatDate } from '../../utils/formatDate';
import { loadRestartHistory, restartCountLastHour, saveRestartHistory, updateRestartHistory } from '../../utils/restartHistory';
import type { RestartHistory } from '../../utils/restartHistory';
import styles from './PodPanel.module.css';

type Tab = 'logs' | 'exec' | 'describe' | 'yaml' | 'metrics' | 'events' | 'containers';
interface Props { pod: PodRef | null; onClose: () => void; }

function age(timestamp: string | null): string {
  if (!timestamp) return '—';
  const minutes = Math.floor(Math.max(0, Date.now() - new Date(timestamp).getTime()) / 60_000);
  if (minutes < 60) return `${minutes} мин`;
  const hours = Math.floor(minutes / 60);
  return hours < 48 ? `${hours} ч ${minutes % 60} мин` : `${Math.floor(hours / 24)} д`;
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
  const [yamlSearch, setYamlSearch] = useState('');
  const [message, setMessage] = useState('');
  const [consoleUnlockedUntil, setConsoleUnlockedUntil] = useState(0);
  const [confirmRequest, setConfirmRequest] = useState<ActionConfirmRequest | null>(null);
  const [restartHistory, setRestartHistory] = useState<RestartHistory>(() => loadRestartHistory());
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
    }
  }, [pod]);

  useEffect(() => {
    const currentDetail = detail.data;
    if (currentDetail) {
      setContainer((current) => current && currentDetail.containers.some((item) => item.name === current) ? current : currentDetail.containers[0]?.name ?? '');
      setResourceText(JSON.stringify(currentDetail.resource, null, 2));
      setRestartHistory((current) => {
        const next = updateRestartHistory(current, [{ clusterId: key[0], namespace: key[1], pod: currentDetail }]);
        if (JSON.stringify(current) === JSON.stringify(next)) return current;
        saveRestartHistory(next);
        return next;
      });
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
    if (!consoleUnlockedUntil) return;
    const remaining = consoleUnlockedUntil - Date.now();
    if (remaining <= 0) {
      setConsoleUnlockedUntil(0);
      return;
    }
    const timer = window.setTimeout(() => setConsoleUnlockedUntil(0), remaining);
    return () => window.clearTimeout(timer);
  }, [consoleUnlockedUntil]);

  const filteredResourceText = useMemo(() => resourceText, [resourceText]);

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
      title: 'Разблокировка консоли пода',
      description: `После проверки команды можно выполнять без повторного подтверждения. Консоль автоматически заблокируется после 5 минут простоя. Первая команда: «${value}».`,
      confirmLabel: 'Разблокировать и выполнить',
      onConfirm: async () => { setConsoleUnlockedUntil(Date.now() + 5 * 60_000); await executeNow(value); }
    });
  };

  return (
    <>
      <div className={styles.overlay} onClick={onClose} />
      <aside className={styles.panel}>
        <div className={styles.header}>
          <div className={styles.title}><button onClick={onClose}>Назад</button><h2>{pod.podName}</h2></div>
          <button className={styles.close} onClick={onClose}>Закрыть</button>
        </div>

        <div className={styles.summary}>
          {detail.isLoading && <div className={styles.loading}>Загрузка пода…</div>}
          {detail.isError && <div className={styles.error}>Не удалось загрузить под: {detail.error?.message}</div>}
          {data && <>
            <div className={styles.badges}><span className={`${styles.statusBadge} ${statusClass}`}><i />{data.status}</span><span>Кластер: <b>{pod.clusterName}</b> · NS: <b>{pod.namespace}</b></span></div>
            <div className={styles.infoGrid}>
              <div><span>IP пода</span><b>{data.ip ?? '—'}</b></div><div><span>Кластер</span><b>{pod.clusterName}</b></div><div><span>Restarts &lt; 1h</span><b>{restartCountLastHour(restartHistory, pod.clusterId, pod.namespace, pod.podName)}</b></div><div><span>Возраст</span><b>{age(data.creationTimestamp)}</b></div><div><span>Namespace</span><b>{pod.namespace}</b></div>
            </div>
            <div className={styles.actions}>
              <button onClick={() => setConfirmRequest({
                title: 'Перезапуск пода',
                description: `Под ${pod.podName} будет удалён, после чего контроллер сможет создать новый экземпляр.`,
                confirmLabel: 'Перезапустить под',
                onConfirm: async () => {
                  await remove.mutateAsync();
                  await queryClient.invalidateQueries({ queryKey: ['pods', pod.clusterId, pod.namespace] });
                  onClose();
                }
              })}>Перезапустить</button>
              <button className={styles.danger} onClick={() => setConfirmRequest({
                title: 'Удаление пода',
                description: `Под ${pod.podName} будет удалён из namespace ${pod.namespace}.`,
                confirmLabel: 'Удалить под',
                onConfirm: async () => {
                  await remove.mutateAsync();
                  await queryClient.invalidateQueries({ queryKey: ['pods', pod.clusterId, pod.namespace] });
                  onClose();
                }
              })}>Удалить</button>
            </div>
          </>}
          {message && <div className={styles.message}>{message}</div>}
        </div>

        <nav className={styles.tabs}>
          {(['logs','exec','describe','yaml','metrics','events','containers'] as Tab[]).map((item) => (
            <button key={item} className={tab === item ? styles.active : ''} onClick={() => setTab(item)}>
              {item === 'logs' ? 'Логи' : item === 'exec' ? 'Консоль пода' : item === 'describe' ? 'Статус пода' : item === 'yaml' ? 'YAML' : item === 'metrics' ? 'Метрики' : item === 'events' ? `События${events.data?.length ? ` ${events.data.length}` : ''}` : 'Контейнеры'}
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
              <div><button onClick={() => navigator.clipboard.writeText(logs.data ?? '')}>Копировать</button></div>
            </div>
            {showPreviousLogs && <div className={styles.previousLogsBanner}>Лог предыдущего запуска</div>}
            <pre className={styles.logs}>{logs.isLoading ? 'Загрузка логов…' : logs.isError ? `Ошибка: ${logs.error?.message}` : logs.data || '(лог пуст)'}</pre>
          </div>}

          {tab === 'exec' && <div className={styles.terminal}><div className={styles.terminalToolbar}><div><select value={container} onChange={(event) => setContainer(event.target.value)}>{data?.containers.map((item) => <option key={item.name}>{item.name}</option>)}</select><span>/bin/sh -lc</span></div><button className={consoleUnlocked ? styles.consoleUnlocked : styles.consoleLocked} title={consoleUnlocked ? 'Консоль разблокирована. Нажмите, чтобы заблокировать.' : 'Консоль заблокирована'} aria-label={consoleUnlocked ? 'Заблокировать консоль' : 'Консоль заблокирована'} onClick={() => setConsoleUnlockedUntil(0)}>{consoleUnlocked ? '🔓' : '🔒'}</button></div><div ref={terminalBodyRef} className={styles.terminalBody} onClick={() => { if (window.getSelection()?.isCollapsed !== false) consoleInputRef.current?.focus(); }}>{terminal.map((item, index) => <div key={index}><div><span className={styles.prompt}>{container}@{pod.podName}:~$</span> {item.command}</div><pre className={item.error ? styles.terminalError : ''}>{item.output}</pre></div>)}<div className={styles.commandLine}><span className={styles.prompt}>{container || 'container'}@{pod.podName}:~$</span><input ref={consoleInputRef} value={command} onChange={(event) => setCommand(event.target.value)} onKeyDown={(event) => event.key === 'Enter' && requestExecute()} autoFocus /></div></div></div>}

          {tab === 'describe' && data && <div>
            <section className={styles.describe}><h3>Статус пода</h3><dl><dt>Имя</dt><dd>{data.name}</dd><dt>Namespace</dt><dd>{data.namespace}</dd><dt>Кластер</dt><dd>{pod.clusterName}</dd><dt>Статус</dt><dd>{data.status}</dd><dt>IP</dt><dd>{data.ip ?? '—'}</dd></dl></section>
            <section className={styles.describe}><h3>Состояния</h3>{data.conditions.map((item) => <div className={styles.condition} key={item.type}><b>{item.type}:</b><span className={item.status === 'True' ? styles.good : ''}>{item.status}</span><span>{item.reason ?? ''}</span><span>{item.message ?? ''}</span></div>)}</section>
          </div>}

          {tab === 'yaml' && <div className={styles.tabFill}><div className={styles.yamlToolbar}><input value={yamlSearch} onChange={(event) => { setYamlSearch(event.target.value); const pos = resourceText.toLowerCase().indexOf(event.target.value.toLowerCase()); const el = document.getElementById('pod-resource-editor') as HTMLTextAreaElement | null; if (el && pos >= 0) { el.focus(); el.setSelectionRange(pos, pos + event.target.value.length); } }} placeholder="Поиск по YAML…" /><button onClick={() => setConfirmRequest({
            title: 'Сохранение YAML',
            description: `Изменения YAML пода ${pod.podName} будут отправлены в кластер. Часть полей Kubernetes может отклонить как неизменяемые.`,
            confirmLabel: 'Сохранить YAML',
            onConfirm: async () => {
              await replace.mutateAsync();
              setMessage('Ресурс пода сохранён. Kubernetes может отклонить изменения immutable-полей.');
              await detail.refetch();
            }
          })}>Сохранить</button></div><div className={styles.editorNote}>Редактор использует форматированный JSON, потому что внешняя YAML-библиотека не подключена. JSON валиден как YAML 1.2.</div><textarea id="pod-resource-editor" className={styles.yaml} spellCheck={false} value={filteredResourceText} onChange={(event) => setResourceText(event.target.value)} /></div>}

          {tab === 'metrics' && <div className={styles.metrics}>{metrics.isLoading ? <div>Загрузка метрик…</div> : metrics.data?.available ? metrics.data.containers.map((item) => <div className={styles.metricCard} key={item.name}><div><span>{item.name}</span><b>{item.cpu}</b></div><p>CPU</p><div><span>Память</span><b>{item.memory}</b></div><p>Окно метрик {metrics.data?.window ?? '—'}</p></div>) : <div className={styles.metricUnavailable}>Метрики недоступны: {metrics.data?.error ?? metrics.error?.message ?? 'metrics-server может быть не установлен.'}</div>}</div>}

          {tab === 'events' && <div className={styles.eventsWrap}><table className={styles.events}><thead><tr><th>Тип</th><th>Причина</th><th>Сообщение</th><th>Источник</th><th>Дата</th><th>Кол-во</th></tr></thead><tbody>{events.data?.map((item, index) => <tr key={`${item.reason}-${item.time}-${index}`}><td><span className={`${styles.eventType} ${item.type.toLowerCase() === 'warning' ? styles.eventWarning : styles.eventNormal}`}>{item.type}</span></td><td>{item.reason}</td><td>{item.message}</td><td>{item.source}</td><td>{item.time ? formatDate(item.time) : '—'}</td><td>{item.count}</td></tr>)}</tbody></table>{events.isLoading && <div className={styles.loading}>Загрузка событий…</div>}</div>}

          {tab === 'containers' && <div className={styles.containers}>{data?.containers.map((item) => <div className={styles.containerCard} key={item.name}><div className={styles.containerHeader}><b>{item.name}</b><span className={`${styles.statusBadge} ${item.ready ? styles.running : styles.pending}`}><i />{item.state}</span></div><dl><dt>Образ</dt><dd>{item.image}</dd><dt>Порты</dt><dd>{item.ports.join(', ') || '—'}</dd><dt>CPU</dt><dd>{item.cpuRequest ?? '—'} / {item.cpuLimit ?? '—'}</dd><dt>Память</dt><dd>{item.memoryRequest ?? '—'} / {item.memoryLimit ?? '—'}</dd><dt>Рестарты</dt><dd>{item.restartCount}</dd></dl><div className={styles.containerActions}><button onClick={() => { setContainer(item.name); setTab('logs'); }}>Логи</button><button onClick={() => { setContainer(item.name); setTab('exec'); }}>Консоль пода</button></div></div>)}</div>}
        </div>
      </aside>
      <ActionConfirmModal request={confirmRequest} onClose={() => setConfirmRequest(null)} />
    </>
  );
}
