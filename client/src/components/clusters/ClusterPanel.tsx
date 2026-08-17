import { useEffect, useMemo, useRef, useState } from 'react';
import { useClusterMutations, useClusters } from '../../hooks/useKubernetes';
import type { Cluster, ClusterAuthType, ClusterInput } from '../../types/kubernetes';
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
  authType: 'kubeconfig',
  enabled: true,
  context: '',
  kubeconfigPath: '',
  kubeconfigData: '',
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
    kubeconfigData: '',
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
  const [kubeconfigMode, setKubeconfigMode] = useState<'file' | 'paste'>('file');
  const [kubeconfigFileName, setKubeconfigFileName] = useState('');
  const kubeconfigFileRef = useRef<HTMLInputElement>(null);

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
    setKubeconfigMode('file');
    setKubeconfigFileName('');
  };

  const startEdit = (cluster: Cluster) => {
    if (cluster.managedBy === 'environment') return;
    setEditingId(cluster.id);
    setForm(fromCluster(cluster));
    setMessage('');
    setKubeconfigMode('file');
    setKubeconfigFileName('');
  };

  const readKubeconfigFile = async (file: File | undefined) => {
    if (!file) return;
    if (file.size > 1_000_000) { setMessage('Файл kubeconfig превышает допустимый размер 1 МБ.'); return; }
    try {
      const content = await file.text();
      if (!content.trim()) throw new Error('Выбранный kubeconfig пуст.');
      set('kubeconfigData', content);
      setKubeconfigFileName(file.name);
      setMessage(`Файл «${file.name}» загружен. Выполните тест подключения перед сохранением.`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : 'Не удалось прочитать kubeconfig.');
    }
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
          {settingsTab === 'personal' && <><label className={styles.themeSwitch}><span><b>Светлая тема</b><small>Настройка сохраняется за учётной записью</small></span><input type="checkbox" checked={theme === 'light'} onChange={(event) => setTheme(event.target.checked ? 'light' : 'dark')} /></label><div className={styles.passwordCard}><h3>Смена пароля</h3><label>Текущий пароль<input type="password" maxLength={256} autoComplete="current-password" value={currentPassword} onChange={(event) => setCurrentPassword(event.target.value)} /></label><label>Новый пароль<input type="password" minLength={12} maxLength={256} autoComplete="new-password" value={newPassword} onChange={(event) => setNewPassword(event.target.value)} placeholder="Не менее 12 символов" /></label><label>Повторите новый пароль<input type="password" minLength={12} maxLength={256} autoComplete="new-password" value={repeatPassword} onChange={(event) => setRepeatPassword(event.target.value)} /></label><button className={styles.primary} disabled={!currentPassword || newPassword.length < 12 || newPassword !== repeatPassword} onClick={() => { void updateOwnPassword(); }}>Изменить пароль</button></div><div className={styles.actions}><button onClick={onLogout}>Выйти из учётной записи</button></div>{message && <div className={styles.message}><span>{message}</span><button className={styles.messageClose} onClick={() => setMessage('')} aria-label="Закрыть сообщение" title="Закрыть">×</button></div>}</>}
          {settingsTab === 'clusters' && <>
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
                  <dt>Алиас</dt><dd className={styles.aliasEditor}><input maxLength={5} value={aliases[cluster.id] ?? ''} placeholder={clusterAlias(cluster.id, cluster.name, {})} onChange={(event) => setAliases((current) => ({ ...current, [cluster.id]: event.target.value.slice(0, 5) }))} /><button type="button" onClick={() => { writeClusterAlias(cluster.id, aliases[cluster.id] ?? ''); setAliases(readClusterAliases()); setMessage(`Алиас кластера ${cluster.name} сохранён.`); }}>OK</button></dd>
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
                  {cluster.managedBy === 'user' && <button className={styles.danger} onClick={() => requestRemove(cluster)}>Удалить</button>}
                </div>
              </div>
            ))}
          </div>

          {editingId && (
            <div className={styles.formCard}>
              <div className={styles.formTitle}>{editingId === 'new' ? 'Добавление кластера' : `Редактирование ${editing?.name ?? 'кластера'}`}</div>

              <label><HintLabel text="Имя" hint="Отображаемое название кластера в интерфейсе." /><input required maxLength={80} value={form.name} onChange={(event) => set('name', event.target.value.replace(/[\u0000-\u001f\u007f]/g, ''))} /></label>
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
                  <div className={styles.kubeconfigSource}>
                    <span>Источник kubeconfig</span>
                    <div><button type="button" className={kubeconfigMode === 'file' ? styles.sourceActive : ''} onClick={() => setKubeconfigMode('file')}>Выбрать файл</button><button type="button" className={kubeconfigMode === 'paste' ? styles.sourceActive : ''} onClick={() => setKubeconfigMode('paste')}>Вставить текст</button></div>
                  </div>
                  {kubeconfigMode === 'file' ? <div className={styles.filePicker}><input ref={kubeconfigFileRef} type="file" hidden accept=".yaml,.yml,.conf,.config,text/yaml,application/yaml,text/plain" onChange={(event) => { void readKubeconfigFile(event.target.files?.[0]); event.currentTarget.value=''; }} /><button type="button" onClick={() => kubeconfigFileRef.current?.click()}>Открыть файл…</button><span>{kubeconfigFileName || (editing?.kubeconfigConfigured ? 'Сохранённый kubeconfig — выберите файл только для замены' : 'Файл не выбран')}</span></div> : <label className={styles.kubeconfigEditor}><HintLabel text="Содержимое kubeconfig" hint="Вставьте полный YAML kubeconfig. Он будет храниться на сервере в зашифрованном виде." /><textarea maxLength={1000000} value={form.kubeconfigData ?? ''} onChange={(event) => { set('kubeconfigData', event.target.value); setKubeconfigFileName(''); }} placeholder="apiVersion: v1&#10;kind: Config&#10;clusters: …" /></label>}
                  <label><HintLabel text="Context" hint="Имя context внутри kubeconfig. Пусто — использовать текущий context." /><input maxLength={253} value={form.context ?? ''} onChange={(event) => set('context', event.target.value)} placeholder="пусто = текущий context" /></label>
                </>
              )}

              {form.authType !== 'kubeconfig' && (
                <>
                  <label><HintLabel text="API server" hint="URL Kubernetes API сервера, например https://cluster.example:6443." /><input type="url" maxLength={2048} value={form.server ?? ''} onChange={(event) => set('server', event.target.value)} placeholder="https://kubernetes.example:6443" /></label>
                  {form.authType === 'basic' && <label><HintLabel text="Пользователь" hint="Имя пользователя для basic-auth подключения." /><input maxLength={256} value={form.username ?? ''} onChange={(event) => set('username', event.target.value)} autoComplete="off" /></label>}
                  {form.authType === 'token' && <label><HintLabel text="Bearer token" hint="Токен сервисного аккаунта или пользователя для авторизации в API." /><input type="password" maxLength={65536} value={form.token ?? ''} onChange={(event) => set('token', event.target.value)} autoComplete="new-password" placeholder={editing?.tokenMasked ? 'сохранён •••••••• — оставьте пустым, чтобы не менять' : ''} /></label>}
                  {form.authType === 'basic' && <label><HintLabel text="Пароль" hint="Пароль для выбранного пользователя basic-auth." /><input type="password" value={form.password ?? ''} onChange={(event) => set('password', event.target.value)} autoComplete="new-password" placeholder={editing?.passwordMasked ? 'сохранён •••••••• — оставьте пустым, чтобы не менять' : ''} /></label>}
                  <label><HintLabel text="CA certificate / certificate-authority-data" hint="PEM-сертификат или base64 содержимое certificate-authority-data для проверки TLS." /><textarea maxLength={1000000} value={form.caData ?? ''} onChange={(event) => set('caData', event.target.value)} placeholder="Необязательный PEM или base64 certificate-authority-data" /></label>
                  <label className={styles.check}><input type="checkbox" checked={Boolean(form.skipTlsVerify)} onChange={(event) => set('skipTlsVerify', event.target.checked)} /> <HintLabel text="Пропустить TLS verification" hint="Использовать только в тестовых стендах. Проверка сертификата API сервера будет отключена." /></label>
                </>
              )}

              <div className={styles.formActions}>
                <button onClick={() => { setEditingId(null); setForm(emptyInput()); }}>Отмена</button>
                <button onClick={() => { void testForm(); }} disabled={(form.authType !== 'kubeconfig' && secretsLocked) || mutations.testInput.isPending}>Тест</button>
                <button className={styles.primary} onClick={() => { void save(); }} disabled={(form.authType !== 'kubeconfig' && secretsLocked) || mutations.create.isPending || mutations.update.isPending}>Сохранить</button>
              </div>
            </div>
          )}

          {message && <div className={styles.message}><span>{message}</span><button className={styles.messageClose} onClick={() => setMessage('')} aria-label="Закрыть сообщение" title="Закрыть">×</button></div>}
          </>}
          {settingsTab === 'users' && <UserManagement />}
        </div>
      </aside>
      <ActionConfirmModal request={confirmRequest} onClose={() => setConfirmRequest(null)} />
    </>
  );
}
