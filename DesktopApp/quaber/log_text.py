"""Cancelable plain-text work, independent of Qt widgets/documents."""
import re
import time


def search_positions(text, needle, canceled):
    matches, utf16 = [], []
    position = units = 0
    if needle:
        for match in re.finditer(re.escape(needle), text, re.IGNORECASE):
            if len(matches) % 128 == 0 and canceled.is_set():
                raise RuntimeError('Log preparation canceled.')
            start, end = match.span()
            units += len(text[position:start].encode('utf-16-le'))//2
            first = units
            units += len(text[start:end].encode('utf-16-le'))//2
            matches.append((start, end))
            utf16.append((first, units))
            position = end
            if len(matches) == 10000: break
    return matches, utf16


def prepare_text(text, needle, canceled):
    if canceled.is_set(): raise RuntimeError('Log preparation canceled.')
    started = time.perf_counter()
    text = text.replace('\r\n', '\n').replace('\r', '\n').replace('\u2029', '\n').replace('\u2028', '\n')
    normalized = time.perf_counter()
    stats = len(text.splitlines()), len(text.encode('utf-8'))
    counted = time.perf_counter()
    if stats[0] > 50000:
        from .kubernetes import LogTooLarge
        raise LogTooLarge('Слишком много строк для просмотра.')
    matches, utf16 = search_positions(text, needle, canceled)
    if canceled.is_set(): raise RuntimeError('Log preparation canceled.')
    return {'text': text, 'stats': stats, 'needle': needle, 'matches': matches, 'utf16': utf16,
            'timings': {'normalize_ms': (normalized-started)*1000, 'stats_ms': (counted-normalized)*1000,
                        'prepare_search_ms': (time.perf_counter()-counted)*1000}}
