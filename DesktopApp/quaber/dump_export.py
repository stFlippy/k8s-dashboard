"""Export the visible inventory as CSV or a dependency-free XLSX workbook."""
import os
from pathlib import Path
import tempfile
from itertools import chain
from zipfile import ZipFile, ZIP_DEFLATED
from xml.sax.saxutils import escape
from .inventory import CSV_FIELDS, write_inventory


def column_name(index):
    result = ''
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def export_dump(destination, rows, *, fields=CSV_FIELDS):
    fields = tuple(fields)
    if not fields or len(fields) > 16384:
        raise ValueError("Превышен предел колонок отчёта.")
    destination = Path(destination)
    if destination.suffix.lower() == ".csv":
        return write_inventory(destination, rows, fields=fields)
    if destination.suffix.lower() != ".xlsx":
        raise ValueError("Выберите CSV или XLSX.")
    if len(rows) >= 1048576:
        raise ValueError("Превышен предел строк XLSX.")
    namespace = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as stream:
        temporary = Path(stream.name)
    try:
        with ZipFile(temporary, 'w', ZIP_DEFLATED) as archive:
            archive.writestr('[Content_Types].xml', '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>')
            archive.writestr('_rels/.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
            archive.writestr('xl/workbook.xml', f'<workbook xmlns="{namespace}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Dumps" sheetId="1" r:id="rId1"/></sheets></workbook>')
            archive.writestr('xl/_rels/workbook.xml.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
            with archive.open('xl/worksheets/sheet1.xml', 'w') as sheet:
                def write(text):
                    sheet.write(text.encode('utf-8'))
                write(f'<worksheet xmlns="{namespace}"><sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews><cols>')
                widths = (32, 24, 18, 32, 70, 24, 26, 20, 16)
                for column in range(1, len(fields) + 1):
                    width = widths[column - 1] if column <= len(widths) else 28
                    write(f'<col min="{column}" max="{column}" width="{width}" customWidth="1"/>')
                write('</cols><sheetData>')
                for number, row in enumerate(chain((fields,), rows), 1):
                    if len(row) != len(fields):
                        raise ValueError("Число полей строки не соответствует колонкам дампа.")
                    write(f'<row r="{number}">')
                    for column, value in enumerate(row):
                        text = ''.join(c if ord(c) >= 32 or c in '\t\r\n' else '\uFFFD' for c in str(value))
                        write(f'<c r="{column_name(column+1)}{number}" t="inlineStr"><is><t xml:space="preserve">{escape(text)}</t></is></c>')
                    write('</row>')
                write(f'</sheetData><autoFilter ref="A1:{column_name(len(fields))}{len(rows)+1}"/></worksheet>')
        os.replace(temporary, destination)
        return str(destination), None
    finally:
        temporary.unlink(missing_ok=True)
