"""Who is reading the Moods without signing in: a count for everyone,
and each one's address for those signed in with an SSH key (not a PickiPedia
sign-in: those include people we barely know).

Every open page asks for its Mood's news every few seconds, so a browser that
asked within the last WATCHING_FOR seconds and has no sign-in is watching.
One per address and browser, in a shared cache entry, as typing is. Only
browsers count: the runner and other scripts read the Moods too, and bots
are left out. The address is the one Caddy saw (the last of
X-Forwarded-For), never one a client claims for itself.
"""

import hashlib
import re
import time

from django.core.cache import cache

KEY = 'watchers'
WATCHING_FOR = 90      # seconds since its last look that a page counts (a hidden tab looks once a minute)
WRITE_EVERY = 20       # seconds between noting the same watcher again
_NOT_A_PERSON = re.compile(r'bot|crawl|spider|slurp|preview|python|curl|wget|headless', re.I)


def client_ip(request):
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
    return (forwarded.split(',')[-1].strip() if forwarded else '') or request.META.get('REMOTE_ADDR', '')


def browser(agent):
    """A few words for a user agent: 'Firefox on Linux', 'Safari on iPhone'."""
    name = next((n for k, n in (('Edg/', 'Edge'), ('Firefox/', 'Firefox'), ('Chrome/', 'Chrome'), ('Safari/', 'Safari'))
                 if k in agent), 'a browser')
    where = next((w for k, w in (('iPhone', 'iPhone'), ('iPad', 'iPad'), ('Android', 'Android'), ('Mac OS X', 'a Mac'),
                                 ('Windows', 'Windows'), ('Linux', 'Linux')) if k in agent), '')
    return f'{name} on {where}' if where else name


def seen(request, slug, now=None):
    """Note a page's look at a Mood, if it's a browser with nobody signed in."""
    agent = request.META.get('HTTP_USER_AGENT', '')
    if 'Mozilla/' not in agent or _NOT_A_PERSON.search(agent) or request.headers.get('Authorization'):
        return
    now = now or time.time()
    ip = client_ip(request)
    who = hashlib.sha256(f'{ip}|{agent}'.encode()).hexdigest()[:16]
    watching = cache.get(KEY) or {}
    was = watching.get(who)
    if was and was['slug'] == slug and now - was['last'] < WRITE_EVERY:
        return
    watching = {k: w for k, w in watching.items() if now - w['last'] < WATCHING_FOR}
    watching[who] = {'ip': ip, 'browser': browser(agent), 'slug': slug, 'last': now,
                     'since': was['since'] if was else now}
    cache.set(KEY, watching, WATCHING_FOR * 2)


def by_mood(now=None):
    """slug -> how many are reading it without signing in: counts only, for anyone."""
    now = now or time.time()
    counts = {}
    for w in (cache.get(KEY) or {}).values():
        if now - w['last'] < WATCHING_FOR:
            counts[w['slug']] = counts.get(w['slug'], 0) + 1
    return counts


def now_watching(slug, admin=False, now=None):
    """{'here', 'all'} counts, and with `admin` (an SSH-key sign-in) each one: {'ip', 'browser', 'mood', 'since'}."""
    now = now or time.time()
    watching = [w for w in (cache.get(KEY) or {}).values() if now - w['last'] < WATCHING_FOR]
    out = {'here': sum(1 for w in watching if w['slug'] == slug), 'all': len(watching)}
    if admin:
        out['who'] = sorted(({'ip': w['ip'], 'browser': w['browser'], 'mood': w['slug'], 'since': w['since']}
                             for w in watching), key=lambda w: (w['mood'] != slug, w['since']))
    return out
