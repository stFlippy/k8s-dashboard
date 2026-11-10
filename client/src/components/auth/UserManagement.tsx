import { useEffect, useState } from 'react';
import { getUsers, setUserAdministrator } from '../../api/auth';
import type { UserAccount } from '../../types/kubernetes';
import { ActionConfirmModal } from '../common/ActionConfirmModal';
import type { ActionConfirmRequest } from '../common/ActionConfirmModal';
import styles from './UserManagement.module.css';

export function UserManagement({ currentUserId }: { currentUserId: string }) {
  const [users, setUsers] = useState<UserAccount[]>([]);
  const [message, setMessage] = useState('');
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
  return <section className={styles.root}>
    <p>Пользователи появляются здесь после первого успешного входа через внешний сервер авторизации. Пароли меняются на том сервере.</p>
    {users.map((user) => <div className={styles.user} key={user.id}>
      <span><b>{user.username}</b><small>{user.isAdmin ? 'Администратор' : 'Пользователь'}</small></span>
      <button onClick={() => requestRole(user)} disabled={user.id === currentUserId && user.isAdmin}>{user.isAdmin ? 'Снять права' : 'Сделать администратором'}</button>
    </div>)}
    {message && <div className={styles.message}>{message}</div>}
    <ActionConfirmModal request={confirm} onClose={() => setConfirm(null)} />
  </section>;
}
