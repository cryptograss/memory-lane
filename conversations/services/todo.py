"""A Mood's to-do list: what its people have to do, kept on PickiPedia.

Each Mood may have a page, Cryptograss:Moods/<slug>/todo, whose list is
YAML between <todo> tags -- on the wiki so anyone can edit it and see who
changed what, and plain enough to write by hand. PickiPedia shows it as a
checklist with real links (PickiPediaContent's <todo> tag); a list inside
<pre>, as the first ones were, is read too.

    <todo>
    - task: Merge memory-lane#113
      who: justin
      kind: merge
      link: https://github.com/jMyles/memory-lane/pull/113
    - task: Redeploy maybelle
      kind: deploy
      done: true
    </todo>

`task` is the one thing an item needs; `who` (a name, or several),
`kind` (review, merge, deploy, edit, or anything), `link`, `note` and
`done` are as they're useful. The page shows it beside the Mood
(views_moods.api_mood_todo); agents keep it current -- every wake points
them to it (poller rules_block) -- adding what they leave someone to do,
and ticking off what's been done.

A renamed Mood's list is found under its old name too, until it's moved.

**Merges tick themselves.** An open item whose link is a GitHub pull
request shows as done once GitHub says it was merged ('merged': true) --
whatever the page says, so nobody has to tick a merge by hand. The page
itself is left as it was; an agent tidies it when it next touches the list.
GitHub is asked once per repository every few minutes (its recently closed
pull requests), and a pull request not among them on its own, so sixty
unsigned requests an hour go a long way; settings.GITHUB_TOKEN, if set,
lifts that ceiling.
"""

import re

from django.core.cache import cache

PAGE = 'Cryptograss:Moods/{slug}/todo'
FOR = 60          # seconds a list is kept before PickiPedia is asked again
MAX_ITEMS = 200
FIELDS = ('task', 'who', 'kind', 'link', 'note', 'done')
_PRE = re.compile(r'<(todo|pre)>\s*\n?(.*?)\n?\s*</\1>', re.S | re.I)


class Unreadable(Exception):
    """The page is there, but its list can't be read: why, to show."""


def page_title(slug):
    return PAGE.format(slug=slug)


def parse(text):
    """The items in a page's text: [{'task', 'who': [...], 'kind', 'link', 'note', 'done'}]. Unreadable if not a list."""
    import yaml
    match = _PRE.search(text or '')
    body = match.group(2) if match else (text or '')
    try:
        loaded = yaml.safe_load(body)
    except yaml.YAMLError as e:
        mark = getattr(e, 'problem_mark', None)
        raise Unreadable(f'its YAML has a problem{f" at line {mark.line + 1}" if mark else ""}: '
                         f'{getattr(e, "problem", None) or e}')
    if loaded is None:
        return []
    if not isinstance(loaded, list):
        raise Unreadable('its YAML should be a list: each item starting "- task: ..."')
    items = []
    for raw in loaded[:MAX_ITEMS]:
        if isinstance(raw, str):
            raw = {'task': raw}
        if not isinstance(raw, dict) or not str(raw.get('task') or '').strip():
            continue
        who = raw.get('who') or []
        who = [w.strip() for w in (who.split(',') if isinstance(who, str) else who) if str(w).strip()]
        items.append({'task': str(raw['task']).strip()[:500], 'who': [str(w)[:60] for w in who][:10],
                      'kind': str(raw.get('kind') or '').strip()[:40], 'link': str(raw.get('link') or '').strip()[:500],
                      'note': str(raw.get('note') or '').strip()[:1000], 'done': bool(raw.get('done'))})
    return items


def _raw(title, http):
    """The page's wikitext, or None if there's no such page."""
    from .mood_view import pickipedia_url
    response = http.get(f'{pickipedia_url()}/index.php', params={'title': title, 'action': 'raw'}, timeout=6,
                        headers={'User-Agent': 'memory-lane (magenta; todo)'})
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise Unreadable(f'PickiPedia answered {response.status_code}')
    return response.text


