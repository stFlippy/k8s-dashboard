import { useEffect, useState } from 'react';
import { createUser, getUsers, resetUserPassword, setUserAdministrator } from '../../api/auth';
import type { UserAccount } from '../../types/kubernetes';
import { ActionConfirmModal } from '../common/ActionConfirmModal';
import type { ActionConfirmRequest } from '../common/ActionConfirmModal';
import styles from './UserManagement.module.css';

export function UserManagement({ currentUserId, authMode }: { currentUserId: string; authMode: 'external' | 'local' }) {
  const [users, setUsers] = useState<UserAccount[]>([]);
  const [username, setUsername] = useState('');
  const [message, setMessage] = useState('');
  const [credential, setCredential] = useState<{ username: string; password: string } | null>(null);
  const [confirm, setConfirm] = useState<ActionConfirmRequest | null>(null);
  const load = async () => {
    try { setUsers(await getUsers()); }
    catch (error) { setMessage(error instanceof Error ? error.message : 'Не удалось загрузить пользователей.'); }
  };
  useEffect(() => { void load(); }, []);
  const requestRole = (user: UserAccount) => setConfirm({
    title: 'Права администратора',
    description: `${user.isAdmin ? 'Отозвать права администратора' : 'Назначить администратором'} пользователя «${user.username}»? Его текущие сессии будут закрыты.`,
    confirmLabel: user.isAdmin ? 'Отозвать права' : 'Назначить',
    onConfirm: async () => { await setUserAdministrator(user.id, !user.isAdmin); await load(); }
  });
  const requestAdd = () => {
    const name = username.trim();
    if (!name || name !== username) { setMessage('Введите имя без пробелов по краям.'); return; }
    if (users.some((user) => user.username.toLocaleLowerCase() === name.toLocaleLowerCase())) { setMessage(`Пользователь «${name}» уже существует.`); return; }
    setConfirm({
      title: 'Добавление пользователя',
      description: `Будет создан пользователь «${name}» с временным паролем. Пароль появится на экране один раз.`,
      subject: `создать пользователя «${name}»`,
      confirmLabel: 'Добавить пользователя',
      onConfirm: async () => { const result = await createUser(name); setCredential({ username: result.user.username, password: result.temporaryPassword }); setUsername(''); setMessage(''); await load(); }
    });
  };
  const requestReset = (user: UserAccount) => setConfirm({
    title: 'Сброс пароля',
    description: `Пароль пользователя «${user.username}» будет заменён временным, все его сессии завершатся.`,
    subject: `сбросить пароль пользователя «${user.username}»`,
    confirmLabel: 'Сбросить пароль',
    onConfirm: async () => { const result = await resetUserPassword(user.id); setCredential({ username: user.username, password: result.temporaryPassword }); setMessage(''); await load(); }
  });
  return <section className={styles.root}>
    {authMode === 'external' ? <p>Пользователи появляются здесь после первого успешного входа через внешний сервер авторизации. Пароли меняются на том сервере.</p> : <>
      <p>Локальные пароли хранятся в SQLite только в виде salted scrypt-хэшей. Новые и сброшенные пароли нужно сменить при первом входе.</p>
      <div className={styles.create}><input value={username} maxLength={128} onChange={(event) => setUsername(event.target.value)} aria-label="Имя нового пользователя" placeholder="Имя пользователя" /><button type="button" onClick={requestAdd} disabled={!username.trim()}>Добавить</button></div>
      {credential && <div className={styles.credential}><b>Временный пароль для {credential.username}</b><code>{credential.password}</code><button type="button" onClick={() => { if (!navigator.clipboard?.writeText) { setMessage('Буфер обмена недоступен. Скопируйте пароль вручную.'); return; } void navigator.clipboard.writeText(credential.password).catch(() => setMessage('Не удалось скопировать пароль. Скопируйте его вручную.')); }}>Копировать</button><small>Пароль показывается только сейчас. Передайте его безопасным способом.</small></div>}
    </>}
    {users.map((user) => <div className={styles.user} key={user.id}>
      <span><b>{user.username}</b><small>{user.isAdmin ? 'Администратор' : 'Пользователь'}{authMode === 'local' && user.mustChangePassword ? ' · ожидается смена пароля' : ''}</small></span>
      {authMode === 'local' && <button type="button" onClick={() => requestReset(user)}>Сбросить пароль</button>}
      <button onClick={() => requestRole(user)} disabled={user.id === currentUserId && user.isAdmin}>{user.isAdmin ? 'Снять права' : 'Сделать администратором'}</button>
    </div>)}
    {message && <div className={styles.message}>{message}</div>}
    <ActionConfirmModal request={confirm} onClose={() => setConfirm(null)} />
  </section>;
}
