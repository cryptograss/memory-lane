"""A merged pull request's item, ticked on the wiki itself: done: true, by a bot.

magenta shows a to-do item linking a pull request as done the moment GitHub
says it merged (services/todo.py, with_merges). The list on PickiPedia said
nothing of it until someone edited it -- so whoever read the page there saw
the item open, and someone had to tidy after. Now, seeing it merged, magenta
has a bot put "  done: true" under that item on the page, and nothing else:
not a character of the rest moves (the YAML isn't read and written back, only
that one line added, or "done: false" turned true). Once per item a day,
whichever way the merge was learned (the webhook, or asking GitHub).

The bot: settings.PICKIPEDIA_TODO_BOT_USER / _PASSWORD, a bot password
(Special:BotPasswords) with "Edit existing pages" and "High-volume (bot)
access", so its edits are marked a bot's -- the wiki feed and Recent changes
leave them out by default. Unset, lists are ticked in magenta only, as before.
"""

import re
import threading

from django.conf import settings
from django.core.cache import cache

_BLOCK = re.compile(r'(<(todo|pre)>)(.*?)(</\2>)', re.S | re.I)
_ITEM = re.compile(r'^- ', re.M)
_DONE = re.compile(r'^(\s+)done:\s*(\S.*)?$')


def enabled():
    return bool(getattr(settings, 'PICKIPEDIA_TODO_BOT_USER', '') and getattr(settings, 'PICKIPEDIA_TODO_BOT_PASSWORD', ''))


def ticked(text, link):
    """The page's text with the item linking `link` marked done; the text unchanged if there's none, or it is."""
    def tick_block(block):
        body = block.group(3)
        starts = [m.start() for m in _ITEM.finditer(body)] + [len(body)]
        for a, b in zip(starts, starts[1:]):
            item = body[a:b]
            lines = item.split('\n')
            if not any(re.fullmatch(r'\s+link:\s*["\']?' + re.escape(link) + r'["\']?\s*', line) for line in lines):
                continue
            for i, line in enumerate(lines):
                done = _DONE.match(line)
                if done:
                    if (done.group(2) or '').strip().lower() in ('true', 'yes'):
                        return block.group(0)  # done already
                    lines[i] = f'{done.group(1)}done: true'
                    break
            else:
                # After its last line with words (an item may end in a blank line before the next).
                last = max(i for i, line in enumerate(lines) if line.strip())
                indent = re.match(r'\s+', lines[1]).group(0) if len(lines) > 1 and re.match(r'\s+\S', lines[1]) else '  '
                lines.insert(last + 1, f'{indent}done: true')
            return block.group(1) + body[:a] + '\n'.join(lines) + body[b:] + block.group(4)
        return block.group(0)
    return _BLOCK.sub(tick_block, text, count=1)


# Ticks for one page, gathered and made in one edit. Several merges are often
# seen in the same look at a list, and MediaWiki tells edits apart by the
# second: three bots' edits in one second each passed as current, and the last
# put back an item the one before had ticked (2026-10-09). So: one edit for
# all a page's waiting ticks, and the page read again after, edited again if
# any didn't stay.
GATHER = 2          # seconds a page's first tick waits for others from the same look
_waiting = {}       # title -> {link}
_lock = threading.Lock()


def tick_later(title, link):
    """Tick `link`'s item on `title`, with any others for it, in the background; at most once a day each."""
    if not enabled() or not cache.add(f'todo:tick:{title}:{link}'.lower(), 1, 86400):
        return
    with _lock:
        first = title not in _waiting
        _waiting.setdefault(title, set()).add(link)
    if first:
        from .todo import _in_background
        _in_background(_tick_waiting, title)


def _tick_waiting(title):
    import time
    time.sleep(GATHER)
    with _lock:
        links = sorted(_waiting.pop(title, set()))
    if links:
        tick(title, links)


def tick(title, links, http=None, settle=1.5):
    """Mark the items linking `links` done on the wiki, in one edit; True if an edit was made and stayed."""
    import time
    import requests
    from .mood_view import pickipedia_url
    links = [links] if isinstance(links, str) else list(links)
    http = http or requests.Session()
    http.headers['User-Agent'] = 'memory-lane (magenta; to-do ticks)'
    api = f'{pickipedia_url()}/api.php'
    token = http.get(api, params={'action': 'query', 'meta': 'tokens', 'type': 'login', 'format': 'json'},
                     timeout=10).json()['query']['tokens']['logintoken']
    login = http.post(api, data={'action': 'login', 'lgname': settings.PICKIPEDIA_TODO_BOT_USER,
                                 'lgpassword': settings.PICKIPEDIA_TODO_BOT_PASSWORD, 'lgtoken': token,
                                 'format': 'json'}, timeout=10).json()
    if (login.get('login') or {}).get('result') != 'Success':
        return False

    def read():
        page = http.get(api, params={'action': 'query', 'prop': 'revisions', 'titles': title, 'rvslots': 'main',
                                     'rvprop': 'content|ids|timestamp', 'formatversion': 2, 'format': 'json'},
                        timeout=10).json()['query']['pages'][0]
        return None if page.get('missing') else page['revisions'][0]

    edited = False
    for _ in range(3):  # again, if someone edited it meanwhile, or a tick didn't stay
        revision = read()
        if revision is None:
            return False
        text = revision['slots']['main']['content']
        new = text
        for link in links:
            new = ticked(new, link)
        if new == text:
            return edited  # all done, there
        csrf = http.get(api, params={'action': 'query', 'meta': 'tokens', 'format': 'json'},
                        timeout=10).json()['query']['tokens']['csrftoken']
        done = http.post(api, data={'action': 'edit', 'title': title, 'text': new, 'token': csrf, 'bot': 1,
                                    'minor': 1, 'nocreate': 1, 'baserevid': revision['revid'],
                                    'basetimestamp': revision['timestamp'], 'format': 'json',
                                    'summary': 'Merged, says GitHub: ' + ', '.join(links)}, timeout=15).json()
        if (done.get('edit') or {}).get('result') == 'Success':
            edited = True
            time.sleep(settle)  # then read it again: did every tick stay?
            continue
        if (done.get('error') or {}).get('code') != 'editconflict':
            return False
    return edited
