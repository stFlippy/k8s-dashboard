import { useEffect, useMemo, useState } from 'react';
import { useClusterMutations, useClusters } from '../../hooks/useKubernetes';
import type { Cluster, ClusterInput } from '../../types/kubernetes';
import styles from './ClusterPanel.module.css';
import { clusterAlias, readClusterAliases, writeClusterAlias } from '../../utils/clusterAliases';
import { UserManagement } from '../auth/UserManagement';
import { changePassword } from '../../api/auth';
import { ActionConfirmModal } from '../common/ActionConfirmModal';
import type { ActionConfirmRequest } from '../common/ActionConfirmModal';

interface Props {
  open: boolean;
  onClose: () => void;
  onLogout: () => void;
}

const emptyInput = (): ClusterInput => ({
  name: '',
  authType: 'kubectl-login',
  enabled: true,
  server: '',
  username: '',
  password: ''
});

function fromCluster(cluster: Cluster): ClusterInput {
  return {
    name: cluster.name,
    authType: cluster.authType,
    enabled: cluster.enabled,
    server: cluster.server ?? '',
    username: cluster.username ?? '',
    password: ''
  };
}

function HintLabel({ text, hint }: { text: string; hint: string }) {
  return <span className={styles.labelText} data-tooltip={hint} title={hint}>{text}</span>;
}

