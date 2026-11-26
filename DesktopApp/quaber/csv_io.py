"""The CSV serialization path for inventory exports."""
import csv


def write_rows(stream,fields,rows,*,max_rows=None):
    writer = csv.writer(stream,quoting=csv.QUOTE_ALL)
    writer.writerow(fields)
    for number,row in enumerate(rows):
        if max_rows is not None and number>=max_rows: raise ValueError('CSV exceeds its row limit.')
        if len(row)!=len(fields): raise ValueError('CSV row does not match its schema.')
        writer.writerow(row)
