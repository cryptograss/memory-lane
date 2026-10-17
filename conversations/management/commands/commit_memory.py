"""Write the record out as a commitment bundle (commitment/SPEC.md).

    python manage.py commit_memory /path/to/bundle --rehearsal
    python manage.py commit_memory /path/to/bundle --epoch 1

Reads the database and writes files; it changes nothing in the record and
sends nothing anywhere. A real (sealed) commitment needs the salt key in
MEMORY_COMMITMENT_KEY, 64 hex digits; there is no fallback, because a bundle
salted with a key nobody kept cannot be compared with the next one. A
rehearsal uses a throwaway key and says so in its manifest.

    --withhold table:raw --withhold kind:tool_result --withhold sender:fibonacci

leaves those leaves out of this edition's files (their hashes are still
there, and the roots are the same). What to disclose is a separate decision
from what to commit, and can be made later, leaf by leaf.
"""
import os
import secrets
import subprocess
import sys
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from commitment import export as commitment_export
from conversations.services import eth_blocks


class Command(BaseCommand):
    help = 'Write the record out as a commitment bundle: chunk files, a manifest, and a verifier.'

    def add_arguments(self, parser):
        parser.add_argument('out', help='An empty or new directory for the bundle')
        parser.add_argument('--epoch', type=int, help='The number of this commitment (1 for the first)')
        parser.add_argument('--rehearsal', action='store_true',
                            help='Use a throwaway salt key; the manifest is marked unsealed')
        parser.add_argument('--eth-block', type=int,
                            help='The Ethereum block of this snapshot (default: the finalized head)')
        parser.add_argument('--withhold', action='append', default=[], metavar='RULE',
                            help='table:<name>, kind:<kind> or sender:<name>; may be repeated')
        parser.add_argument('--allow-missing', action='store_true',
                            help='Skip tables an older copy of the database lacks (rehearsals only)')

    def handle(self, *args, **options):
        if options['rehearsal']:
            secret, sealed, epoch = secrets.token_bytes(32), False, 0
        else:
            if options['allow_missing']:
                raise CommandError('A sealed commitment covers every table; --allow-missing is for rehearsals.')
            if not options['epoch'] or options['epoch'] < 1:
                raise CommandError('A sealed commitment needs --epoch (1 for the first).')
            key = os.environ.get('MEMORY_COMMITMENT_KEY', '').strip()
            try:
                secret = bytes.fromhex(key)
            except ValueError:
                secret = b''
            if len(secret) != 32:
                raise CommandError('MEMORY_COMMITMENT_KEY must be set to 64 hex digits (or use --rehearsal).')
            sealed, epoch = True, options['epoch']

        eth_block = options['eth_block']
        if eth_block is None:
            try:
                eth_block = eth_blocks.fetch_head(eth_blocks.rpc_url())[0]
            except Exception as e:  # any failure to reach a node: ask for the number instead
                raise CommandError(f'Could not fetch the finalized block ({e}); pass --eth-block.')

        self.stdout.write(f"{'Sealed commitment' if sealed else 'Rehearsal'}, epoch {epoch}, at block {eth_block:,}")
        try:
            manifest, edition = commitment_export.export(
                options['out'], secret, epoch, eth_block, sealed, withhold=options['withhold'],
                allow_missing=options['allow_missing'], log=self.stdout.write)
        except commitment_export.ExportError as e:
            raise CommandError(str(e))

        out = Path(options['out'])
        self.stdout.write(f"\n{manifest['totals']['leaves']:,} leaves in {manifest['totals']['chunks']} chunks, "
                          f"{len(manifest['views'])} views, {edition['seconds']}s")
        for rule, count in sorted(edition['withheld'].items()):
            self.stdout.write(f'  withheld by {rule}: {count:,}')
        self.stdout.write(f"root, SHA-256:      {manifest['root']['sha256']}")
        self.stdout.write(f"root, SHA3-256:     {manifest['root']['sha3_256']}")
        self.stdout.write(f"manifest, SHA-256:  {edition['manifest_sha256']}")

        # The bundle has to stand on its own: check it with the copy of the
        # verifier that was just written into it, not with this process.
        result = subprocess.run([sys.executable, str(out / 'verify_memory.py'), str(out)],
                                capture_output=True, text=True)
        if result.returncode != 0:
            self.stdout.write(result.stdout[-2000:])
            raise CommandError('The bundle does not verify against its own manifest.')
        self.stdout.write(result.stdout.strip().splitlines()[-5])
        self.stdout.write(self.style.SUCCESS(f'Verified with its own verify_memory.py. Bundle: {out}'))
