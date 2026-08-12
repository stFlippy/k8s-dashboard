import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  getDeployment,
  getDeploymentEvents,
  getPods,
  removeDeployment,
  replaceDeploymentResource,
  restartDeployment,
  setDeploymentReplicas,
  updateDeploymentResources
} from '../../api/kubernetes';
import type { DeploymentContainer, WorkloadRef } from '../../types/kubernetes';
import { ActionConfirmModal } from '../common/ActionConfirmModal';
import type { ActionConfirmRequest } from '../common/ActionConfirmModal';
import { formatDate } from '../../utils/formatDate';
import { loadRestartHistory, restartCountLastHour, saveRestartHistory, updateRestartHistory } from '../../utils/restartHistory';
import type { RestartHistory } from '../../utils/restartHistory';
import styles from './DeploymentPage.module.css';

interface Props {
  workload: WorkloadRef;
  onOpenPod: (pod: import('../../types/kubernetes').PodRef) => void;
  onDeleted: () => void;
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

function ResourceModal({ container, onClose, onApply }: { container: DeploymentContainer; onClose: () => void; onApply: (values: DeploymentContainer) => void; }) {
  const [values, setValues] = useState(container);
  const set = (key: keyof DeploymentContainer, value: string) => setValues((current) => ({ ...current, [key]: value || null }));
  return (
    <div className={styles.modalOverlay} onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <div className={styles.modal}>
        <div className={styles.modalHeader}><h2>Изменение ресурсов — {container.name}</h2><button onClick={onClose}>Закрыть</button></div>
        <div className={styles.modalBody}>
          <label>CPU request<input value={values.cpuRequest ?? ''} onChange={(event) => set('cpuRequest', event.target.value)} placeholder="250m" /></label>
          <label>CPU limit<input value={values.cpuLimit ?? ''} onChange={(event) => set('cpuLimit', event.target.value)} placeholder="500m" /></label>
          <label>Memory request<input value={values.memoryRequest ?? ''} onChange={(event) => set('memoryRequest', event.target.value)} placeholder="128Mi" /></label>
          <label>Memory limit<input value={values.memoryLimit ?? ''} onChange={(event) => set('memoryLimit', event.target.value)} placeholder="256Mi" /></label>
        </div>
        <div className={styles.modalFooter}><button onClick={onClose}>Отмена</button><button className={styles.primary} onClick={() => onApply(values)}>Сохранить</button></div>
      </div>
    </div>
  );
}

export function DeploymentPage({ workload, onOpenPod, onDeleted }: Props) {
  const client = useQueryClient();
  const [message, setMessage] = useState('');
  const [resourceContainer, setResourceContainer] = useState<DeploymentContainer | null>(null);
  const [confirmRequest, setConfirmRequest] = useState<ActionConfirmRequest | null>(null);
  const [restartHistory, setRestartHistory] = useState<RestartHistory>(() => loadRestartHistory());
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
  useEffect(() => {
    setRestartHistory((current) => {
      const next = updateRestartHistory(current, deploymentPods.map((pod) => ({ clusterId: workload.clusterId, namespace: workload.namespace, pod })));
      if (JSON.stringify(current) === JSON.stringify(next)) return current;
      saveRestartHistory(next);
      return next;
    });
  }, [pods.dataUpdatedAt, workload.clusterId, workload.namespace]);
  const [replicas, setReplicas] = useState(0);
  const [resourceText, setResourceText] = useState('');

  useEffect(() => {
    if (detail.data) {
      setReplicas(detail.data.replicas);
      setResourceText(JSON.stringify(detail.data.resource, null, 2));
    }
  }, [detail.data]);

  const refresh = async () => {
    await Promise.all([
      client.invalidateQueries({ queryKey: ['deployment-detail', workload.clusterId, workload.namespace, workload.deploymentName] }),
      client.invalidateQueries({ queryKey: ['deployments', workload.clusterId, workload.namespace] }),
      client.invalidateQueries({ queryKey: ['pods', workload.clusterId, workload.namespace] })
    ]);
  };

  const scale = useMutation({ mutationFn: (value: number) => setDeploymentReplicas(workload.clusterId, workload.namespace, workload.deploymentName, value), onSuccess: refresh });
  const restart = useMutation({ mutationFn: () => restartDeployment(workload.clusterId, workload.namespace, workload.deploymentName), onSuccess: refresh });
  const remove = useMutation({ mutationFn: () => removeDeployment(workload.clusterId, workload.namespace, workload.deploymentName) });
  const resources = useMutation({
    mutationFn: (container: DeploymentContainer) => updateDeploymentResources(workload.clusterId, workload.namespace, workload.deploymentName, {
      containerName: container.name,
      cpuRequest: container.cpuRequest,
      cpuLimit: container.cpuLimit,
      memoryRequest: container.memoryRequest,
      memoryLimit: container.memoryLimit
    }),
    onSuccess: async () => {
      setResourceContainer(null);
      setMessage('Ресурсы обновлены. Kubernetes начинает rollout нового шаблона подов.');
      await refresh();
    }
  });
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

  return (
    <div className={styles.root}>
      <div className={styles.header}>
        <div>
          <h2>{data.name}</h2>
          <p>Кластер: <b>{workload.clusterName}</b> · Namespace: <b>{workload.namespace}</b></p>
        </div>
        <div className={styles.actions}>
          {data.containers[0] && <button className={styles.primary} onClick={() => setResourceContainer(data.containers[0])}>Изменить ресурсы</button>}
          <button onClick={() => setConfirmRequest({
            title: 'Reload pods',
            description: `Для deployment ${data.name} будет запрошен rolling restart всех подов.`,
            confirmLabel: 'Выполнить reload pods',
            onConfirm: async () => {
              await restart.mutateAsync();
              setMessage('Запрошен rolling restart подов.');
            }
          })}>Reload pods</button>
          <button className={styles.danger} onClick={() => setConfirmRequest({
            title: 'Удаление deployment',
            description: `Deployment ${data.name} будет удалён из namespace ${workload.namespace}.`,
            confirmLabel: 'Удалить deployment',
            onConfirm: async () => {
              await remove.mutateAsync();
              await client.invalidateQueries({ queryKey: ['deployments', workload.clusterId, workload.namespace] });
              onDeleted();
            }
          })}>Удалить</button>
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
            <input type="number" min="0" value={replicas} onChange={(event) => setReplicas(Math.max(0, Number(event.target.value) || 0))} />
            <button onClick={() => setReplicas((value) => value + 1)}>+</button>
            <button
              className={styles.apply}
              disabled={replicas === data.replicas || scale.isPending}
              onClick={() => setConfirmRequest({
                title: 'Изменение количества реплик',
                description: `Количество реплик для ${data.name} изменится с ${data.replicas} до ${replicas}.`,
                confirmLabel: 'Изменить реплики',
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
            <thead><tr><th>Статус</th><th>Имя</th><th>Фаза</th><th>Возраст</th><th>Restarts &lt; 1h</th><th>IP</th><th>Хост</th><th>Действия</th></tr></thead>
            <tbody>{deploymentPods.map((pod) => (
              <tr key={pod.name} onClick={() => onOpenPod({ clusterId: workload.clusterId, clusterName: workload.clusterName, namespace: workload.namespace, podName: pod.name, deploymentName: data.name })}>
                <td><span className={`${styles.podDot} ${styles[pod.status.toLowerCase()]}`} /></td>
                <td className={styles.mono}>{pod.name}</td><td>{pod.status}</td><td>{age(pod.creationTimestamp)}</td><td>{restartCountLastHour(restartHistory, workload.clusterId, workload.namespace, pod.name)}</td><td>{pod.ip ?? '—'}</td><td>{pod.host ?? '—'}</td>
                <td><button className={styles.smallButton} onClick={(event) => { event.stopPropagation(); onOpenPod({ clusterId: workload.clusterId, clusterName: workload.clusterName, namespace: workload.namespace, podName: pod.name, deploymentName: data.name }); }}>Открыть</button></td>
              </tr>
            ))}</tbody>
          </table>
          {!deploymentPods.length && <div className={styles.empty}>Для этого deployment поды не найдены.</div>}
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

      <section className={styles.section}>
        <div className={styles.sectionHeader}><h3>YAML</h3><button onClick={() => setConfirmRequest({
          title: 'Сохранение YAML',
          description: `Изменения YAML для deployment ${data.name} будут отправлены в кластер.`,
          confirmLabel: 'Сохранить YAML',
          onConfirm: async () => {
            await replace.mutateAsync();
          }
        })}>Сохранить</button></div>
        <div className={styles.sectionBody}>
          <div className={styles.editorNote}>Без дополнительной YAML-библиотеки редактор использует форматированный JSON. JSON корректен как YAML 1.2.</div>
          <textarea className={styles.yaml} spellCheck={false} value={resourceText} onChange={(event) => setResourceText(event.target.value)} />
        </div>
      </section>

      {resourceContainer && <ResourceModal container={resourceContainer} onClose={() => setResourceContainer(null)} onApply={(value) => {
        setResourceContainer(null);
        setConfirmRequest({
          title: 'Изменение ресурсов контейнера',
          description: `Ресурсы контейнера ${value.name} будут обновлены. Это приведёт к перезапуску подов deployment ${data.name}.`,
          confirmLabel: 'Обновить ресурсы',
          onConfirm: async () => {
            await resources.mutateAsync(value);
          }
        });
      }} />}

      <ActionConfirmModal request={confirmRequest} onClose={() => setConfirmRequest(null)} />
    </div>
  );
}
