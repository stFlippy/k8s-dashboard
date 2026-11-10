import { useQuery } from '@tanstack/react-query';
import { getSystemStatus } from '../../api/auth';
import styles from './SystemStatus.module.css';

const size = (bytes: number) => `${(bytes / 1024 / 1024).toFixed(1)} МиБ`;

export function SystemStatus() {
  const query = useQuery({ queryKey: ['system-status'], queryFn: getSystemStatus, refetchInterval: 15_000 });
  const status = query.data;
  return <section className={styles.root}>
    <h3>Состояние сервера Quaber</h3>
    {query.isPending && <p>Загрузка…</p>}
    {query.isError && <p role="alert" className={styles.error}>Не удалось получить состояние сервера: {query.error.message}</p>}
    {status && <>
      <div className={styles.metrics}>
        <div><span>CPU хоста</span><b>{status.cpuPercent === null ? '—' : `${status.cpuPercent.toFixed(1)}%`}</b></div>
        <div><span>RAM хоста</span><b>{size(status.memoryUsedBytes)} / {size(status.memoryTotalBytes)}</b></div>
        <div><span>Диск хоста</span><b>{size(status.diskUsedBytes)} / {size(status.diskTotalBytes)}</b></div>
        <div><span>SQLite Quaber</span><b>{size(status.databaseBytes)}</b></div>
      </div>
      <p className={styles.hint}>Журнал мутаций хранится {status.auditRetentionDays} дней. Ниже — ваши последние действия; «успешно» означает, что API принял запрос, а не то, что rollout завершён.</p>
      <div className={styles.history}>{status.recent.length ? status.recent.map((item) => <div key={item.id} className={styles.entry}><span>{new Date(item.requested_at).toLocaleString('ru-RU')}</span><b>{item.operation}</b><span>{[item.kind, item.name, item.namespace, item.cluster_id].filter(Boolean).join(' / ')}</span><strong className={item.state === 'failed' ? styles.error : ''}>{item.state}{item.http_status ? ` (${item.http_status})` : ''}</strong></div>) : <p>Действий пока нет.</p>}</div>
    </>}
  </section>;
}
