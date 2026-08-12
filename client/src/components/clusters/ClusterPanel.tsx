import { useEffect, useMemo, useState } from 'react';
import { useClusterMutations, useClusters } from '../../hooks/useKubernetes';
import type { Cluster, ClusterAuthType, ClusterInput } from '../../types/kubernetes';
import styles from './ClusterPanel.module.css';

interface Props {
  open: boolean;
  onClose: () => void;
}

const emptyInput = (): ClusterInput => ({
  name: '',
  authType: 'kubeconfig',
  enabled: true,
  context: '',
  kubeconfigPath: '',
  server: '',
  username: '',
  token: '',
  password: '',
  caData: '',
  skipTlsVerify: false
});

function fromCluster(cluster: Cluster): ClusterInput {
  return {
    name: cluster.name,
    authType: cluster.authType,
    enabled: cluster.enabled,
    context: cluster.context ?? '',
    kubeconfigPath: cluster.kubeconfigPath ?? '',
    server: cluster.server ?? '',
    username: cluster.username ?? '',
    token: '',
    password: '',
    caData: '',
    skipTlsVerify: cluster.skipTlsVerify
  };
}

function HintLabel({ text, hint }: { text: string; hint: string }) {
  return <span className={styles.labelText} data-tooltip={hint} title={hint}>{text}</span>;
}

