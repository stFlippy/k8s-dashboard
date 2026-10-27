import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQueries, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  getDeployment,
  getDeploymentEvents,
  getPodMetrics,
  getPods,
  replaceDeploymentResource,
  restartDeployment,
  setDeploymentReplicas,
} from '../../api/kubernetes';
import type { DeploymentContainer, WorkloadRef } from '../../types/kubernetes';
import { ActionConfirmModal } from '../common/ActionConfirmModal';
import type { ActionConfirmRequest } from '../common/ActionConfirmModal';
import { formatDate } from '../../utils/formatDate';
import styles from './DeploymentPage.module.css';
import { CodeEditor } from '../common/CodeEditor';
import type { PodMetrics } from '../../types/kubernetes';
import type { UseQueryResult } from '@tanstack/react-query';

interface Props {
  workload: WorkloadRef;
  onOpenPod: (pod: import('../../types/kubernetes').PodRef) => void;
  onClose: () => void;
}

function age(timestamp: string | null): string {
  if (!timestamp) return '—';
  const diff = Date.now() - new Date(timestamp).getTime();
  const minutes = Math.floor(diff / 60_000);
  if (minutes < 60) return `${minutes} мин`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${hours} ч`;
  return `${Math.floor(hours / 24)} д`;
}

function quantityNumber(value: string | null): number | null {
  if (!value) return null; const match = value.trim().match(/^([0-9.]+)(n|u|m|Ki|Mi|Gi|Ti|k|K|M|G)?$/); if (!match) return null;
  const amount = Number(match[1]); const factors: Record<string, number> = { n: 1e-9, u: 1e-6, m: 1e-3, k: 1e3, K: 1e3, M: 1e6, G: 1e9, Ki: 1024, Mi: 1024 ** 2, Gi: 1024 ** 3, Ti: 1024 ** 4 };
  return Number.isFinite(amount) ? amount * (factors[match[2] ?? ''] ?? 1) : null;
}

function resourcePercent(metrics: PodMetrics | undefined, containers: DeploymentContainer[], resource: 'cpu' | 'memory'): number | null {
  if (!metrics?.available) return null; let used = 0; let limit = 0;
  for (const metric of metrics.containers) { const usage = resource === 'cpu' ? metric.cpu.usageCores : metric.memory.usageBytes; const spec = containers.find((item) => item.name === metric.name); const maximum = resource === 'cpu' ? metric.cpu.limitCores ?? quantityNumber(spec?.cpuLimit ?? null) : metric.memory.limitBytes ?? quantityNumber(spec?.memoryLimit ?? null); if (usage !== null && maximum !== null) { used += usage; limit += maximum; } }
  return limit > 0 ? Math.max(0, (used / limit) * 100) : null;
}

function resourceUnavailableReason(metrics: PodMetrics | undefined, containers: DeploymentContainer[], resource: 'cpu' | 'memory'): string {
  if (!metrics) return 'Метрики pod ещё загружаются.'; if (!metrics.available) return metrics.error ?? 'Metrics Server не вернул данные pod.';
  if (!containers.some((item) => resource === 'cpu' ? item.cpuLimit : item.memoryLimit)) return `Для контейнеров deployment не задан ${resource === 'cpu' ? 'CPU' : 'RAM'} limit.`;
  return 'Недостаточно данных для расчёта процента.';
}
function ResourcePercent({ value, reason }: { value: number | null; reason: string }) { return <span title={value === null ? reason : `Использовано ${Math.round(value)}% от limit`} className={`${styles.resourcePercent} ${value === null ? styles.resourceUnknown : value > 90 ? styles.resourceHigh : value >= 75 ? styles.resourceWarn : styles.resourceOk}`}>{value === null ? '—' : `${Math.round(value)}%`}</span>; }

export function DeploymentPage({ workload, onOpenPod, onClose }: Props) {
  const client = useQueryClient();
  const [message, setMessage] = useState('');
  const [confirmRequest, setConfirmRequest] = useState<ActionConfirmRequest | null>(null);
  const [yamlOpen, setYamlOpen] = useState(false);
  const detail = useQuery({
    queryKey: ['deployment-detail', workload.clusterId, workload.namespace, workload.deploymentName],
    queryFn: () => getDeployment(workload.clusterId, workload.namespace, workload.deploymentName),
    refetchInterval: 5_000,
    refetchOnWindowFocus: true
  });
  const pods = useQuery({
    queryKey: ['pods', workload.clusterId, workload.namespace],
    queryFn: () => getPods(workload.clusterId, workload.namespace),
    refetchInterval: 5_000,
    refetchOnWindowFocus: true
  });
  const deploymentEvents = useQuery({
    queryKey: ['deployment-events', workload.clusterId, workload.namespace, workload.deploymentName],
    queryFn: () => getDeploymentEvents(workload.clusterId, workload.namespace, workload.deploymentName),
    refetchInterval: 10_000,
    refetchOnWindowFocus: true
  });
  const deploymentPods = useMemo(() => (pods.data ?? []).filter((pod) => pod.name.startsWith(`${workload.deploymentName}-`)), [pods.data, workload.deploymentName]);
  const podMetrics = useQueries({ queries: deploymentPods.map((pod) => ({ queryKey: ['pod-metrics', workload.clusterId, workload.namespace, pod.name], queryFn: () => getPodMetrics(workload.clusterId, workload.namespace, pod.name), refetchInterval: 5_000 })) }) as UseQueryResult<PodMetrics, Error>[];
  const [replicas, setReplicas] = useState(0);
  const [resourceText, setResourceText] = useState('');

  useEffect(() => {
    if (detail.data) {
      setReplicas(detail.data.replicas);
      if (!yamlOpen) setResourceText(JSON.stringify(detail.data.resource, null, 2));
    }
  }, [detail.data, yamlOpen]);

  const refresh = async () => {
    await Promise.all([
      client.invalidateQueries({ queryKey: ['deployment-detail', workload.clusterId, workload.namespace, workload.deploymentName] }),
      client.invalidateQueries({ queryKey: ['deployments', workload.clusterId, workload.namespace] }),
      client.invalidateQueries({ queryKey: ['pods', workload.clusterId, workload.namespace] })
    ]);
  };

  const scale = useMutation({ mutationFn: (value: number) => setDeploymentReplicas(workload.clusterId, workload.namespace, workload.deploymentName, value), onSuccess: refresh });
  const restart = useMutation({ mutationFn: () => restartDeployment(workload.clusterId, workload.namespace, workload.deploymentName), onSuccess: refresh });
  const replace = useMutation({
    mutationFn: async () => {
      let parsed: unknown;
      try {
        parsed = JSON.parse(resourceText);
      } catch {
        throw new Error('Редактор использует JSON-синтаксис. Не удалось распознать JSON.');
      }
      return replaceDeploymentResource(workload.clusterId, workload.namespace, workload.deploymentName, parsed);
    },
    onSuccess: async () => {
      setMessage('Ресурс deployment сохранён.');
      await refresh();
    }
  });

  if (detail.isLoading) return <div className={styles.state}>Загрузка deployment…</div>;
  if (detail.isError || !detail.data) return <div className={styles.stateError}>Не удалось загрузить deployment: {detail.error?.message}</div>;
  const data = detail.data;
  const confirmationTarget = { cluster: workload.clusterName, namespace: workload.namespace };

  return (
    <><div className={styles.drawerOverlay} onClick={onClose} /><aside className={styles.root}>
      <div className={styles.header}>
        <div>
          <h2>{data.name}</h2>
          <p>Кластер: <b>{workload.clusterName}</b> · Namespace: <b>{workload.namespace}</b></p>
        </div>
        <div className={styles.actions}>
          <button onClick={() => setYamlOpen(true)}>YAML</button>
          <button onClick={() => setConfirmRequest({
            title: 'Reload pods',
            description: `Для Deployment ${data.name} будет запрошен rolling restart всех Pods.`,
            confirmLabel: 'Выполнить reload pods',
            target: confirmationTarget,
            onConfirm: async () => {
              await restart.mutateAsync();
              setMessage('Запрошен rolling restart Pods.');
            }
          })}>Reload pods</button>
          <button className={styles.close} onClick={onClose} aria-label="Закрыть" title="Закрыть">×</button>
        </div>
      </div>

      {message && <div className={styles.message}>{message}</div>}

      <div className={styles.stats}>
        <div><strong>{data.availableReplicas}/{data.replicas}</strong><span>Pods</span></div>
        <div><strong>{age(data.creationTimestamp)}</strong><span>Возраст</span></div>
        <div title={data.image}><strong className={styles.imageValue}>{data.image || '—'}</strong><span>Образ</span></div>
      </div>

      <section className={styles.section}>
        <div className={styles.sectionHeader}>
          <h3>Реплики</h3>
          <div className={styles.replicaControl}>
            <button onClick={() => setReplicas((value) => Math.max(0, value - 1))}>−</button>
            <input type="number" min="0" max="10000" step="1" value={replicas} onKeyDown={(event) => { if (['e','E','+','-','.'].includes(event.key)) event.preventDefault(); }} onChange={(event) => { if (/^\d{0,5}$/.test(event.target.value)) setReplicas(Math.min(10000, Math.max(0, Number(event.target.value) || 0))); }} />
            <button onClick={() => setReplicas((value) => value + 1)}>+</button>
            <button
              className={styles.apply}
              disabled={replicas === data.replicas || scale.isPending}
              onClick={() => setConfirmRequest({
                title: 'Изменение количества реплик',
                description: `Количество реплик для ${data.name} изменится с ${data.replicas} до ${replicas}.`,
                confirmLabel: 'Изменить реплики',
                target: confirmationTarget,
                onConfirm: async () => {
                  await scale.mutateAsync(replicas);
                  setMessage(`Количество реплик изменено: ${replicas}.`);
                }
              })}
            >Применить</button>
          </div>
        </div>
        <div className={styles.sectionBody}>
          <table className={styles.podTable}>
            <thead><tr><th>Статус</th><th>Имя</th><th>Фаза</th><th>Возраст</th><th>IP</th><th>CPU</th><th>RAM</th><th>Действия</th></tr></thead>
            <tbody>{deploymentPods.map((pod, podIndex) => (
              <tr key={pod.name} onClick={() => onOpenPod({ clusterId: workload.clusterId, clusterName: workload.clusterName, namespace: workload.namespace, podName: pod.name, deploymentName: data.name })}>
                <td><span className={`${styles.podDot} ${styles[pod.status.toLowerCase()]}`} /></td>
                <td className={styles.mono}>{pod.name}</td><td>{pod.status}</td><td>{age(pod.creationTimestamp)}</td><td>{pod.ip ?? '—'}</td><td><ResourcePercent value={resourcePercent(podMetrics[podIndex]?.data, data.containers, 'cpu')} reason={resourceUnavailableReason(podMetrics[podIndex]?.data, data.containers, 'cpu')} /></td><td><ResourcePercent value={resourcePercent(podMetrics[podIndex]?.data, data.containers, 'memory')} reason={resourceUnavailableReason(podMetrics[podIndex]?.data, data.containers, 'memory')} /></td>
                <td><button className={styles.smallButton} onClick={(event) => { event.stopPropagation(); onOpenPod({ clusterId: workload.clusterId, clusterName: workload.clusterName, namespace: workload.namespace, podName: pod.name, deploymentName: data.name }); }}>Открыть</button></td>
              </tr>
            ))}</tbody>
          </table>
          {!deploymentPods.length && <div className={styles.empty}>Для этого Deployment Pods не найдены.</div>}
        </div>
      </section>

      <section className={styles.section}>
        <div className={styles.sectionHeader}><h3>События Deployment / ReplicaSet</h3></div>
        <div className={styles.sectionBody}>
          {deploymentEvents.isLoading && <div className={styles.empty}>Загрузка событий…</div>}
          {deploymentEvents.isError && <div className={styles.eventError}>Не удалось загрузить события: {deploymentEvents.error?.message}</div>}
          {!deploymentEvents.isLoading && !deploymentEvents.isError && (
            <table className={styles.eventTable}>
              <thead><tr><th>Объект</th><th>Тип</th><th>Причина</th><th>Сообщение</th><th>Источник</th><th>Дата</th><th>Кол-во</th></tr></thead>
              <tbody>
                {(deploymentEvents.data ?? []).map((event, index) => (
                  <tr key={`${event.objectKind}/${event.objectName}/${event.reason}/${event.time ?? index}`}>
                    <td className={styles.mono}>{event.objectKind}/{event.objectName}</td>
                    <td><span className={`${styles.eventBadge} ${event.type.toLowerCase() === 'warning' ? styles.eventWarning : styles.eventNormal}`}>{event.type}</span></td>
                    <td>{event.reason}</td>
                    <td>{event.message}</td>
                    <td>{event.source || '—'}</td>
                    <td>{event.time ? formatDate(event.time) : '—'}</td>
                    <td>{event.count}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {!deploymentEvents.isLoading && !deploymentEvents.isError && !(deploymentEvents.data ?? []).length && <div className={styles.empty}>События для Deployment и связанных ReplicaSet не найдены.</div>}
        </div>
      </section>

      {yamlOpen && <div className={styles.yamlModalOverlay} onMouseDown={(event) => event.target === event.currentTarget && setYamlOpen(false)}><div className={styles.yamlModal}><div className={styles.modalHeader}><h2>YAML — {data.name}</h2><div><button className={styles.primary} onClick={() => setConfirmRequest({ title: 'Сохранение YAML', description: `Изменения YAML для deployment ${data.name} будут отправлены в кластер.`, confirmLabel: 'Сохранить YAML', target: confirmationTarget, onConfirm: async () => { await replace.mutateAsync(); setYamlOpen(false); } })}>Сохранить</button><button className={styles.modalClose} onClick={() => setYamlOpen(false)} aria-label="Закрыть" title="Закрыть">×</button></div></div><div className={styles.yamlModalBody}><CodeEditor value={resourceText} onChange={setResourceText} /></div></div></div>}

      <ActionConfirmModal request={confirmRequest} onClose={() => setConfirmRequest(null)} />
    </aside></>
  );
}
