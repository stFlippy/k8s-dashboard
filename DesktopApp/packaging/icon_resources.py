"""Prepare the existing Bender artwork for Windows and verify embedded PE icons."""
import argparse
import hashlib
import json
from pathlib import Path
import struct

SIZES = (16, 24, 32, 48, 64, 128, 256)
ROOT = Path(__file__).resolve().parent.parent


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def prepare(source, destination):
    from PyQt5.QtCore import QByteArray, QBuffer, QIODevice, Qt
    from PyQt5.QtGui import QImage, QPainter
    original = QImage(str(source))
    if original.isNull():
        raise RuntimeError(f'Cannot read icon source: {source}')
    frames = []
    for size in SIZES:
        image = QImage(size, size, QImage.Format_ARGB32)
        image.fill(Qt.transparent)
        scaled = original.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        painter = QPainter(image)
        painter.drawImage((size - scaled.width()) // 2, (size - scaled.height()) // 2, scaled)
        painter.end()
        data = QByteArray()
        buffer = QBuffer(data)
        if not buffer.open(QIODevice.WriteOnly) or not image.save(buffer, 'PNG'):
            raise RuntimeError(f'Cannot encode {size}px icon frame')
        buffer.close()
        frames.append(bytes(data))
    offset = 6 + 16 * len(frames)
    entries = []
    for size, data in zip(SIZES, frames):
        entries.append(struct.pack('<BBBBHHII', size % 256, size % 256, 0, 0, 1, 32, len(data), offset))
        offset += len(data)
    destination.write_bytes(struct.pack('<HHH', 0, 1, len(frames)) + b''.join(entries) + b''.join(frames))
    return {'source_sha256': digest(source), 'ico_sha256': digest(destination), 'sizes': SIZES}


def verify(executable, icon):
    import pefile
    raw = icon.read_bytes()
    reserved, kind, count = struct.unpack_from('<HHH', raw)
    if (reserved, kind, count) != (0, 1, len(SIZES)):
        raise RuntimeError('ICO header/frame count differs from the Bender icon contract')
    expected = []
    for index, size in enumerate(SIZES):
        width, height, colors, _, planes, bits, length, offset = struct.unpack_from('<BBBBHHII', raw, 6 + index * 16)
        if (width or 256, height or 256) != (size, size):
            raise RuntimeError('ICO frame dimensions differ from the icon contract')
        data = raw[offset:offset + length]
        if len(data) != length:
            raise RuntimeError('Truncated ICO frame')
        expected.append(((width, height, colors, planes, bits, length), data))
    icons, groups = {}, []
    with pefile.PE(str(executable), fast_load=True) as pe:
        pe.parse_data_directories(directories=[2])
        for category in getattr(pe, 'DIRECTORY_ENTRY_RESOURCE', ()).entries:
            if category.id not in (3, 14):
                continue
            for resource in category.directory.entries:
                for language in resource.directory.entries:
                    entry = language.data.struct
                    payload = pe.get_data(entry.OffsetToData, entry.Size)
                    if category.id == 3:
                        icons[(resource.id, language.id)] = payload
                    else:
                        groups.append((resource.id, language.id, payload))
    if not groups:
        raise RuntimeError('EXE has no RT_GROUP_ICON resource')
    for group_id, language, payload in groups:
        if struct.unpack_from('<HHH', payload) != (0, 1, count):
            raise RuntimeError('EXE icon group differs from the prepared ICO')
        for index, (header, expected_data) in enumerate(expected):
            width, height, colors, _, planes, bits, length, resource_id = struct.unpack_from('<BBBBHHIH', payload, 6 + index * 14)
            if (width, height, colors, planes, bits, length) != header:
                raise RuntimeError('Embedded icon frame header differs from the prepared ICO')
            if icons.get((resource_id, language)) != expected_data:
                raise RuntimeError('Embedded icon pixels differ from the prepared Bender ICO')
    return {'passed': True, 'exe_sha256': digest(executable), 'ico_sha256': digest(icon),
            'sizes': SIZES, 'group_count': len(groups), 'comparison': 'exact embedded RT_ICON payloads'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('prepare', 'verify'))
    parser.add_argument('--source', type=Path, default=ROOT / 'public/icon.png')
    parser.add_argument('--ico', type=Path, default=ROOT / 'public/icon.ico')
    parser.add_argument('--exe', type=Path)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    if args.command == 'verify' and args.exe is None:
        parser.error('verify requires --exe')
    result = prepare(args.source, args.ico) if args.command == 'prepare' else verify(args.exe, args.ico)
    content = json.dumps(result, indent=2)
    if args.report:
        args.report.write_text(content, encoding='utf-8')
    print(content)
