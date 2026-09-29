"""Ethereum block heights for the record.

A message's eth_blockheight is the chain head when it was written: the
latest block whose timestamp is at or before the message's timestamp.

Nothing here runs on the ingest path. Ingest must not depend on an outside
node being up, so messages arrive with eth_blockheight null and
`manage.py stamp_blockheights` fills them in afterwards:

1. sync_anchors() records real (number, timestamp) pairs from a node, one
   every ANCHOR_SPACING blocks, plus the head.
2. BlockClock.block_at() interpolates between the anchors either side of a
   timestamp. Post-merge every slot is exactly 12 seconds, so this is exact
   across any interval with no missed slot. Missed slots run about one per
   256 blocks, so elsewhere it is off by a block or two at most.

A timestamp past the newest anchor gets None rather than a guess; the next
run stamps it once an anchor covers it.
"""

import bisect
import logging
import math
import os

import requests

logger = logging.getLogger(__name__)

DEFAULT_RPC_URL = 'https://ethereum-rpc.publicnode.com'
ANCHOR_SPACING = 256
SECONDS_PER_SLOT = 12
BATCH_SIZE = 100


def rpc_url():
    return os.environ.get('ETH_RPC_URL', DEFAULT_RPC_URL)


def _post(url, payload):
    response = requests.post(url, json=payload, timeout=30,
                             headers={'User-Agent': 'memory-lane'})
    response.raise_for_status()
    return response.json()


def fetch_head(url):
    """(number, timestamp) of the latest block."""
    result = _post(url, {'jsonrpc': '2.0', 'id': 1, 'method': 'eth_getBlockByNumber',
                         'params': ['latest', False]})['result']
    return int(result['number'], 16), int(result['timestamp'], 16)


def fetch_blocks(url, numbers):
    """[(number, timestamp)] for the given block numbers, in one batch request."""
    payload = [{'jsonrpc': '2.0', 'id': n, 'method': 'eth_getBlockByNumber', 'params': [hex(n), False]}
               for n in numbers]
    replies = _post(url, payload)
    errors = [r['error'] for r in replies if r.get('error')]
    if errors:
        raise RuntimeError(f"RPC errors fetching blocks: {errors[:3]}")
    return [(int(r['result']['number'], 16), int(r['result']['timestamp'], 16)) for r in replies]


def sync_anchors(since_ts, url=None, max_batches=500):
    """
    Make sure anchors cover everything from since_ts (unix seconds) to the head.

    Returns the number of anchors added. Bounded by max_batches requests so a
    bad input can never turn into an unbounded loop against a metered node.
    """
    from conversations.models import BlockAnchor

    url = url or rpc_url()
    head_number, head_ts = fetch_head(url)

    # Blocks never outnumber slots, so counting back one block per slot lands
    # at or before the block at since_ts.
    start = head_number - math.ceil(max(head_ts - since_ts, 0) / SECONDS_PER_SLOT)
    start = max(start - start % ANCHOR_SPACING, 0)

    wanted = set(range(start, head_number + 1, ANCHOR_SPACING)) | {head_number}
    have = set(BlockAnchor.objects.filter(number__gte=start).values_list('number', flat=True))
    missing = sorted(wanted - have)

    added = 0
    for i in range(0, len(missing), BATCH_SIZE):
        if i // BATCH_SIZE >= max_batches:
            logger.warning(f"Stopped after {max_batches} batches; {len(missing) - added} anchors still missing")
            break
        blocks = fetch_blocks(url, missing[i:i + BATCH_SIZE])
        BlockAnchor.objects.bulk_create(
            [BlockAnchor(number=n, timestamp=t) for n, t in blocks], ignore_conflicts=True)
        added += len(blocks)
    return added


class BlockClock:
    """Timestamp -> block height, from the anchors in the database."""

    def __init__(self, anchors=None):
        if anchors is None:
            from conversations.models import BlockAnchor
            anchors = BlockAnchor.objects.order_by('number').values_list('number', 'timestamp')
        pairs = sorted(anchors)
        self.numbers = [n for n, _ in pairs]
        self.timestamps = [t for _, t in pairs]

    @property
    def covers(self):
        """(earliest, latest) unix seconds this clock can answer for, or None."""
        if not self.numbers:
            return None
        return self.timestamps[0], self.timestamps[-1]

    def block_at(self, ts):
        """The block at the head at unix time ts, or None if outside the anchors."""
        if not self.numbers or ts < self.timestamps[0] or ts > self.timestamps[-1]:
            return None
        i = bisect.bisect_right(self.timestamps, ts) - 1
        n0, t0 = self.numbers[i], self.timestamps[i]
        if ts == t0 or i + 1 == len(self.numbers):
            return n0
        n1, t1 = self.numbers[i + 1], self.timestamps[i + 1]
        estimate = n0 + (ts - t0) * (n1 - n0) // (t1 - t0)
        return min(max(estimate, n0), n1 - 1)


def stamp_messages(clock, chunk=5000):
    """Fill eth_blockheight on messages the clock covers. Returns how many."""
    from conversations.models import Message

    covers = clock.covers
    if not covers:
        return 0
    pending = (Message.objects
               .filter(eth_blockheight__isnull=True, timestamp__isnull=False,
                       timestamp__gte=covers[0] * 1000, timestamp__lte=covers[1] * 1000)
               .order_by('id'))

    stamped = 0
    last_id = None
    while True:
        page = pending if last_id is None else pending.filter(id__gt=last_id)
        rows = list(page.values_list('id', 'timestamp')[:chunk])
        if not rows:
            return stamped
        Message.objects.bulk_update(
            [Message(id=i, eth_blockheight=clock.block_at(ts // 1000)) for i, ts in rows],
            ['eth_blockheight'])
        stamped += len(rows)
        last_id = rows[-1][0]


def stamp_created(clock, model):
    """Fill eth_blockheight from created_at on a model that has both (Motion, Note)."""
    stamped = 0
    for obj in model.objects.filter(eth_blockheight__isnull=True):
        block = clock.block_at(int(obj.created_at.timestamp()))
        if block is not None:
            obj.eth_blockheight = block
            obj.save(update_fields=['eth_blockheight'])
            stamped += 1
    return stamped

