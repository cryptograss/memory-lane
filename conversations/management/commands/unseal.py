"""Open what was taken back, with the recovery key's private half -- and put it back.

    manage.py unseal --list [--by NAME] [--since 2026-10-08]
    manage.py unseal MESSAGE_ID            (shows what it said; asks for the key)
    manage.py unseal MESSAGE_ID --restore  (puts it back, pictures and all)

The private half is read from standard input or a prompt, never from the
command line or a file: it's used for this one run and not kept. See
conversations/services/sealing.py and retract.py.
"""

import base64
import getpass
import json
import sys

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Open sealed copies of what was taken back, with the recovery key's private half; --restore puts it back."

    def add_arguments(self, parser):
        parser.add_argument('message_id', nargs='?')
        parser.add_argument('--list', action='store_true', help='list sealed copies: who took back what, when')
        parser.add_argument('--by', help='only those taken back by this name')
        parser.add_argument('--since', help='only those since this date or time')
        parser.add_argument('--restore', action='store_true', help='put the newest sealed copy back')

    def handle(self, *args, **opts):
        from conversations.models import SealedCopy
        if opts['list']:
            copies = SealedCopy.objects.order_by('at')
            if opts['by']:
                copies = copies.filter(by=opts['by'])
            if opts['since']:
                copies = copies.filter(at__gte=opts['since'])
            for c in copies:
                self.stdout.write(f'{c.at:%Y-%m-%d %H:%M} {c.by:<12} {c.kind:<8} #{c.mood_slug} {c.message_id}')
            return
        if not opts['message_id']:
            raise CommandError('a message id, or --list')
        copy = SealedCopy.objects.filter(message_id=opts['message_id']).order_by('-at').first()
        if copy is None:
            raise CommandError('nothing sealed for that message')
        payload = self.open(copy)
        if not opts['restore']:
            self.stdout.write(json.dumps({'kind': copy.kind, 'by': copy.by, 'at': copy.at.isoformat(),
                                          'content': payload['content'],
                                          'media': [m['sha'] for m in payload['media']]}, indent=2, ensure_ascii=False))
            return
        self.restore(copy, payload)
        self.stdout.write(f'put back: {copy.message_id} (taken back by {copy.by}, {copy.kind}, {copy.at:%Y-%m-%d %H:%M})')

    def open(self, copy):
        from conversations.services.sealing import SealingError, unseal
        key = (sys.stdin.readline() if not sys.stdin.isatty() else getpass.getpass('recovery private key: ')).strip()
        try:
            return json.loads(unseal(copy.sealed, key))
        except SealingError as e:
            raise CommandError(str(e))

    def restore(self, copy, payload):
        from conversations.models import Media, Message, MessageChange, ThinkingEntity
        message = Message.objects.filter(id=copy.message_id).first()
        if message is None:
            raise CommandError('the message itself is gone from the record')
        for m in payload['media']:
            if not Media.objects.filter(sha256=m['sha']).exists():
                data = base64.b64decode(m['data'])
                Media.objects.create(sha256=m['sha'], mime=m['mime'], data=data, size=len(data),
                                     added_by=ThinkingEntity.objects.filter(name=m.get('added_by')).first(),
                                     **({'license': m['license']} if m.get('license') else {}))
        type(message).objects.filter(pk=message.pk).update(content=payload['content'])
        MessageChange.objects.update_or_create(message=message, defaults={
            'mood': message.mood, 'kind': 'restored', 'by': 'recovery', 'reached': []})
