"""Open work: pull requests and new issues across our repositories, and who's in them.

What's open comes from a forge -- GitHub, for now: one search for open pull
requests and one for issues opened lately, across the cryptograss
organization, magenta and memory-lane. That's the only part that knows
about GitHub (FORGE below); swapping it -- for a wiki-backed record of
changes, or another forge -- leaves the rest alone.

Who's involved comes from our own record, not the forge: the Moods where a
change is mentioned (by its link), the people who mentioned it, and who
asked for it -- the last person to speak in that Mood before it was first
mentioned. Most changes are opened by an agent, so the person who asked is
the one worth showing.
"""

import re
import time
from datetime import datetime, timedelta, timezone

from django.core.cache import cache

from .repos import same

ORG = 'org:cryptograss'
SCOPES = f'{ORG} repo:jMyles/memory-lane repo:magent-cryptograss/magenta'  # until they're the org's (services/repos.py)
NEW_ISSUE_DAYS = 14
CACHE_FOR = 600  # seconds: the forge allows an unauthenticated search ten times a minute
MENTION_DAYS = 90  # how far back the record is read for mentions
# Forge accounts and the names they go by here. Hunter's inventory holds the
# same (github_username); memory-lane#65 makes that one record per person.
FORGE_PEOPLE = {'jmyles': 'justin', 'audioskywalker': 'skyler', 'rjpartingtoniii': 'rj',
                'fibonacci-frames': 'fibonacci', 'magent-cryptograss': 'magent'}
_LINK = re.compile(r'github\.com/([\w.-]+/[\w.-]+)/(pull|issues)/(\d+)', re.I)


def _search(query):
    import requests
    response = requests.get('https://api.github.com/search/issues', params={'q': query, 'per_page': 100,
                                                                            'sort': 'updated', 'order': 'desc'},
                            headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'memory-lane'}, timeout=10)
    response.raise_for_status()
    return response.json().get('items', [])


def _scoped(query):
    """The search across the org and the repositories still outside it: once
    those have moved, GitHub may refuse their old names (422), so the org alone."""
    import requests
    try:
        return _search(f'{query} {SCOPES}')
    except requests.HTTPError as e:
        if getattr(e.response, 'status_code', None) != 422:
            raise
        return _search(f'{query} {ORG}')


def _item(raw, kind):
    repo = '/'.join(raw['repository_url'].split('/')[-2:])
    login = (raw.get('user') or {}).get('login', '')
    return {'kind': kind, 'repo': repo, 'number': raw['number'], 'title': raw['title'], 'url': raw['html_url'],
            'author': FORGE_PEOPLE.get(login.lower(), login), 'opened_at': raw['created_at'],
            'updated_at': raw['updated_at'], 'draft': bool(raw.get('draft'))}


def forge_items():
    """[item] open on the forge: pull requests, and issues opened lately. Cached."""
    items = cache.get('work:forge')
    if items is None:
        since = (datetime.now(timezone.utc) - timedelta(days=NEW_ISSUE_DAYS)).date().isoformat()
        items = ([_item(r, 'pr') for r in _scoped('is:pr is:open')]
                 + [_item(r, 'issue') for r in _scoped(f'is:issue is:open created:>={since}')])
        cache.set('work:forge', items, CACHE_FOR)
    return items


def involvement(keys):
    """{(repo, number): {'moods', 'mentioned_by', 'asked_by'}} from what was said in the Moods."""
    from conversations.models import Message, Mood, ThinkingEntity
    from conversations.services.mood_view import MACHINERY_SENDERS
    wanted = {(same(r), n) for r, n in keys}
    humans = set(ThinkingEntity.objects.filter(is_biological_human=True).values_list('name', flat=True))
    titles = dict(Mood.objects.values_list('slug', 'title'))
    since = datetime.now(timezone.utc) - timedelta(days=MENTION_DAYS)
    rows = (Message.objects.filter(mood__isnull=False, is_sidechain=False, created_at__gt=since,
                                   content__icontains='github.com/')
            .exclude(sender_id__in=MACHINERY_SENDERS).order_by('created_at')
            .values('id', 'mood_id', 'mood__slug', 'sender_id', 'created_at', 'content'))
    found = {}
    for row in rows:
        seen = set()
        for repo, _, number in _LINK.findall(str(row['content'])):
            key = (same(repo), int(number))  # said before it moved: the same one
            if key not in wanted or key in seen:
                continue
            seen.add(key)
            entry = found.setdefault(key, {'moods': {}, 'mentioned_by': set(), 'first': row})
            entry['moods'].setdefault(row['mood__slug'], str(row['id']))
            if row['sender_id'] in humans:
                entry['mentioned_by'].add(row['sender_id'])
    out = {}
    for key, entry in found.items():
        first = entry['first']
        asker = (Message.objects.filter(mood_id=first['mood_id'], created_at__lt=first['created_at'],
                                        sender_id__in=humans, is_sidechain=False)
                 .order_by('-created_at').values_list('sender_id', flat=True).first())
        out[key] = {'moods': [{'slug': s, 'title': titles.get(s, s), 'id': i} for s, i in entry['moods'].items()],
                    'mentioned_by': sorted(entry['mentioned_by']), 'asked_by': asker}
    return out


def open_work():
    """Open pull requests and new issues, each with the people and Moods it involves."""
    items = forge_items()
    found = involvement([(i['repo'], i['number']) for i in items])
    for item in items:
        known = found.get((same(item['repo']), item['number'])) or {}
        item['moods'] = known.get('moods', [])
        item['asked_by'] = known.get('asked_by')
        people = set(known.get('mentioned_by', []))
        if known.get('asked_by'):
            people.add(known['asked_by'])
        if item['author'] in FORGE_PEOPLE.values() and item['author'] != 'magent':
            people.add(item['author'])
        item['people'] = sorted(people)
    return items
