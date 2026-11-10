import { useState } from 'react';
import { login } from '../../api/auth';
import type { UserAccount } from '../../types/kubernetes';
import styles from './AuthScreen.module.css';

export function AuthScreen({ onAuthenticated }: { onAuthenticated: (user: UserAccount) => void }) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [pending, setPending] = useState(false);

  const submit = async () => {
    setError('');
    setPending(true);
    try {
      onAuthenticated(await login(username, password));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Ошибка авторизации.');
    } finally {
      setPending(false);
      setPassword('');
    }
  };

  return <div className={styles.root}><form onSubmit={(event) => { event.preventDefault(); void submit(); }}>
    <img src="/logo.png" alt="Quaber" />
    <h1>Вход в Quaber</h1>
    <label>Логин<input autoFocus autoComplete="username" maxLength={128} required value={username} onChange={(event) => setUsername(event.target.value)} /></label>
    <label>Пароль<input type="password" maxLength={1024} required autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} /></label>
    {error && <div className={styles.error}>{error}</div>}
    <button disabled={pending}>{pending ? 'Проверка…' : 'Войти'}</button>
  </form></div>;
}
