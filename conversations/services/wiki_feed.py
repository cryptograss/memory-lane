"""PickiPedia's recent changes, as lines in a Mood's thread (#general by default).

No scheduler: the runner asks for the pulse every second, so the pulse (and
a feed Mood's own page) nudge this at most once a minute, and the wiki is
asked in the background. Each new change becomes a system row, shown as an
event line -- "✎ SkymanJenkins edited [[Page]] (+120) · 'why'" -- never a
turn, so it wakes no agent and makes no Mood look busy. Bots' edits are
left out. The first look takes only the latest few, not the wiki's history.

A Mood's own to-do list (Cryptograss:Moods/<slug>/todo, services/todo.py)
is that Mood's business: an edit to it is shown there, under the name the
Mood has now or one it had, and not in the feed Moods. On PickiPedia's own
Special:RecentChanges it shows as any edit does.

A new release is the exception to leaving bots out (refresh_releases): its
Release: page is made by a bot (Blue Railroad Imports) once delivery-kid has
pinned it, and each one is news -- "until such time as it gets too loud"
(Justin, 2026-10-09). It gets a line of its own, with its player.
"""

import re
import threading
import time
import uuid

from django.conf import settings
from django.core.cache import cache

SOURCE = 'wiki'
RELEASE_SOURCE = 'release'
RELEASE_NS = 3004         # PickiPedia's Release: namespace
RELEASE_FRESH = 3 * 3600  # seconds: on the first look, a release this new is still news
EVERY = 60          # seconds between looks at the wiki
FIRST_LOOK = 3      # changes taken the first time, before there's anything to go on from
LIMIT = 25          # changes asked for per look
_TODO = re.compile(r'^Cryptograss:Moods/([^/]+)/todo$', re.I)


def feed_moods():
    return list(getattr(settings, 'WIKI_FEED_MOODS', ['general']))


def home_of(title):
    """For a Mood's to-do page, the slug of that Mood ('' if no Mood has that name, now or before); else None."""
    found = _TODO.match((title or '').replace('_', ' '))
    if not found:
        return None
    from .links import mood_names
    return mood_names().get(found.group(1).lower(), '')


def where(change, feeds):
    """The Moods (slugs) a change is shown in: its own Mood for a to-do list, else the feed Moods."""
    home = home_of(change.get('title'))
    return list(feeds) if home is None else ([home] if home else [])


def nudge(http=None, wait=False):
    """Look at the wiki if it's been a minute: in the background, unless `wait`."""
    if not feed_moods() or not cache.add('wiki-feed:lock', 1, EVERY):
        return False
    if wait:
        refresh(http)
        refresh_releases(http)
    else:
        threading.Thread(target=_refresh_and_close, args=(http,), daemon=True).start()
    return True


def _refresh_and_close(http):
    from django.db import connection
    try:
        for look in (refresh, refresh_releases):
            try:
                look(http)
            except Exception:
                pass  # the wiki or the network: next minute
    finally:
        connection.close()


def changes(http=None):
    """The wiki's newest changes by people, oldest first."""
    import requests
    from conversations.services.mood_view import pickipedia_url
    http = http or requests
    answer = http.get(f'{pickipedia_url()}/api.php', timeout=8, headers={'User-Agent': 'memory-lane (magenta)'},
                      params={'action': 'query', 'list': 'recentchanges', 'rcshow': '!bot', 'rctype': 'edit|new',
                              'rcprop': 'title|ids|sizes|user|comment|timestamp', 'rclimit': LIMIT, 'format': 'json'})
    return list(reversed(answer.json()['query']['recentchanges']))


