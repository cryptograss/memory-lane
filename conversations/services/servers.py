"""The servers the Moods run on: whether each answers, and when it's being redeployed.

The Moods show four dots -- hunter, maybelle, delivery-kid, pickipedia --
pulsing while one is redeployed. Whether a server answers is checked from
here, at most once a minute. Redeploys are told by the deploy scripts on
maybelle (maybelle-config's report-deploy.sh), which post to /api/deploys/
with a key from the vault; each start, finish or failure is announced in
the Moods it concerns -- hunter's and maybelle's in all of them, the others
in their own -- as a line in the thread, kept in the record.
"""

import re
import socket
import time
import uuid

from django.core.cache import cache

SERVERS = [
    {'name': 'hunter', 'check': ('tcp', 'hunter.cryptograss.live', 443), 'moods': '*',
     'about': 'the containers: people, Moods, the runners', 'page': 'Cryptograss:Hunter'},
    {'name': 'maybelle', 'check': ('self',), 'moods': '*',
     'about': 'the record (memory-lane), the memory server, the wiki builds', 'page': 'Cryptograss:Maybelle'},
    {'name': 'delivery-kid', 'check': ('http', 'https://delivery-kid.cryptograss.live/health'),
     'moods': ['delivery-kid'], 'about': 'pinning and delivery', 'page': 'Cryptograss:Delivery-kid'},
    {'name': 'pickipedia', 'check': ('http', 'https://pickipedia.xyz/api.php?action=query&meta=siteinfo&format=json'),
     'moods': ['pickipedia-and-rabbithole'], 'about': 'the wiki', 'page': ''},  # it is the wiki: its front page
]
CARD_FOR = 6 * 3600       # seconds a server's PickiPedia card (art, role) is kept
_INFOBOX_ART = re.compile(r'\|\s*image\s*=\s*<pre[^>]*>\n?(.*?)</pre>', re.S)
_INFOBOX_ROLE = re.compile(r'\|\s*role\s*=\s*([^\n|]+)')
NAMES = [s['name'] for s in SERVERS]
STATES = ('started', 'finished', 'failed')
SOURCE = 'deploy'
CHECK_EVERY = 60          # seconds between looks at a server
DEPLOY_PATIENCE = 90 * 60  # a redeploy unheard of for this long is presumed over


def probe(check, timeout=4):
    """(answers, milliseconds) for one server's check."""
    kind = check[0]
    start = time.monotonic()
    try:
        if kind == 'self':
            return True, 0
        if kind == 'tcp':
            with socket.create_connection((check[1], check[2]), timeout=timeout):
                pass
        elif kind == 'http':
            import requests
            response = requests.get(check[1], timeout=timeout)
            if response.status_code >= 500:
                return False, round((time.monotonic() - start) * 1000)
        return True, round((time.monotonic() - start) * 1000)
    except Exception:
        return False, round((time.monotonic() - start) * 1000)


def wiki_card(server):
    """{'url', 'role', 'art'} from a server's PickiPedia page: the ASCII art
    and role in its infobox, so the dots' popover shows what the wiki does.
    Kept six hours; a wiki that doesn't answer leaves just the link."""
    from conversations.services.motion_view import pickipedia_url
    base, title = pickipedia_url(), server.get('page') or ''
    url = f"{base}/wiki/{title.replace(' ', '_')}" if title else f'{base}/'
    if not title:
        return {'url': url, 'role': '', 'art': ''}
    key = f'server-card:{title}'
    card = cache.get(key)
    if card is None:
        card = {'url': url, 'role': '', 'art': ''}
        try:
            import requests
            page = requests.get(f'{base}/api.php', timeout=5, headers={'User-Agent': 'memory-lane (magenta)'},
                                params={'action': 'query', 'titles': title, 'prop': 'revisions', 'rvprop': 'content',
                                        'rvslots': 'main', 'format': 'json', 'formatversion': 2}).json()
            text = page['query']['pages'][0]['revisions'][0]['slots']['main']['content']
            art, role = _INFOBOX_ART.search(text), _INFOBOX_ROLE.search(text)
            card['art'] = art.group(1).strip('\n').rstrip() if art else ''
            card['role'] = role.group(1).strip() if role else ''
            cache.set(key, card, CARD_FOR)
        except Exception:
            cache.set(key, card, 600)  # try again in ten minutes
    return card


def last_deploy(name):
    """The newest deploy event told for a server, as stored, or None."""
    from conversations.models import Message
    row = (Message.objects.filter(source_file=SOURCE, content__server=name)
           .order_by('-created_at').values('content', 'created_at').first())
    if row is None:
        return None
    return {**row['content'], 'at': row['created_at'].isoformat(), 'at_ts': row['created_at'].timestamp()}


def status(now=None):
    """Every server: whether it answers, and its newest deploy event."""
    now = now or time.time()
    out = []
    for server in SERVERS:
        key = f"server-check:{server['name']}"
        checked = cache.get(key)
        if checked is None:
            up, ms = probe(server['check'])
            checked = {'up': up, 'ms': ms, 'checked_at': now}
            cache.set(key, checked, CHECK_EVERY)
        deploy = last_deploy(server['name'])
        deploying = bool(deploy and deploy.get('state') == 'started' and now - deploy['at_ts'] < DEPLOY_PATIENCE)
        out.append({'name': server['name'], 'about': server['about'], **wiki_card(server), **checked,
                    'deploying': deploying,
                    'deploy': {k: v for k, v in (deploy or {}).items() if k != 'at_ts'} or None})
    return out


def moods_for(name):
    from conversations.models import Motion
    server = next(s for s in SERVERS if s['name'] == name)
    if server['moods'] == '*':
        return list(Motion.objects.all())
    return list(Motion.objects.filter(slug__in=server['moods']))


def record_deploy(name, state, commit='', by='', note=''):
    """Announce a deploy event in the Moods it concerns; returns how many."""
    from conversations.models import ConversationParticipant, Message
    took = None
    if state in ('finished', 'failed'):
        started = last_deploy(name)
        if started and started.get('state') == 'started':
            took = round(time.time() - started['at_ts'])
    content = {'type': 'deploy', 'server': name, 'state': state, 'commit': commit[:40], 'by': by[:40],
               'note': note[:200], 'took': took}
    system, _ = ConversationParticipant.objects.get_or_create(name='system', defaults={'participant_type': 'system'})
    moods = moods_for(name)
    stamp = int(time.time() * 1000)
    Message.objects.bulk_create([
        Message(id=uuid.uuid4(), sender=system, motion=mood, content=content, timestamp=stamp, source_file=SOURCE)
        for mood in moods])
    cache.delete(f'server-check:{name}')  # look again: it may be back, or gone
    return len(moods)
