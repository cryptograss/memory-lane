"""Give messages, Motions and notes their Ethereum block heights.

Ingest leaves eth_blockheight null so it never waits on a node. This fills
it in afterwards: sync anchors from a node up to the head, then interpolate
everything the anchors now cover. Idempotent; run it on a timer.
"""

from django.core.management.base import BaseCommand
from django.db.models import Min

from conversations.models import Message, Motion, Note
from conversations.services import eth_blocks


class Command(BaseCommand):
    help = "Sync block anchors from an Ethereum node and stamp eth_blockheight where it is missing."

    def add_arguments(self, parser):
        parser.add_argument('--rpc-url', default=None,
                            help=f'JSON-RPC endpoint (default: $ETH_RPC_URL or {eth_blocks.DEFAULT_RPC_URL})')
        parser.add_argument('--max-batches', type=int, default=500,
                            help=f'Cap on RPC requests of {eth_blocks.BATCH_SIZE} blocks each')
        parser.add_argument('--no-sync', action='store_true',
                            help='Use only the anchors already stored; make no RPC calls')

    def handle(self, *args, **options):
        if not options['no_sync']:
            earliest_ms = (Message.objects.filter(eth_blockheight__isnull=True, timestamp__isnull=False)
                           .aggregate(t=Min('timestamp'))['t'])
            if earliest_ms is not None:
                added = eth_blocks.sync_anchors(earliest_ms // 1000, url=options['rpc_url'],
                                                max_batches=options['max_batches'])
                self.stdout.write(f"anchors added: {added}")

        clock = eth_blocks.BlockClock()
        messages = eth_blocks.stamp_messages(clock)
        motions = eth_blocks.stamp_created(clock, Motion)
        notes = eth_blocks.stamp_created(clock, Note)
        self.stdout.write(self.style.SUCCESS(
            f"stamped messages={messages} motions={motions} notes={notes}"))
