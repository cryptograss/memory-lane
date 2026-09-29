"""Open a Motion and attach existing messages to it.

The corpus predates Motions, so conversation is attached deliberately rather
than guessed at. The only handle available today is the Claude Code session,
which is a runtime instance rather than a subject, so a Motion may need
several sessions attached. Repeatable and idempotent.
"""

import json
from collections import defaultdict

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from conversations.models import Message, Motion


class Command(BaseCommand):
    help = "Open a Motion (if needed) and attach messages from sessions to it."

    def add_arguments(self, parser):
        parser.add_argument('slug', help='Stable key, e.g. delivery-kid')
        parser.add_argument('--session', action='append', default=[], metavar='UUID',
                            help='Session whose messages belong to this Motion (repeatable)')
        parser.add_argument('--jsonl', action='append', default=[], metavar='PATH',
                            help='Transcript file whose messages belong to this Motion (repeatable). '
                                 'Also restores the session id the old importer dropped.')
        parser.add_argument('--title', default='', help='Human-facing title')
        parser.add_argument('--description', default='', help='What this Motion is for')
        parser.add_argument('--block', type=int, help='Block height at which it was opened')
        parser.add_argument('--reassign', action='store_true',
                            help='Also move messages already attached to another Motion')
        parser.add_argument('--dry-run', action='store_true',
                            help='Report what would change and write nothing')

    @transaction.atomic
    def handle(self, *args, **options):
        slug = options['slug']
        sessions = options['session']
        dry_run = options['dry_run']

        motion, created = Motion.objects.get_or_create(slug=slug)
        for field in ('title', 'description'):
            if options[field]:
                setattr(motion, field, options[field])
        if options['block']:
            motion.eth_blockheight = options['block']
        if not dry_run:
            motion.save()

        self.stdout.write(f"{'Opened' if created else 'Found'} Motion '{slug}'")

        total = 0
        for session in sessions:
            candidates = Message.objects.filter(session_id=session)
            if not candidates.exists():
                raise CommandError(f"No messages for session {session}")

            targets = candidates if options['reassign'] else candidates.filter(motion__isnull=True)
            already = candidates.filter(motion=motion).count()
            count = targets.exclude(motion=motion).count()

            if not dry_run:
                targets.update(motion=motion)

            total += count
            note = f" ({already} already attached)" if already else ""
            self.stdout.write(f"  {session}: {count} messages{note}")

        for path in options['jsonl']:
            count, restored = self.attach_transcript(motion, path, options['reassign'])
            total += count
            self.stdout.write(f"  {path}: {count} messages ({restored} had lost their session)")

        verb = 'would attach' if dry_run else 'attached'
        self.stdout.write(self.style.SUCCESS(f"{verb} {total} messages to '{slug}'"))

        if dry_run:
            transaction.set_rollback(True)

    def attach_transcript(self, motion, path, reassign):
        """Attach every message a transcript holds, by uuid.

        Until the importer kept session ids (2026-09), every message was
        stored without one, so --session can't find the early part of a
        long conversation. The transcript still names each message's
        session; this puts it back and attaches the message. Only a missing
        session id is filled in; an existing one is never changed.
        Returns (attached, sessions restored).
        """
        by_session = defaultdict(list)
        with open(path) as f:
            for line in f:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get('uuid') and event.get('sessionId'):
                    by_session[event['sessionId']].append(event['uuid'])
        if not by_session:
            raise CommandError(f"No messages in {path}")

        attached = restored = 0
        for session, ids in by_session.items():
            for i in range(0, len(ids), 1000):
                messages = Message.objects.filter(id__in=ids[i:i + 1000])
                restored += messages.filter(session_id__isnull=True).update(session_id=session)
                targets = messages if reassign else messages.filter(motion__isnull=True)
                attached += targets.exclude(motion=motion).update(motion=motion)
        return attached, restored
