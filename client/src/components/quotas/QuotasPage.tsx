import { useMemo } from 'react';
import { useQueries } from '@tanstack/react-query';
import type { UseQueryResult } from '@tanstack/react-query';
import { getResourceQuotas } from '../../api/kubernetes';
import { useClusterNamespaces } from '../../hooks/useClusterNamespaces';
import styles from './QuotasPage.module.css';

function quantityNumber(value: string): number | null {
  const match = value.trim().match(/^([+-]?(?:\d+(?:\.\d*)?|\.\d+))(?:([eE][+-]?\d+)|([numkKMGTEP]|[KMGTPE]i))?$/);
  if (!match) return null;
  const amount = Number(match[1]);
  if (!Number.isFinite(amount)) return null;
  if (match[2]) return amount * 10 ** Number(match[2].slice(1));
  const suffix = match[3] ?? '';
  const decimal: Record<string, number> = { n: 1e-9, u: 1e-6, m: 1e-3, k: 1e3, K: 1e3, M: 1e6, G: 1e9, T: 1e12, P: 1e15, E: 1e18 };
  const binary: Record<string, number> = { Ki: 1024, Mi: 1024 ** 2, Gi: 1024 ** 3, Ti: 1024 ** 4, Pi: 1024 ** 5, Ei: 1024 ** 6 };
  return amount * (binary[suffix] ?? decimal[suffix] ?? 1);
}

function percent(used: string, hard: string): number | null {
  const u = quantityNumber(used);
  const h = quantityNumber(hard);
  if (u === null || h === null || h <= 0) return null;
  return Math.max(0, Math.min(100, (u / h) * 100));
}

export function QuotasPage() {
  const { clustersLoading, clustersError, groups } = useClusterNamespaces();
  const targets = useMemo(() => groups.flatMap((group) => group.namespaces.map((namespace) => ({
    clusterId: group.cluster.id,
    clusterName: group.cluster.name,
    namespace: namespace.name
  }))), [groups]);

  const quotaQueries = useQueries({
    queries: targets.map((target) => ({
      queryKey: ['resource-quotas', target.clusterId, target.namespace],
      queryFn: () => getResourceQuotas(target.clusterId, target.namespace),
      refetchInterval: 10_000,
      refetchOnWindowFocus: true
    }))
  }) as UseQueryResult<import('../../types/kubernetes').ResourceQuota[], Error>[];

  const sections = useMemo(() => groups.map((group) => ({
    cluster: group.cluster,
    namespaces: group.namespaces.map((namespace) => {
      const index = targets.findIndex((target) => target.clusterId === group.cluster.id && target.namespace === namespace.name);
      return { namespace: namespace.name, quotas: quotaQueries[index]?.data ?? [], error: quotaQueries[index]?.error ?? null };
    })
  })), [groups, targets, quotaQueries]);

  if (clustersLoading) return <div className={styles.state}>Загрузка кластеров…</div>;
  if (clustersError) return <div className={styles.error}>{clustersError.message}</div>;

  const hasQuotas = sections.some((section) => section.namespaces.some((namespace) => namespace.quotas.length > 0));
  const quotaErrors = sections.flatMap((section) => section.namespaces.filter((namespace) => namespace.error).map((namespace) => `${section.cluster.name}/${namespace.namespace}: ${namespace.error?.message}`));
  const quotasLoading = quotaQueries.some((query) => query.isLoading);

  return (
    <section className={styles.root}>
      <div className={styles.header}>
        <div><h2>Квоты ресурсов</h2><p>Администрирование → квоты ресурсов</p></div>
      </div>
      <div className={styles.content}>
        {quotaErrors.length > 0 && <div className={styles.error}>Не удалось прочитать часть ResourceQuota: {quotaErrors.join(' · ')}</div>}
        {sections.map((section, clusterIndex) => (
          <div className={styles.clusterSection} key={section.cluster.id}>
            <div className={`${styles.clusterTitle} ${styles[`tone${clusterIndex % 4}`]}`}>
              <span>{section.cluster.name}</span><small>{section.namespaces.length} namespace</small>
            </div>
            {section.namespaces.map((namespace) => namespace.quotas.map((quota) => {
              const keys = [...new Set([...Object.keys(quota.hard), ...Object.keys(quota.used)])].sort();
              return (
                <article className={styles.card} key={`${namespace.namespace}/${quota.name}`}>
                  <div className={styles.cardHeader}>
                    <div><h3>{quota.name}</h3><span>{namespace.namespace}</span></div>
                    <small>Жёсткие лимиты</small>
                  </div>
                  <div className={styles.cardBody}>
                    {keys.map((key) => {
                      const hard = quota.hard[key] ?? '—';
                      const used = quota.used[key] ?? '0';
                      const value = hard === '—' ? null : percent(used, hard);
                      return (
                        <div className={styles.quotaRow} key={key}>
                          <span className={styles.label} title={key}>{key}</span>
                          <div className={styles.barWrap}>
                            <div className={styles.barBg}>{value !== null && <div className={`${styles.barFill} ${value >= 90 ? styles.over : value >= 70 ? styles.warn : styles.ok}`} style={{ width: `${value}%` }} />}</div>
                          </div>
                          <div className={styles.values}><b>{used}</b><span>/ {hard}</span></div>
                        </div>
                      );
                    })}
                  </div>
                </article>
              );
            }))}
          </div>
        ))}
        {!hasQuotas && quotasLoading && <div className={styles.empty}>Поиск ResourceQuota во всех namespace…</div>}
        {!hasQuotas && !quotasLoading && !quotaErrors.length && <div className={styles.empty}>В подключённых namespace объекты ResourceQuota не найдены. Эта страница показывает только созданные Kubernetes-объекты ResourceQuota, а не общую ёмкость нод.</div>}
      </div>
    </section>
  );
}
