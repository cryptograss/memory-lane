"""Apply pattern redaction to messages already in the record.

The importer redacts every line from now on; this catches up what was
stored before. Dry run by default: it reports how many messages would
change and shows each change only as redacted context, so running it
never prints a secret.

    python manage.py redact_stored            # report
    python manage.py redact_stored --apply    # rewrite the rows
"""

from django.core.management.base import BaseCommand

from conversations.models import Message, RawImportedContent
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
        self.shown = 0
        verb = 'redacted' if options['apply'] else 'would redact'
        # Message content is what the views render; raw_data is the line as
        # imported, which the legacy heap endpoints serve as-is.
        for model, field, noun in ((Message, 'content', 'messages'),
                                   (RawImportedContent, 'raw_data', 'raw imported lines')):
            redactions, changed = self.sweep(model, field, options)
            self.stdout.write(self.style.SUCCESS(f'{verb} {redactions} values in {changed} {noun}'))

    def sweep(self, model, field, options):
        changed = redactions = 0
        last = None
        while True:
            rows = model.objects.order_by('id')
            if last is not None:
                rows = rows.filter(id__gt=last)
            rows = list(rows.values_list('id', field)[:options['chunk']])
            if not rows:
                break
            last = rows[-1][0]
            for row_id, value in rows:
                new, count = redact_value(value)
                if not count:
                    continue
                changed += 1
                redactions += count
                for snippet in contexts(new):
                    if self.shown < options['show']:
                        self.stdout.write(f'  {str(row_id)[:8]} …{snippet}')
                        self.shown += 1
                if options['apply']:
                    model.objects.filter(id=row_id).update(**{field: new})
        return redactions, changed
