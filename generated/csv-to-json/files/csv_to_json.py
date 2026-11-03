#!/usr/bin/env python3
"""Convert a CSV file with a header row into a JSON array of objects."""
import argparse
import csv
import json
import re
import sys

INT_RE = re.compile(r'^-?(0|[1-9]\d*)$')
FLOAT_RE = re.compile(r'^-?\d+\.\d+$')


def convert(v):
    s = v.strip()
    if INT_RE.match(s):
        return int(s)
    if FLOAT_RE.match(s):
        return float(s)
    return v


def main():
    p = argparse.ArgumentParser(
        description='Convert a CSV file (header row) to a JSON array of objects. '
                    'Integer and decimal values are written as numbers.')
    p.add_argument('input', help='path to input CSV file')
    p.add_argument('output', help='path to output JSON file')
    a = p.parse_args()

    try:
        with open(a.input, newline='', encoding='utf-8') as f:
            reader = csv.reader(f)
            header = next(reader, None)
            if not header or any(not h.strip() for h in header):
                print('error: missing or empty header row', file=sys.stderr)
                return 1
            rows = []
            for i, r in enumerate(reader, start=2):
                if not r:
                    continue
                if len(r) > len(header):
                    print('error: row %d has more cells than headers' % i,
                          file=sys.stderr)
                    return 1
                r = r + [''] * (len(header) - len(r))
                rows.append({h: convert(v) for h, v in zip(header, r)})
    except (OSError, UnicodeDecodeError, csv.Error) as e:
        print('error: cannot read %s: %s' % (a.input, e), file=sys.stderr)
        return 1

    try:
        with open(a.output, 'w', encoding='utf-8') as f:
            json.dump(rows, f, ensure_ascii=False, indent=2)
            f.write('\n')
    except OSError as e:
        print('error: cannot write %s: %s' % (a.output, e), file=sys.stderr)
        return 1

    print('Wrote %d rows to %s' % (len(rows), a.output))
    return 0


if __name__ == '__main__':
    sys.exit(main())
