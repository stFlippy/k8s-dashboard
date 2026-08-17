import { useMemo, useRef, useState } from 'react';
import styles from './CodeEditor.module.css';

interface Props { value: string; onChange: (value: string) => void; label?: string; }

export function CodeEditor({ value, onChange, label = 'JSON / YAML 1.2' }: Props) {
  const [search, setSearch] = useState('');
  const [currentMatch, setCurrentMatch] = useState(0);
  const [scrollTop, setScrollTop] = useState(0);
  const editorRef = useRef<HTMLTextAreaElement>(null);
  const lines = useMemo(() => Array.from({ length: Math.max(1, value.split('\n').length) }, (_, index) => index + 1), [value]);
  const matches = useMemo(() => {
    const needle = search.trim().toLocaleLowerCase(); if (!needle) return [] as number[];
    const found: number[] = []; const text = value.toLocaleLowerCase(); let offset = 0;
    while ((offset = text.indexOf(needle, offset)) >= 0) { found.push(offset); offset += Math.max(1, needle.length); }
    return found;
  }, [search, value]);
  const navigate = (direction: 1 | -1) => {
    const editor = editorRef.current; const query = search.trim(); if (!editor || !query || !matches.length) return;
    const selected = matches.findIndex((index) => index === editor.selectionStart);
    const matchIndex = selected >= 0 ? (selected + direction + matches.length) % matches.length : direction > 0 ? 0 : matches.length - 1; const index = matches[matchIndex];
    setCurrentMatch(matchIndex + 1); editor.focus(); editor.setSelectionRange(index, index + query.length);
    const line = value.slice(0, index).split('\n').length - 1; editor.scrollTop = Math.max(0, line * 20.15 - editor.clientHeight / 2);
  };
  return <div className={styles.root}>
    <div className={styles.toolbar}><span>{label}</span><div><input value={search} onChange={(event) => { setSearch(event.target.value); setCurrentMatch(0); }} onKeyDown={(event) => { if (event.key === 'Enter') { event.preventDefault(); navigate(1); } }} placeholder="Поиск…"/><span className={styles.matchCount}>{search.trim() ? `${currentMatch}/${matches.length}` : '0/0'}</span><button type="button" onClick={() => navigate(-1)} disabled={!matches.length} title="Предыдущее совпадение">↑</button><button type="button" onClick={() => navigate(1)} disabled={!matches.length} title="Следующее совпадение">↓</button></div></div>
    <div className={styles.editor}><pre style={{ transform: `translateY(-${scrollTop}px)` }}>{lines.join('\n')}</pre><textarea ref={editorRef} spellCheck={false} value={value} onScroll={(event) => setScrollTop(event.currentTarget.scrollTop)} onChange={(event) => onChange(event.target.value)} /></div>
  </div>;
}
