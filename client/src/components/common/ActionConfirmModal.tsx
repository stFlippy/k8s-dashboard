import { useEffect, useMemo, useState } from 'react';
import styles from './ActionConfirmModal.module.css';

export interface ActionConfirmRequest {
  title: string;
  description: string;
  confirmLabel: string;
  onConfirm: () => Promise<void> | void;
}

interface Props {
  request: ActionConfirmRequest | null;
  onClose: () => void;
}

function randomTwoDigit(): number {
  return Math.floor(Math.random() * 90) + 10;
}

export function ActionConfirmModal({ request, onClose }: Props) {
  const [first, setFirst] = useState<number>(randomTwoDigit());
  const [second, setSecond] = useState<number>(randomTwoDigit());
  const [answer, setAnswer] = useState('');
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (request) {
      setFirst(randomTwoDigit());
      setSecond(randomTwoDigit());
      setAnswer('');
      setError('');
      setSubmitting(false);
    }
  }, [request]);

  const expected = useMemo(() => first + second, [first, second]);

  if (!request) return null;

  const submit = async () => {
    if (Number(answer) !== expected) {
      setError('Неверный ответ. Подтверждение не выполнено.');
      return;
    }
    try {
      setSubmitting(true);
      await request.onConfirm();
      onClose();
    } catch (modalError) {
      setError(modalError instanceof Error ? modalError.message : 'Не удалось выполнить действие.');
      setSubmitting(false);
    }
  };

  return (
    <div className={styles.overlay} onMouseDown={(event) => event.target === event.currentTarget && !submitting && onClose()}>
      <div className={styles.modal} role="dialog" aria-modal="true" aria-labelledby="confirm-action-title">
        <div className={styles.header}>
          <h3 id="confirm-action-title">{request.title}</h3>
          <button type="button" onClick={onClose} disabled={submitting}>Закрыть</button>
        </div>
        <div className={styles.body}>
          <p>{request.description}</p>
          <div className={styles.mathCard}>
            <span>Для подтверждения решите пример:</span>
            <strong>{first} + {second} = ?</strong>
            <input
              value={answer}
              onChange={(event) => setAnswer(event.target.value.replace(/[^0-9-]/g, ''))}
              placeholder="Введите ответ"
              autoFocus
            />
          </div>
          {error && <div className={styles.error}>{error}</div>}
        </div>
        <div className={styles.footer}>
          <button type="button" onClick={onClose} disabled={submitting}>Отмена</button>
          <button type="button" className={styles.primary} onClick={() => { void submit(); }} disabled={submitting || !answer.trim()}>
            {submitting ? 'Выполняется…' : request.confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
