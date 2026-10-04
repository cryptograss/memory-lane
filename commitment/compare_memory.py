#!/usr/bin/env python3
"""What changed in the record between two commitment bundles.

    python3 compare_memory.py OLDER_BUNDLE NEWER_BUNDLE [--show N]

A row is told apart by its key. Where both bundles disclose it, the record
text is compared. Where one withholds it, the leaf hashes are compared,
which is only meaningful if both bundles were salted with the same key (a
row's salt never changes, so an unchanged row has an unchanged leaf hash).

Exit status is 0 if nothing was altered or removed, 1 otherwise. Rows that
were only added do not count against it: the record is supposed to grow.
Needs only Python's standard library.
"""
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

LEAF = b'\x00'


def leaves(bundle):
    """key -> (SHA-256 leaf hash, SHA-256 of the record text or None if withheld)."""
    found = {}
    for path in sorted((bundle / 'chunks').rglob('*.tsv')):
        with open(path, 'rb') as f:
            for line in f:
                kind, key, a, b = line[:-1].split(b'\t', 3)
                if kind == b'D':
                    found[key] = (hashlib.sha256(LEAF + bytes.fromhex(a.decode()) + b).digest(),
                                  hashlib.sha256(b).digest())
                else:
                    found[key] = (bytes.fromhex(a.decode()), None)
    return found


def records(bundle, keys):
    """The disclosed record of each of `keys`, as parsed JSON."""
    found = {}
    for path in sorted((bundle / 'chunks').rglob('*.tsv')):
        with open(path, 'rb') as f:
            for line in f:
                kind, key, _, b = line[:-1].split(b'\t', 3)
                if kind == b'D' and key in keys:
                    found[key] = json.loads(b)
    return found


def main(argv):
    show = 10
    if '--show' in argv:
        at = argv.index('--show')
        show = int(argv[at + 1])
        del argv[at:at + 2]
    if len(argv) != 3:
        print(__doc__)
        return 2
    old_dir, new_dir = Path(argv[1]), Path(argv[2])
    old_manifest = json.loads((old_dir / 'manifest.json').read_bytes())
    new_manifest = json.loads((new_dir / 'manifest.json').read_bytes())
    same_key = old_manifest['salt_key_id'] == new_manifest['salt_key_id']
    old, new = leaves(old_dir), leaves(new_dir)

    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    altered, unknown = [], []
    for key in set(old) & set(new):
        (old_leaf, old_text), (new_leaf, new_text) = old[key], new[key]
        if old_text is not None and new_text is not None:
            if old_text != new_text:
                altered.append(key)
        elif same_key:
            if old_leaf != new_leaf:
                altered.append(key)
        else:
            unknown.append(key)
    altered.sort()

    def table(key):
        return key.split(b'/', 1)[0].decode()

    print(f"epoch {old_manifest['epoch']} (block {old_manifest['snapshot']['eth_block']}) -> "
          f"epoch {new_manifest['epoch']} (block {new_manifest['snapshot']['eth_block']})")
    if not same_key:
        print('The two bundles were salted with different keys: withheld rows cannot be compared.')
    for label, keys in (('added', added), ('removed', removed), ('altered', altered), ('not comparable', unknown)):
        counts = Counter(table(k) for k in keys)
        print(f"{label}: {len(keys)}" + (f"  ({', '.join(f'{n} {t}' for t, n in sorted(counts.items()))})" if keys else ''))

    if altered and show:
        before, after = records(old_dir, set(altered[:show])), records(new_dir, set(altered[:show]))
        print()
        for key in altered[:show]:
            if key in before and key in after:
                a, b = before[key], after[key]
                fields = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
                print(f"altered {key.decode()}: {', '.join(fields)}")
            else:
                print(f"altered {key.decode()}: (withheld in one bundle; the leaf hash differs)")
        if len(altered) > show:
            print(f'... and {len(altered) - show} more')
    if removed and show:
        print()
        for key in removed[:show]:
            print(f'removed {key.decode()}')
        if len(removed) > show:
            print(f'... and {len(removed) - show} more')
    return 1 if altered or removed else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