def refresh(http=None):
    """Add what's new on the wiki to each feed Mood; how many lines were added."""
    from conversations.models import ConversationParticipant, Message, Mood
    feeds = feed_moods()
    if not Mood.objects.filter(slug__in=feeds).exists():
        return 0
    found = changes(http)
    routed = {}
    for change in found:
        for slug in where(change, feeds):
            routed.setdefault(slug, []).append(change)
    system, _ = ConversationParticipant.objects.get_or_create(name='system', defaults={'participant_type': 'system'})
    added = 0
    for mood in Mood.objects.filter(slug__in=list(routed)):
        mine = routed[mood.slug]
        shown = set(Message.objects.filter(mood=mood, source_file=SOURCE).order_by('-created_at')
                    .values_list('content__rcid', flat=True)[:500])
        shown.discard(None)
        # Only what's newer than the newest line here (rcids only grow): an
        # older change it never showed stays history, not news.
        fresh = [c for c in mine if c.get('rcid', 0) > max(shown)] if shown else mine[-FIRST_LOOK:]
        for change in fresh:
            if not cache.add(f"wiki-feed:{mood.slug}:{change['rcid']}", 1, 7 * 86400):
                continue  # another worker took it just now
            Message.objects.create(
                id=uuid.uuid4(), sender=system, mood=mood, source_file=SOURCE, timestamp=int(time.time() * 1000),
                content={'type': 'wiki', 'rcid': change['rcid'], 'kind': change.get('type', 'edit'),
                         'title': change.get('title', ''), 'user': change.get('user', ''),
                         'comment': (change.get('comment') or '')[:300], 'revid': change.get('revid'),
                         'delta': (change.get('newlen') or 0) - (change.get('oldlen') or 0),
                         'at': change.get('timestamp', '')})
            added += 1
            if home_of(change.get('title')):  # its to-do list changed: open pages ask for it again now
                from .todo import forget
                forget(mood)
    return added


def new_releases(http=None):
    """Release: pages made lately (by anyone, the bot included), oldest first."""
    import requests
    from conversations.services.mood_view import pickipedia_url
    http = http or requests
    answer = http.get(f'{pickipedia_url()}/api.php', timeout=8, headers={'User-Agent': 'memory-lane (magenta)'},
                      params={'action': 'query', 'list': 'recentchanges', 'rcnamespace': RELEASE_NS, 'rctype': 'new',
                              'rcprop': 'title|ids|user|timestamp', 'rclimit': LIMIT, 'format': 'json'})
    found = answer.json()['query']['recentchanges']
    return [c for c in reversed(found) if (c.get('title') or '').startswith('Release:')]


def release_card(title, http=None):
    """{'title', 'by', 'kind'} from a Release page's YAML (its uploader's wiki name, without 'wiki:');
    what can't be read is left out, and the line still says a release was made."""
    import requests
    import yaml
    from conversations.services.mood_view import pickipedia_url
    http = http or requests
    try:
        raw = http.get(f'{pickipedia_url()}/index.php', params={'title': title, 'action': 'raw'}, timeout=8,
                       headers={'User-Agent': 'memory-lane (magenta)'}).text
        data = yaml.safe_load(raw)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    by = str(data.get('uploaded_by') or '')
    return {'title': str(data.get('title') or '').strip()[:200], 'by': by.removeprefix('wiki:')[:80],
            'kind': str(data.get('release_type') or '')[:40]}


def refresh_releases(http=None):
    """Put each new release in the feed Moods, once; how many lines were added."""
    from datetime import datetime, timezone
    from conversations.models import ConversationParticipant, Message, Mood
    moods = list(Mood.objects.filter(slug__in=feed_moods()))
    if not moods:
        return 0
    found = new_releases(http)
    if not found:
        return 0
    system, _ = ConversationParticipant.objects.get_or_create(name='system', defaults={'participant_type': 'system'})
    now = datetime.now(timezone.utc)

    def recent(change):
        try:
            when = datetime.fromisoformat(change['timestamp'].replace('Z', '+00:00'))
        except (KeyError, ValueError):
            return False
        return (now - when).total_seconds() < RELEASE_FRESH

    cards, added = {}, 0
    for mood in moods:
        shown = set(Message.objects.filter(mood=mood, source_file=RELEASE_SOURCE).order_by('-created_at')
                    .values_list('content__rcid', flat=True)[:500])
        shown.discard(None)
        # Only what's newer than the newest line here; the first time, only what's recent.
        fresh = [c for c in found if c.get('rcid', 0) > max(shown)] if shown else [c for c in found if recent(c)]
        for change in fresh:
            if not cache.add(f"wiki-feed:release:{mood.slug}:{change['rcid']}", 1, 7 * 86400):
                continue  # another worker took it just now
            title = change['title']
            if title not in cards:
                cards[title] = release_card(title, http)
            card = cards[title]
            cid = title.partition(':')[2]
            Message.objects.create(
                id=uuid.uuid4(), sender=system, mood=mood, source_file=RELEASE_SOURCE,
                timestamp=int(time.time() * 1000),
                content={'type': 'release', 'rcid': change['rcid'], 'cid': cid, 'page': title,
                         'title': card.get('title') or cid, 'by': card.get('by', ''), 'kind': card.get('kind', ''),
                         'at': change.get('timestamp', '')})
            added += 1
    if added:
        cache.delete('embeds:releases')  # its player, at once: the release list is kept ten minutes
    return added
