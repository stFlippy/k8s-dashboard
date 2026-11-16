import { useState } from 'react';
import { changePassword, login } from '../../api/auth';
import type { UserAccount } from '../../types/kubernetes';
import styles from './AuthScreen.module.css';

interface Props {
  user?: UserAccount | null;
  onAuthenticated: (user: UserAccount | null) => void;
}

export function AuthScreen({ user, onAuthenticated }: Props) {
  const [username, setUsername] = useState('');
  const [current, setCurrent] = useState('');
  const [next, setNext] = useState('');
  const [repeat, setRepeat] = useState('');
  const [error, setError] = useState('');
  const [pending, setPending] = useState(false);
  const changing = Boolean(user?.mustChangePassword);

  const submit = async () => {
    setError('');
    if (changing && next !== repeat) { setError('Новые пароли не совпадают.'); return; }
    setPending(true);
    try {
      if (changing) {
        await changePassword(current, next, repeat);
        setCurrent(''); setNext(''); setRepeat('');
        onAuthenticated(null);
      } else {
        const result = await login(username, current);
        setCurrent('');
        onAuthenticated(result);
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Ошибка авторизации.');
    } finally {
      setPending(false);
    }
  };

  return <div className={styles.root}><form onSubmit={(event) => { event.preventDefault(); void submit(); }}>
    <img src="/logo.png" alt="Quaber" />
    <h1>{changing ? 'Смена временного пароля' : 'Вход в Quaber'}</h1>
    {changing && <p>Пользователь: <b>{user?.username}</b></p>}
    {!changing && <label>Логин<input autoFocus autoComplete="username" maxLength={128} required value={username} onChange={(event) => setUsername(event.target.value)} /></label>}
    <label>{changing ? 'Текущий пароль' : 'Пароль'}<input type="password" autoFocus={changing} autoComplete="current-password" maxLength={1024} required value={current} onChange={(event) => setCurrent(event.target.value)} /></label>
    {changing && <>
      <label>Новый пароль<input type="password" autoComplete="new-password" minLength={12} maxLength={256} required value={next} onChange={(event) => setNext(event.target.value)} /></label>
      <label>Повторите новый пароль<input type="password" autoComplete="new-password" minLength={12} maxLength={256} required value={repeat} onChange={(event) => setRepeat(event.target.value)} /></label>
    </>}
    {error && <div className={styles.error}>{error}</div>}
    <button disabled={pending || (changing && next !== repeat)}>{pending ? 'Проверка…' : changing ? 'Сменить пароль' : 'Войти'}</button>
    {changing && <small>После смены пароля войдите снова.</small>}
  </form></div>;
}