def for_mood(mood, http=None):
    """{'page', 'edit', 'items', 'error'?}: the Mood's list (under its name now, or one it had), kept a minute."""
    key = f'todo:{mood.slug}'
    found = cache.get(key)
    if found is not None:
        return found
    import requests
    from urllib.parse import quote
    from .mood_view import pickipedia_url
    http = http or requests
    names = [mood.slug] + list(mood.aliases.values_list('slug', flat=True))
    title, text, error = page_title(mood.slug), None, ''
    try:
        for slug in names:
            text = _raw(page_title(slug), http)
            if text is not None:
                title = page_title(slug)
                break
    except Exception as e:  # noqa: BLE001 -- PickiPedia unreachable: say so, and ask again soon
        error = str(e) if isinstance(e, Unreadable) else "PickiPedia couldn't be reached"
    items = []
    if text is not None and not error:
        try:
            items = parse(text)
        except Unreadable as e:
            error = str(e)
    items = with_merges(items, http)
    page = f"{pickipedia_url()}/wiki/{quote(title.replace(' ', '_'))}"
    found = {'page': page, 'edit': f'{page}?action=edit', 'exists': text is not None, 'items': items,
             **({'error': error} if error else {})}
    cache.set(key, found, 15 if error else FOR)
    return found


# --- merges, from GitHub ------------------------------------------------------------

_PULL = re.compile(r'^https://github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)(?:[/#?].*)?$', re.I)
CLOSED_FOR = 300       # seconds a repository's recently closed pull requests are kept
OPEN_FOR = 600         # seconds a pull request on its own, still open, is kept
MERGED_FOR = 86400     # seconds one merged is kept: merged is merged


def _github(path, http, **params):
    from django.conf import settings
    token = getattr(settings, 'GITHUB_TOKEN', '')
    headers = {'Accept': 'application/vnd.github+json', 'User-Agent': 'memory-lane (magenta; todo)',
               **({'Authorization': f'Bearer {token}'} if token else {})}
    response = http.get(f'https://api.github.com{path}', params=params, headers=headers, timeout=6)
    return response.json() if response.status_code == 200 else None


def _recently_merged(owner, repo, http):
    """{number} of a repository's recently closed pull requests that were merged; None if GitHub won't say."""
    key = f'todo:merged:{owner}/{repo}'.lower()
    found = cache.get(key)
    if found is None:
        listed = _github(f'/repos/{owner}/{repo}/pulls', http, state='closed', sort='updated', direction='desc',
                         per_page=100)
        found = {p['number'] for p in listed if p.get('merged_at')} if isinstance(listed, list) else 'unknown'
        cache.set(key, found, CLOSED_FOR)
    return None if found == 'unknown' else found


def _merged(owner, repo, number, http):
    """Whether GitHub says this pull request was merged (False if it won't say)."""
    recent = _recently_merged(owner, repo, http)
    if recent and number in recent:
        return True
    key = f'todo:pull:{owner}/{repo}#{number}'.lower()
    found = cache.get(key)
    if found is None:
        pull = _github(f'/repos/{owner}/{repo}/pulls/{number}', http)
        found = bool(pull and pull.get('merged_at'))
        cache.set(key, found, MERGED_FOR if found else OPEN_FOR)
    return found


def with_merges(items, http):
    """The items, an open one linking a GitHub pull request that's been merged marked done ('merged': True)."""
    out = []
    for item in items:
        match = _PULL.match(item.get('link') or '')
        if match and not item['done']:
            try:
                if _merged(match.group(1), match.group(2), int(match.group(3)), http):
                    item = {**item, 'done': True, 'merged': True}
            except Exception:  # noqa: BLE001 -- GitHub unreachable: the item stands as written
                pass
        out.append(item)
    return out


def forget(mood):
    cache.delete(f'todo:{mood.slug}')
    changed()


# --- told at once: a merge, by GitHub's webhook; an edit, by the wiki feed ------------
# Asking GitHub on a timer is a few minutes behind. GitHub can say so itself
# the moment a pull request merges (a repository webhook, "Pull requests", to
# /api/github/hook/, signed with settings.GITHUB_WEBHOOK_SECRET). Then every
# list forgets what it kept, and the stamp every open page polls for changes:
# they ask for their list again within a few seconds.

STAMP = 'todo:stamp'


def changed():
    """Tell open pages a list may have changed: they ask again at their next poll."""
    import time
    cache.set(STAMP, int(time.time() * 1000), None)


def stamp():
    return cache.get(STAMP) or 0


def merged_now(owner, repo, number):
    """GitHub says this pull request just merged: kept as merged, every list asked again."""
    from conversations.models import Mood
    cache.set(f'todo:pull:{owner}/{repo}#{number}'.lower(), True, MERGED_FOR)
    recent = cache.get(f'todo:merged:{owner}/{repo}'.lower())
    if isinstance(recent, set):
        cache.set(f'todo:merged:{owner}/{repo}'.lower(), recent | {number}, CLOSED_FOR)
    cache.delete_many([f'todo:{slug}' for slug in Mood.objects.values_list('slug', flat=True)])
    changed()
