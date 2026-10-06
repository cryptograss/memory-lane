"""Links between Moods and messages, and what links to what.

In what anyone writes:

    #general              a Mood, by its name now or one it had: a link to it
    #m-<id>               a message, by its id or the id's first 8 characters
    .../moods/<slug>/#m-<id>   a message's link, as its time copies it

A message link renders as a card -- who said it, where, its first words --
that goes there (mood_view._inline). The other way round, every message
someone linked to shows who linked it ("linked from"), and every message
someone replied to (a reply opens '↩ #m-<id>') who replied.

What links to what is read from the record, not kept apart: an index built
by one look over every Mood's messages that carry '#m-' (a second or so),
then kept up by looking only at what's new, and rebuilt every hour for
anything written out of order (an import). It lives in the cache, shared by
every worker.
"""

import re
import time

from django.core.cache import cache

UUID = r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
# A message, by id or its first 8, as '#m-...' alone or ending a permalink.
TARGET = re.compile(rf'#m-({UUID}|[0-9a-f]{{8}})(?![0-9a-f-])', re.I)
CARD_FOR = 86400        # a message never changes: its card is kept a day
REBUILD_EVERY = 3600    # seconds between full looks
FRESH_EVERY = 5         # seconds between looks at what's new
SNIPPET = 90


def mood_names():
    """{a Mood's name, now or before a rename: its slug now}. Kept a few seconds."""
    names = cache.get('links:moods')
    if names is None:
        from conversations.models import Mood, MoodAlias
        names = dict(Mood.objects.values_list('slug', 'slug'))
        for alias, slug in MoodAlias.objects.values_list('slug', 'mood__slug'):
            names.setdefault(alias, slug)
        cache.set('links:moods', names, 10)
    return names


def resolve(ref):
    """The full id of the message `ref` names (an id, or its first 8 characters), or None."""
    from django.db.models import TextField
    from django.db.models.functions import Cast
    from conversations.models import Message
    ref = ref.lower()
    if len(ref) == 36:
        return ref
    key = f'links:ref:{ref}'
    found = cache.get(key)
    if found is None:  # as memory.resolve_message does: the id as text, for Postgres
        ids = list(Message.objects.filter(mood__isnull=False).annotate(id_text=Cast('id', output_field=TextField()))
                   .filter(id_text__startswith=ref).values_list('id', flat=True)[:2])
        found = str(ids[0]) if len(ids) == 1 else ''  # none, or more than one: no link
        cache.set(key, found, CARD_FOR)
    return found or None


def card(ref):
    """{'id', 'mood', 'sender', 'is_human', 'snippet'} for a linked message, or None: in a Mood, and there."""
    full = resolve(ref)
    if not full:
        return None
    key = f'links:card:{full}'
    found = cache.get(key)
    if found is None:
        from conversations.models import Message
        from .mood_view import replied
        message = Message.objects.filter(id=full, mood__isnull=False).first()
        about = replied(full) if message else None
        found = {**about, 'mood': message.mood_slug, 'snippet': about['snippet'][:SNIPPET]} if about else {}
        cache.set(key, found, CARD_FOR)
    return found or None


# --- what links to what ------------------------------------------------------------

def _sources(since=None):
    """(message, the ids it links, whether it's a reply) for readable Mood messages carrying '#m-'."""
    from django.db.models import TextField
    from django.db.models.functions import Cast
    from conversations.models import Message, Mood
    from .mood_view import MACHINERY_SENDERS, is_wrapper, prose, quiet_reason, reply_to
    rows = (Message.objects.filter(mood_id__in=list(Mood.objects.values_list('id', flat=True)), is_sidechain=False)
            .exclude(sender_id__in=MACHINERY_SENDERS | {'mood-poller'}))
    if since is not None:
        rows = rows.filter(created_at__gt=since)
    rows = rows.annotate(as_text=Cast('content', TextField())).filter(as_text__contains='#m-')
    for message in rows.select_related('tooluse', 'thought'):
        if hasattr(message, 'tooluse') or hasattr(message, 'thought'):
            continue  # a tool's input, or thinking: not something said
        text = prose(message.content)
        if not text or is_wrapper(text) or quiet_reason(text) is not None:
            continue
        target, _ = reply_to(text)
        refs = [target] if target else []
        refs += [m.group(1) for m in TARGET.finditer(text)]
        yield message, refs, target


def _add(index, message, refs, reply_target):
    for ref in dict.fromkeys(refs):  # each once, in order
        full = resolve(ref)
        if not full or full == str(message.id):
            continue
        entry = {'id': str(message.id), 'sender': message.sender_id, 'mood': message.mood_slug,
                 'reply': ref == reply_target, 'at': message.created_at.isoformat()}
        here = index.setdefault(full, [])
        if not any(e['id'] == entry['id'] for e in here):
            here.append(entry)


def index():
    """{message id: [{'id', 'sender', 'mood', 'reply', 'at'}, ...]}: who linked or replied to each."""
    from django.utils import timezone
    now = time.time()
    found = cache.get('links:index')
    built = cache.get('links:built') or 0
    if found is None or now - built > REBUILD_EVERY:
        if cache.add('links:building', 1, 60) or found is None:
            found = {}
            started = timezone.now()
            for message, refs, reply in _sources():
                _add(found, message, refs, reply)
            cache.set('links:index', found, None)
            cache.set('links:built', now, None)
            cache.set('links:cursor', started, None)
            cache.set('links:fresh', now, None)
            cache.delete('links:building')
        return found
    if now - (cache.get('links:fresh') or 0) > FRESH_EVERY and cache.add('links:freshening', 1, 30):
        try:
            from datetime import timedelta
            cursor = cache.get('links:cursor')
            cursor = cursor - timedelta(seconds=60) if cursor else None  # a write still in flight: seen next time
            started = timezone.now()
            changed = False
            for message, refs, reply in _sources(since=cursor):
                _add(found, message, refs, reply)
                changed = True
            if changed:
                cache.set('links:index', found, None)
            cache.set('links:cursor', started, None)
            cache.set('links:fresh', now, None)
        finally:
            cache.delete('links:freshening')
    return found


_memo = {'at': 0.0, 'index': None}


def current():
    """index(), asked at most once a second per process: a page of turns asks it for each."""
    if _memo['index'] is None or time.time() - _memo['at'] > 1:
        _memo['index'], _memo['at'] = index(), time.time()
    return _memo['index']


def linked_from(message_id, links=None):
    """Who linked or replied to a message, oldest first; [] if nobody."""
    return sorted((links if links is not None else current()).get(str(message_id), []), key=lambda e: e['at'])
