"""Apply pattern redaction to messages already in the record.

The importer redacts every line from now on; this catches up what was
stored before. Dry run by default: it reports how many messages would
change and shows each change only as redacted context, so running it
never prints a secret.

    python manage.py redact_stored            # report
    python manage.py redact_stored --apply    # rewrite the rows
"""

from django.core.management.base import BaseCommand

from conversations.models import Message
from conversations.services.redaction import MARK, redact_value


def contexts(value, width=40):
    """Snippets around each redaction in an already-redacted value."""
    text = value if isinstance(value, str) else str(value)
    at = text.find(MARK)
    while at != -1:
        yield text[max(0, at - width):at + len(MARK)].replace('\n', ' ')
        at = text.find(MARK, at + 1)


class Command(BaseCommand):
    help = "Pattern-redact message content stored before redaction existed (dry run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Rewrite the affected rows')
        parser.add_argument('--show', type=int, default=40, help='How many redacted contexts to print')
        parser.add_argument('--chunk', type=int, default=2000)

    def handle(self, *args, **options):
        changed = redactions = shown = 0
        last = None
        while True:
            rows = Message.objects.order_by('id')
            if last is not None:
                rows = rows.filter(id__gt=last)
            rows = list(rows.values_list('id', 'content')[:options['chunk']])
            if not rows:
                break
            last = rows[-1][0]
            for message_id, content in rows:
                new, count = redact_value(content)
                if not count:
                    continue
                changed += 1
                redactions += count
                for snippet in contexts(new):
                    if shown < options['show']:
                        self.stdout.write(f'  {str(message_id)[:8]} …{snippet}')
                        shown += 1
                if options['apply']:
                    Message.objects.filter(id=message_id).update(content=new)
        verb = 'redacted' if options['apply'] else 'would redact'
        self.stdout.write(self.style.SUCCESS(f'{verb} {redactions} values in {changed} messages'))