export function ClusterPanel({ open, onClose }: Props) {
  const clustersQuery = useClusters();
  const mutations = useClusterMutations();
  const [editingId, setEditingId] = useState<string | null>(null);
  const [form, setForm] = useState<ClusterInput>(emptyInput());
  const [message, setMessage] = useState<string>('');

  const clusters = clustersQuery.data ?? [];
  const secretsLocked = useMemo(() => clusters.some((cluster) => cluster.secretsLocked), [clusters]);
  const editing = clusters.find((cluster) => cluster.id === editingId) ?? null;

  useEffect(() => {
    if (!open) {
      setEditingId(null);
      setForm(emptyInput());
      setMessage('');
    }
  }, [open]);

  const set = <K extends keyof ClusterInput>(key: K, value: ClusterInput[K]) => {
    setForm((current) => ({ ...current, [key]: value }));
  };

  const startCreate = () => {
    setEditingId('new');
    setForm(emptyInput());
    setMessage('');
  };

  const startEdit = (cluster: Cluster) => {
    if (cluster.managedBy === 'environment') return;
    setEditingId(cluster.id);
    setForm(fromCluster(cluster));
    setMessage('');
  };

  const save = async () => {
    setMessage('');
    try {
      if (editingId === 'new') {
        await mutations.create.mutateAsync(form);
        setMessage('Кластер сохранён.');
      } else if (editingId) {
        await mutations.update.mutateAsync({ id: editingId, input: form });
        setMessage('Кластер обновлён.');
      }
      setEditingId(null);
      setForm(emptyInput());
    } catch (error) {
      setMessage(error instanceof Error ? error.message : 'Не удалось сохранить кластер.');
    }
  };

  const runTest = async (cluster: Cluster) => {
    setMessage(`Проверка подключения к ${cluster.name}…`);
    try {
      const result = await mutations.test.mutateAsync(cluster.id);
      setMessage(`${cluster.name}: подключение установлено, namespaces: ${result.namespaces}.`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : 'Проверка подключения завершилась ошибкой.');
    }
  };

  const remove = async (cluster: Cluster) => {
    if (!window.confirm(`Удалить конфигурацию кластера «${cluster.name}»?`)) return;
    setMessage('');
    try {
      await mutations.remove.mutateAsync(cluster.id);
      if (editingId === cluster.id) setEditingId(null);
      setMessage('Конфигурация кластера удалена.');
    } catch (error) {
      setMessage(error instanceof Error ? error.message : 'Не удалось удалить конфигурацию кластера.');
    }
  };

  if (!open) return null;

  return (
    <>
      <div className={styles.overlay} onClick={onClose} />
      <aside className={styles.panel} aria-label="Панель пользователя и кластеров">
        <div className={styles.header}>
          <div>
            <h2>Панель пользователя</h2>
            <p>Кластеры и параметры подключения</p>
          </div>
          <button className={styles.close} onClick={onClose}>Закрыть</button>
        </div>

        <div className={styles.body}>
          {secretsLocked && (
            <div className={styles.warning}>
              Хранилище токенов/паролей заблокировано. Задайте <code>DASHBOARD_MASTER_KEY</code> в корневом <code>.env</code>, чтобы сохранять чувствительные данные.
            </div>
          )}

          <div className={styles.securityNote}>Сохранённые значения токенов и паролей не возвращаются в браузер. Уже существующие секреты отображаются только маской, а пустое поле при редактировании сохраняет текущее значение.</div>

          <div className={styles.sectionHeader}>
            <h3>Кластеры</h3>
            <button className={styles.primary} onClick={startCreate}>Добавить кластер</button>
          </div>

          {clustersQuery.isLoading && <div className={styles.state}>Загрузка кластеров…</div>}
          {clustersQuery.isError && <div className={styles.error}>{clustersQuery.error?.message ?? 'Не удалось загрузить список кластеров.'}</div>}

          <div className={styles.clusterList}>
            {clusters.map((cluster) => (
              <div className={styles.clusterCard} key={cluster.id}>
                <div className={styles.clusterTop}>
                  <div>
                    <div className={styles.clusterName}>{cluster.name}</div>
                    <div className={styles.clusterMeta}>
                      {cluster.authType} · {cluster.enabled ? 'включён' : 'выключен'} · {cluster.managedBy === 'environment' ? 'из окружения' : 'пользовательский'}
                    </div>
                  </div>
                  <span className={`${styles.statusDot} ${cluster.enabled ? styles.enabled : ''}`} />
                </div>
                <dl className={styles.details}>
                  {cluster.context && <><dt>Context</dt><dd>{cluster.context}</dd></>}
                  {cluster.server && <><dt>API</dt><dd>{cluster.server}</dd></>}
                  {cluster.username && <><dt>Пользователь</dt><dd>{cluster.username}</dd></>}
                  {cluster.tokenMasked && <><dt>Токен</dt><dd className={styles.secret}>{cluster.tokenMasked}</dd></>}
                  {cluster.passwordMasked && <><dt>Пароль</dt><dd className={styles.secret}>{cluster.passwordMasked}</dd></>}
                  {cluster.caConfigured && <><dt>CA</dt><dd>настроен</dd></>}
                  {cluster.skipTlsVerify && <><dt>TLS</dt><dd className={styles.insecure}>проверка отключена</dd></>}
                </dl>
                <div className={styles.actions}>
                  <button onClick={() => { void runTest(cluster); }}>Проверить</button>
                  {cluster.managedBy === 'user' && <button onClick={() => startEdit(cluster)}>Изменить</button>}
                  {cluster.managedBy === 'user' && <button className={styles.danger} onClick={() => { void remove(cluster); }}>Удалить</button>}
                </div>
              </div>
            ))}
          </div>

          {editingId && (
            <div className={styles.formCard}>
              <div className={styles.formTitle}>{editingId === 'new' ? 'Добавление кластера' : `Редактирование ${editing?.name ?? 'кластера'}`}</div>

              <label><HintLabel text="Имя" hint="Отображаемое название кластера в интерфейсе." /><input value={form.name} onChange={(event) => set('name', event.target.value)} /></label>
              <label><HintLabel text="Тип подключения" hint="Выберите способ авторизации: kubeconfig, bearer token или логин/пароль." />
                <select value={form.authType} onChange={(event) => set('authType', event.target.value as ClusterAuthType)}>
                  <option value="kubeconfig">Kubeconfig / context</option>
                  <option value="token">Bearer token</option>
                  <option value="basic">Логин / пароль</option>
                </select>
              </label>
              <label className={styles.check}><input type="checkbox" checked={form.enabled} onChange={(event) => set('enabled', event.target.checked)} /> <HintLabel text="Кластер активен" hint="Неактивный кластер сохраняется в настройках, но не участвует в запросах и матрице." /></label>

              {form.authType === 'kubeconfig' && (
                <>
                  <label><HintLabel text="Путь к kubeconfig" hint="Полный путь к файлу kubeconfig на сервере. Пусто — использовать ~/.kube/config." /><input value={form.kubeconfigPath ?? ''} onChange={(event) => set('kubeconfigPath', event.target.value)} placeholder="пусто = ~/.kube/config" /></label>
                  <label><HintLabel text="Context" hint="Имя context внутри kubeconfig. Пусто — использовать текущий context." /><input value={form.context ?? ''} onChange={(event) => set('context', event.target.value)} placeholder="пусто = текущий context" /></label>
                </>
              )}

              {form.authType !== 'kubeconfig' && (
                <>
                  <label><HintLabel text="API server" hint="URL Kubernetes API сервера, например https://cluster.example:6443." /><input value={form.server ?? ''} onChange={(event) => set('server', event.target.value)} placeholder="https://kubernetes.example:6443" /></label>
                  {form.authType === 'basic' && <label><HintLabel text="Пользователь" hint="Имя пользователя для basic-auth подключения." /><input value={form.username ?? ''} onChange={(event) => set('username', event.target.value)} autoComplete="off" /></label>}
                  {form.authType === 'token' && <label><HintLabel text="Bearer token" hint="Токен сервисного аккаунта или пользователя для авторизации в API." /><input type="password" value={form.token ?? ''} onChange={(event) => set('token', event.target.value)} autoComplete="new-password" placeholder={editing?.tokenMasked ? 'сохранён •••••••• — оставьте пустым, чтобы не менять' : ''} /></label>}
                  {form.authType === 'basic' && <label><HintLabel text="Пароль" hint="Пароль для выбранного пользователя basic-auth." /><input type="password" value={form.password ?? ''} onChange={(event) => set('password', event.target.value)} autoComplete="new-password" placeholder={editing?.passwordMasked ? 'сохранён •••••••• — оставьте пустым, чтобы не менять' : ''} /></label>}
                  <label><HintLabel text="CA certificate / certificate-authority-data" hint="PEM-сертификат или base64 содержимое certificate-authority-data для проверки TLS." /><textarea value={form.caData ?? ''} onChange={(event) => set('caData', event.target.value)} placeholder="Необязательный PEM или base64 certificate-authority-data" /></label>
                  <label className={styles.check}><input type="checkbox" checked={Boolean(form.skipTlsVerify)} onChange={(event) => set('skipTlsVerify', event.target.checked)} /> <HintLabel text="Пропустить TLS verification" hint="Использовать только в тестовых стендах. Проверка сертификата API сервера будет отключена." /></label>
                </>
              )}

              <div className={styles.formActions}>
                <button onClick={() => { setEditingId(null); setForm(emptyInput()); }}>Отмена</button>
                <button className={styles.primary} onClick={() => { void save(); }} disabled={(form.authType !== 'kubeconfig' && secretsLocked) || mutations.create.isPending || mutations.update.isPending}>Сохранить</button>
              </div>
            </div>
          )}

          {message && <div className={styles.message}>{message}</div>}
        </div>
      </aside>
    </>
  );
}