export function ClusterPanel({ open, onClose, onLogout }: Props) {
  const clustersQuery = useClusters();
  const mutations = useClusterMutations();
  const [editingId, setEditingId] = useState<string | null>(null);
  const [form, setForm] = useState<ClusterInput>(emptyInput());
  const [message, setMessage] = useState<string>('');
  const [aliases, setAliases] = useState<Record<string, string>>(() => readClusterAliases());
  const [theme, setTheme] = useState<'dark' | 'light'>(() => window.localStorage.getItem('quaber.theme') === 'light' ? 'light' : 'dark');
  const [settingsTab, setSettingsTab] = useState<'personal' | 'clusters' | 'users'>('personal');
  const [currentPassword, setCurrentPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [repeatPassword, setRepeatPassword] = useState('');
  const [confirmRequest, setConfirmRequest] = useState<ActionConfirmRequest | null>(null);

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

  useEffect(() => { document.documentElement.dataset.theme = theme; window.localStorage.setItem('quaber.theme', theme); }, [theme]);

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
  const testForm = async () => {
    setMessage('Проверка введённых параметров подключения…');
    try { const result = await mutations.testInput.mutateAsync({ input: form, existingId: editingId && editingId !== 'new' ? editingId : undefined }); setMessage(`Подключение установлено, namespaces: ${result.namespaces}. Настройки ещё не сохранены.`); }
    catch (error) { setMessage(error instanceof Error ? error.message : 'Проверка подключения завершилась ошибкой.'); }
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
    setMessage('');
    try {
      await mutations.remove.mutateAsync(cluster.id);
      if (editingId === cluster.id) setEditingId(null);
      setMessage('Конфигурация кластера удалена.');
    } catch (error) {
      setMessage(error instanceof Error ? error.message : 'Не удалось удалить конфигурацию кластера.');
    }
  };
  const requestRemove = (cluster: Cluster) => setConfirmRequest({
    title: 'Удаление кластера',
    description: `Будет безвозвратно удалена конфигурация кластера «${cluster.name}». Кластер исчезнет из матрицы и сохранённых настроек этого приложения.`,
    subject: `безвозвратно удалить кластер «${cluster.name}»`,
    confirmLabel: 'Удалить кластер',
    onConfirm: () => remove(cluster)
  });
  const updateOwnPassword = async () => { if(newPassword!==repeatPassword){setMessage('Новые пароли не совпадают.');return;}try { await changePassword(currentPassword,newPassword); setMessage('Пароль изменён. Выполняется выход из учётной записи.'); onLogout(); } catch(error){setMessage(error instanceof Error?error.message:'Не удалось изменить пароль.');} };

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
          <button className={styles.close} onClick={onClose} aria-label="Закрыть" title="Закрыть">×</button>
        </div>

        <nav className={styles.settingsTabs}><button className={settingsTab === 'personal' ? styles.active : ''} onClick={() => setSettingsTab('personal')}>Личное</button><button className={settingsTab === 'clusters' ? styles.active : ''} onClick={() => setSettingsTab('clusters')}>Кластеры</button><button className={settingsTab === 'users' ? styles.active : ''} onClick={() => setSettingsTab('users')}>Пользователи</button></nav>

        <div className={styles.body}>
          {settingsTab === 'personal' && <><label className={styles.themeSwitch}><span><b>Светлая тема</b><small>Настройка сохраняется за учётной записью</small></span><input type="checkbox" checked={theme === 'light'} onChange={(event) => setTheme(event.target.checked ? 'light' : 'dark')} /></label><div className={styles.passwordCard}><h3>Смена пароля</h3><label>Текущий пароль<input type="password" maxLength={256} autoComplete="off" value={currentPassword} onChange={(event) => setCurrentPassword(event.target.value)} /></label><label>Новый пароль<input type="password" minLength={12} maxLength={256} autoComplete="off" value={newPassword} onChange={(event) => setNewPassword(event.target.value)} /></label><label>Повторите новый пароль<input type="password" minLength={12} maxLength={256} autoComplete="off" value={repeatPassword} onChange={(event) => setRepeatPassword(event.target.value)} /></label><button className={styles.primary} disabled={!currentPassword || newPassword.length < 12 || newPassword !== repeatPassword} onClick={() => { void updateOwnPassword(); }}>Изменить пароль</button></div><div className={styles.actions}><button onClick={onLogout}>Выйти из учётной записи</button></div>{message && <div className={styles.message}><span>{message}</span><button className={styles.messageClose} onClick={() => setMessage('')} aria-label="Закрыть сообщение" title="Закрыть">×</button></div>}</>}
          {settingsTab === 'clusters' && <>
          {secretsLocked && (
            <div className={styles.warning}>
              Хранилище kubeconfig заблокировано. Задайте <code>DASHBOARD_MASTER_KEY</code> в корневом <code>.env</code>, чтобы сохранять чувствительные данные.
            </div>
          )}

          <div className={styles.securityNote}>Единственный способ подключения — персональный <b>kubectl login</b>. Пароль передаётся локальному клиенту параметром <b>--password</b> и не сохраняется; созданный kubeconfig хранится зашифрованным, а запросы Quaber выполняются через отдельный локальный kubectl proxy пользователя.</div>

          <div className={styles.sectionHeader}>
            <h3>Кластеры</h3>
            <button className={styles.primary} onClick={startCreate}>Добавить кластер</button>
          </div>

          {clustersQuery.isLoading && <div className={styles.state}>Загрузка кластеров…</div>}
          {clustersQuery.isError && <div className={styles.error}>{clustersQuery.error?.message ?? 'Не удалось загрузить список кластеров.'}</div>}

          <div className={styles.clusterList}>
            {clusters.map((cluster, index) => editingId === cluster.id ? null : (
              <div className={styles.clusterCard} key={cluster.id} style={{ order: index * 2 }}>
                <div className={styles.clusterTop}>
                  <div>
                    <div className={styles.clusterName}>{cluster.name}</div>
                    <div className={styles.clusterMeta}>
                      kubectl proxy · {cluster.enabled ? 'включён' : 'выключен'} · персональный
                    </div>
                  </div>
                  <span className={`${styles.statusDot} ${cluster.enabled ? styles.enabled : ''}`} />
                </div>
                <dl className={styles.details}>
                  <dt>Алиас</dt><dd className={styles.aliasEditor}><input maxLength={5} value={aliases[cluster.id] ?? ''} placeholder={clusterAlias(cluster.id, cluster.name, {})} onChange={(event) => setAliases((current) => ({ ...current, [cluster.id]: event.target.value.slice(0, 5) }))} /><button type="button" onClick={() => { writeClusterAlias(cluster.id, aliases[cluster.id] ?? ''); setAliases(readClusterAliases()); setMessage(`Алиас кластера ${cluster.name} сохранён.`); }}>OK</button></dd>
                  {cluster.context && <><dt>Context</dt><dd>{cluster.context}</dd></>}
                  {cluster.server && <><dt>API</dt><dd>{cluster.server}</dd></>}
                  {cluster.username && <><dt>Пользователь</dt><dd>{cluster.username}</dd></>}
                  <dt>Вход</dt><dd className={cluster.loginRequired ? styles.insecure : ''}>{cluster.loginRequired ? 'требуется пароль' : 'kubeconfig активен'}</dd>
                </dl>
                <div className={styles.actions}>
                  <button onClick={() => { void runTest(cluster); }}>Проверить</button>
                  {cluster.managedBy === 'user' && <button className={cluster.loginRequired ? styles.primary : ''} onClick={() => startEdit(cluster)}>{cluster.loginRequired ? 'Войти' : 'Обновить вход'}</button>}
                  {cluster.managedBy === 'user' && <button onClick={() => startEdit(cluster)}>Изменить</button>}
                  {cluster.managedBy === 'user' && <button className={styles.danger} onClick={() => requestRemove(cluster)}>Удалить</button>}
                </div>
              </div>
            ))}

          {editingId && (
            <div className={styles.formCard} style={{ order: editingId === 'new' ? clusters.length * 2 + 1 : Math.max(0, clusters.findIndex((cluster) => cluster.id === editingId)) * 2 }}>
              <div className={styles.formTitle}>{editingId === 'new' ? 'Добавление кластера' : `Редактирование ${editing?.name ?? 'кластера'}`}</div>

              <label><HintLabel text="Имя" hint="Отображаемое название кластера в интерфейсе." /><input required maxLength={80} value={form.name} onChange={(event) => set('name', event.target.value.replace(/[\u0000-\u001f\u007f]/g, ''))} /></label>
              <label><HintLabel text="Тип подключения" hint="Quaber получает персональный kubeconfig через addons/kubectl login и запускает локальный proxy." /><input value="Kubectl login → kubectl proxy" readOnly /></label>
              <label className={styles.check}><input type="checkbox" checked={form.enabled} onChange={(event) => set('enabled', event.target.checked)} /> <HintLabel text="Кластер активен" hint="Неактивный кластер сохраняется в настройках, но не участвует в запросах и матрице." /></label>

              <label><HintLabel text="API server" hint="URL Kubernetes API сервера, например https://cluster.example:6443." /><input type="url" required maxLength={2048} value={form.server ?? ''} onChange={(event) => set('server', event.target.value)} placeholder="https://kubernetes.example:6443" /></label>
              <label><HintLabel text="Пользователь" hint={editing ? 'Логин сохранён. Для повторной авторизации введите только пароль.' : 'Логин пользователя Kubernetes. Он будет сохранён в настройках кластера.'} /><input required maxLength={256} value={form.username ?? ''} onChange={(event) => set('username', event.target.value)} readOnly={Boolean(editing)} autoComplete="username" /></label>
              <label><HintLabel text="Пароль" hint="Передаётся в addons/kubectl параметром --password, используется для создания kubeconfig и не сохраняется." /><input type="password" required={editingId === 'new'} maxLength={1024} value={form.password ?? ''} onChange={(event) => set('password', event.target.value)} autoComplete="off" /></label>

              <div className={styles.formActions}>
                <button onClick={() => { setEditingId(null); setForm(emptyInput()); }}>Отмена</button>
                <button onClick={() => { void testForm(); }} disabled={secretsLocked || mutations.testInput.isPending}>Тест</button>
                <button className={styles.primary} onClick={() => { void save(); }} disabled={secretsLocked || mutations.create.isPending || mutations.update.isPending}>Сохранить</button>
              </div>
            </div>
          )}
          </div>

          {message && <div className={styles.message}><span>{message}</span><button className={styles.messageClose} onClick={() => setMessage('')} aria-label="Закрыть сообщение" title="Закрыть">×</button></div>}
          </>}
          {settingsTab === 'users' && <UserManagement />}
        </div>
      </aside>
      <ActionConfirmModal request={confirmRequest} onClose={() => setConfirmRequest(null)} />
    </>
  );
}
