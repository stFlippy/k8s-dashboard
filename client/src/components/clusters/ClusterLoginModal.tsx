import { useMemo, useState } from 'react';
import type { FormEvent } from 'react';
import type { Cluster } from '../../types/kubernetes';
import { useClusterMutations } from '../../hooks/useKubernetes';
import styles from './ClusterLoginModal.module.css';

interface Props {
  clusters: Cluster[];
  onClose: () => void;
  onAuthenticated: (clusterIds: string[]) => void;
}

interface LoginGroup {
  key: string;
  username: string;
  clusters: Cluster[];
}

export function ClusterLoginModal({ clusters, onClose, onAuthenticated }: Props) {
  const mutations = useClusterMutations();
  const [passwords, setPasswords] = useState<Record<string, string>>({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [pendingKey, setPendingKey] = useState<string | null>(null);
  const groups = useMemo(() => {
    const result = new Map<string, LoginGroup>();
    for (const cluster of clusters) {
      const username = cluster.username?.trim() || 'Логин не указан';
      const key = username.toLocaleLowerCase();
      const group = result.get(key) ?? { key, username, clusters: [] };
      group.clusters.push(cluster);
      result.set(key, group);
    }
    return [...result.values()];
  }, [clusters]);

  const submit = async (event: FormEvent, group: LoginGroup) => {
    event.preventDefault();
    const password = passwords[group.key] ?? '';
    if (!password) {
      setErrors((current) => ({ ...current, [group.key]: 'Введите пароль.' }));
      return;
    }
    setPendingKey(group.key);
    setErrors((current) => ({ ...current, [group.key]: '' }));
    try {
      const ids = group.clusters.map((cluster) => cluster.id);
      await mutations.login.mutateAsync({ clusterIds: ids, password });
      setPasswords((current) => ({ ...current, [group.key]: '' }));
      onAuthenticated(ids);
    } catch (error) {
      setErrors((current) => ({ ...current, [group.key]: error instanceof Error ? error.message : 'Не удалось выполнить вход.' }));
    } finally {
      setPendingKey(null);
    }
  };

  return (
    <div className={styles.overlay} role="presentation">
      <section className={styles.modal} role="dialog" aria-modal="true" aria-labelledby="cluster-login-title">
        <header>
          <div><h2 id="cluster-login-title">Требуется вход в кластер</h2><p>Срок действия сохранённого токена завершился.</p></div>
          <button type="button" className={styles.close} onClick={onClose} aria-label="Закрыть" title="Закрыть">×</button>
        </header>
        <div className={styles.content}>
          {groups.map((group) => (
            <form key={group.key} className={styles.group} onSubmit={(event) => void submit(event, group)}>
              <div className={styles.groupInfo}>
                <span>Логин</span>
                <strong>{group.username}</strong>
                <small>Кластеры: {group.clusters.map((cluster) => cluster.name).join(', ')}</small>
              </div>
              <label>
                <span>Пароль</span>
                <input
                  type="password"
                  value={passwords[group.key] ?? ''}
                  onChange={(event) => setPasswords((current) => ({ ...current, [group.key]: event.target.value }))}
                  autoComplete="off"
                  maxLength={1024}
                  autoFocus={groups.length === 1}
                  disabled={pendingKey !== null}
                />
              </label>
              <button type="submit" disabled={pendingKey !== null}>{pendingKey === group.key ? 'Вход…' : `Войти в ${group.clusters.length > 1 ? `${group.clusters.length} кластера` : 'кластер'}`}</button>
              {errors[group.key] && <div className={styles.error}>{errors[group.key]}</div>}
            </form>
          ))}
        </div>
      </section>
    </div>
  );
}
