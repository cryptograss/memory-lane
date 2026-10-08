"""Open what was taken back, with the recovery key's private half -- and put it back.

    manage.py unseal --list [MESSAGE_ID] [--by NAME] [--since 2026-10-08T15:00]
        who took back what, when, each sealed copy numbered (no key needed)
    manage.py unseal MESSAGE_ID [--copy N]
        what that message said before its first takeback (or copy N)
    manage.py unseal MESSAGE_ID --restore [--copy N]
        put it back as it was before its first takeback (or as copy N has it)
    manage.py unseal --restore --by NAME [--since TIME]
        undo everything NAME took back (since TIME): each message as it was
        before NAME first touched it -- after a captured account, say

A message edited twice by someone else (original, then A, then B) has two
sealed copies, the original and A: put back, it's the original, not A. What
a message said when it's put back is sealed too (a copy "replaced"), so a
putting back can itself be undone.

An admin's page can do the same, one message at a time, opening the seals
in the browser (moods.html): this is for when the page won't do -- many
messages at once, or a page you'd rather not hand the key to.

The private half is read from standard input or a prompt, never from the
command line or a file: used for this one run, not kept. See
conversations/services/sealing.py and retract.py.
"""

import getpass
import json
import sys

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Open sealed copies of what was taken back, with the recovery key's private half; --restore puts it back."

    def add_arguments(self, parser):
        parser.add_argument('message_id', nargs='?')
        parser.add_argument('--list', action='store_true', help='who took back what, when (no key needed)')
        parser.add_argument('--by', help='only what this name took back')
        parser.add_argument('--since', help='only what was taken back since this date or time')
        parser.add_argument('--copy', type=int, help='this sealed copy (its number in --list), not the earliest')
        parser.add_argument('--restore', action='store_true', help='put it back')

    def handle(self, *args, **opts):
        from conversations.models import SealedCopy
        copies = SealedCopy.objects.order_by('at', 'id')
        if opts['message_id']:
            copies = copies.filter(message_id=opts['message_id'])
        if opts['by']:
            copies = copies.filter(by=opts['by'])
        if opts['since']:
            copies = copies.filter(at__gte=opts['since'])
        if opts['list']:
            for c in copies:
                self.stdout.write(f'{c.id:>6}  {c.at:%Y-%m-%d %H:%M}  {c.by:<12} {c.kind:<8} #{c.mood_slug}  {c.message_id}')
            return
        if opts['copy']:
            copies = copies.filter(id=opts['copy'])
        if not opts['message_id'] and not (opts['restore'] and opts['by']):
            raise CommandError('a message id; or --restore --by NAME [--since TIME]; or --list')
        # For each message, its earliest copy among these: as it was before the first takeback.
        earliest = {}
        for c in copies:
            earliest.setdefault(c.message_id, c)
        if not earliest:
            raise CommandError('nothing sealed matches')
        key = self.key()
        for copy in earliest.values():
            payload = self.open(copy, key)
            if not opts['restore']:
                self.stdout.write(json.dumps({'copy': copy.id, 'kind': copy.kind, 'by': copy.by, 'at': copy.at.isoformat(),
                                              'content': payload['content'],
                                              'media': [m['sha'] for m in payload['media']]}, indent=2, ensure_ascii=False))
                continue
            self.restore(copy, payload)
            self.stdout.write(f'put back: {copy.message_id} as it was before {copy.by} {copy.kind} it '
                              f'({copy.at:%Y-%m-%d %H:%M}, copy {copy.id})')

    def key(self):
        return (sys.stdin.readline() if not sys.stdin.isatty() else getpass.getpass('recovery private key: ')).strip()

    def open(self, copy, key):
        from conversations.services.retract import Refused, opened
        from conversations.services.sealing import SealingError, unseal
        try:
            return opened(copy, unseal(copy.sealed, key))
        except (SealingError, Refused) as e:
            raise CommandError(f'copy {copy.id}: {e}')

    def restore(self, copy, payload):
        from conversations.services.retract import Refused, put_back
        try:
            put_back(copy, payload, 'recovery')
        except Refused as e:
            raise CommandError(str(e))
