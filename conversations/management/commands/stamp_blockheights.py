"""Give messages, Moods and notes their Ethereum block heights.

Ingest leaves eth_blockheight null so it never waits on a node. This fills
it in afterwards: sync anchors from a node up to the finalized head, then
interpolate everything the anchors now cover. Idempotent; run it on a timer.
A failed sync is reported and stamping goes ahead with the anchors on hand.
"""

from django.core.management.base import BaseCommand
from django.db.models import Min

from conversations.models import Message, Mood, Note
from conversations.services import eth_blocks


def earliest_unstamped():
    """Unix seconds of the oldest post-Merge thing still waiting for a height, or None."""
    floor = eth_blocks.MERGE_TIMESTAMP
    candidates = []
    ms = (Message.objects.filter(eth_blockheight__isnull=True, timestamp__gte=floor * 1000)
          .aggregate(t=Min('timestamp'))['t'])
    if ms is not None:
        candidates.append(ms // 1000)
    for model in (Mood, Note):
        created = model.objects.filter(eth_blockheight__isnull=True).aggregate(t=Min('created_at'))['t']
        if created is not None:
            candidates.append(max(int(created.timestamp()), floor))
    return min(candidates) if candidates else None


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
        since = None if options['no_sync'] else earliest_unstamped()
        if since is not None:
            try:
                added = eth_blocks.sync_anchors(since, url=options['rpc_url'],
                                                max_batches=options['max_batches'])
                self.stdout.write(f"anchors added: {added}")
            except Exception as e:  # a bad node reply must not stop stamping what we can
                self.stderr.write(f"anchor sync failed, stamping with existing anchors: {e}")

        clock = eth_blocks.BlockClock()
        messages = eth_blocks.stamp_messages(clock)
        moods = eth_blocks.stamp_created(clock, Mood)
        notes = eth_blocks.stamp_created(clock, Note)
        self.stdout.write(self.style.SUCCESS(
            f"stamped messages={messages} moods={moods} notes={notes}"))
