#!/usr/bin/env python3
"""Apply or check the public sections shared by Axon Memory and Billo Tech."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
SHARED = ROOT / 'axonmemory' / 'shared-sections.html'
PAGES = (ROOT / 'axonmemory' / 'index.html', ROOT / 'billo-tech' / 'index.html')
START = '<section class="section" id="publications">'
END = '</section>'


def updated_page(page, shared):
    start = page.index(START)
    repository = page.index('<section class="section" id="repositories">', start)
    end = page.index(END, repository) + len(END)
    return page[:start] + shared + page[end:]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='fail if either page differs')
    args = parser.parse_args()
    shared = SHARED.read_text(encoding='utf-8').strip()
    drift = []
    for path in PAGES:
        original = path.read_text(encoding='utf-8')
        try:
            updated = updated_page(original, shared)
        except ValueError:
            print(f'{path}: required section not found', file=sys.stderr)
            return 1
        if original != updated:
            drift.append(path)
            if not args.check:
                path.write_text(updated, encoding='utf-8')
                print(f'updated {path}')
    if args.check and drift:
        for path in drift:
            print(f'shared content differs: {path}', file=sys.stderr)
        return 1
    if not drift:
        print('shared sections match both pages')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
