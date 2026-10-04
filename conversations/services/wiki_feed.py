"""PickiPedia's recent changes, as lines in a Mood's thread (#general by default).

No scheduler: the runner asks for the pulse every second, so the pulse (and
a feed Mood's own page) nudge this at most once a minute, and the wiki is
asked in the background. Each new change becomes a system row, shown as an
event line -- "✎ SkymanJenkins edited [[Page]] (+120) · 'why'" -- never a
turn, so it wakes no agent and makes no Mood look busy. Bots' edits are
left out. The first look takes only the latest few, not the wiki's history.
"""

import threading
import time
import uuid

from django.conf import settings
from django.core.cache import cache

SOURCE = 'wiki'
EVERY = 60          # seconds between looks at the wiki
FIRST_LOOK = 3      # changes taken the first time, before there's anything to go on from
LIMIT = 25          # changes asked for per look


def feed_moods():
    return list(getattr(settings, 'WIKI_FEED_MOODS', ['general']))


def nudge(http=None, wait=False):
    """Look at the wiki if it's been a minute: in the background, unless `wait`."""
    if not feed_moods() or not cache.add('wiki-feed:lock', 1, EVERY):
        return False
    if wait:
        refresh(http)
    else:
        threading.Thread(target=_refresh_and_close, args=(http,), daemon=True).start()
    return True


def _refresh_and_close(http):
    from django.db import connection
    try:
        refresh(http)
    except Exception:
        pass  # the wiki or the network: next minute
    finally:
        connection.close()


def changes(http=None):
    """The wiki's newest changes by people, oldest first."""
    import requests
    from conversations.services.motion_view import pickipedia_url
    http = http or requests
    answer = http.get(f'{pickipedia_url()}/api.php', timeout=8, headers={'User-Agent': 'memory-lane (magenta)'},
                      params={'action': 'query', 'list': 'recentchanges', 'rcshow': '!bot', 'rctype': 'edit|new',
                              'rcprop': 'title|ids|sizes|user|comment|timestamp', 'rclimit': LIMIT, 'format': 'json'})
    return list(reversed(answer.json()['query']['recentchanges']))


def refresh(http=None):
    """Add what's new on the wiki to each feed Mood; how many lines were added."""
    from conversations.models import ConversationParticipant, Message, Motion
    moods = list(Motion.objects.filter(slug__in=feed_moods()))
    if not moods:
        return 0
    found = changes(http)
    system, _ = ConversationParticipant.objects.get_or_create(name='system', defaults={'participant_type': 'system'})
    added = 0
    for mood in moods:
        shown = set(Message.objects.filter(motion=mood, source_file=SOURCE).order_by('-created_at')
                    .values_list('content__rcid', flat=True)[:500])
        shown.discard(None)
        # Only what's newer than the newest line here (rcids only grow): an
        # older change it never showed stays history, not news.
        fresh = [c for c in found if c.get('rcid', 0) > max(shown)] if shown else found[-FIRST_LOOK:]
        for change in fresh:
            if not cache.add(f"wiki-feed:{mood.slug}:{change['rcid']}", 1, 7 * 86400):
                continue  # another worker took it just now
            Message.objects.create(
                id=uuid.uuid4(), sender=system, motion=mood, source_file=SOURCE, timestamp=int(time.time() * 1000),
                content={'type': 'wiki', 'rcid': change['rcid'], 'kind': change.get('type', 'edit'),
                         'title': change.get('title', ''), 'user': change.get('user', ''),
                         'comment': (change.get('comment') or '')[:300], 'revid': change.get('revid'),
                         'delta': (change.get('newlen') or 0) - (change.get('oldlen') or 0),
                         'at': change.get('timestamp', '')})
            added += 1
    return added
