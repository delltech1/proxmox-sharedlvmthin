#!/usr/bin/env python3
"""Fail closed unless a successful paginated GitHub release response lacks a tag."""
import json
from pathlib import Path
import re
import sys


def unique_keys(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('duplicate response key')
        result[key] = value
    return result


def assert_absent(raw, tag):
    if not re.fullmatch(r'v[0-9][A-Za-z0-9._+-]*', tag):
        raise ValueError('invalid release tag')
    def invalid_constant(value):
        raise ValueError(f'invalid JSON constant: {value}')
    pages = json.loads(raw, object_pairs_hook=unique_keys, parse_constant=invalid_constant)
    if not isinstance(pages, list) or not pages:
        raise ValueError('missing paginated release response')
    seen = set()
    for page in pages:
        if not isinstance(page, list):
            raise ValueError('invalid release page')
        for release in page:
            if not isinstance(release, dict):
                raise ValueError('invalid release record')
            name = release.get('tag_name')
            if not isinstance(name, str) or not name or name in seen:
                raise ValueError('missing or duplicate release tag')
            seen.add(name)
            if name == tag:
                raise ValueError('release already exists; refusing to overwrite it')


def main():
    if len(sys.argv) != 3:
        raise ValueError('usage: assert-release-absent.py RESPONSE TAG')
    assert_absent(Path(sys.argv[1]).read_text(encoding='utf-8'), sys.argv[2])


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError) as exc:
        print(f'Release publication refused: {exc}', file=sys.stderr)
        raise SystemExit(1)
