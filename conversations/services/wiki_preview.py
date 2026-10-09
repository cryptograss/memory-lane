"""A PickiPedia page in brief, for the card shown when a link to it is hovered.

PickiPedia has no TextExtracts or PageImages, and its pages aren't
formulaic, but those that matter most share a shape: a first template
whose name says Info -- {{Infobox resource}} for servers, {{BandInfo}},
{{MusicianInfo}} -- of `key = value` lines, then prose. So a preview is:

    art      an infobox image that is ASCII art (<pre>), as servers have
    image    else its picture (the infobox's, or the page's first File:)
    summary  the opening prose, a sentence or two
    facts    up to four short infobox lines (role, type, origin, genre...)

A Release: page is YAML: its title, type, uploader and venue. What a page
says is kept six hours (a page nobody can read, ten minutes), so a thread
full of links asks the wiki once per page.
"""

import hashlib
import html
import re

from django.core.cache import cache

KEEP_FOR = 6 * 3600
MISS_FOR = 600
SUMMARY_CHARS = 280
FACT_CHARS = 80
FACTS = 4
# Infobox lines that aren't facts: how it's laid out, or what the card shows otherwise.
NOT_FACTS = {'name', 'image', 'image_caption', 'caption', 'title', 'logo', 'alt', 'image_size', 'width'}
_PRE = re.compile(r'<pre\b[^>]*>(.*?)</pre>', re.S | re.I)
_TITLE_OK = re.compile(r'^[^#<>\[\]{}|\x00-\x1f]{1,255}$')


def _split_top(text, sep='|'):
    """`text` split at `sep` where it isn't inside [[...]] or {{...}}."""
    parts, buf, depth, i = [], [], 0, 0
    while i < len(text):
        two = text[i:i + 2]
        if two in ('[[', '{{'):
            depth += 1
            buf.append(two)
            i += 2
        elif two in (']]', '}}'):
            depth = max(0, depth - 1)
            buf.append(two)
            i += 2
        elif text[i] == sep and depth == 0:
            parts.append(''.join(buf))
            buf = []
            i += 1
        else:
            buf.append(text[i])
            i += 1
    parts.append(''.join(buf))
    return parts


def _templates(text):
    """The top-level {{...}} in `text`: [(start, end, inside)]."""
    found, depth, start, i = [], 0, 0, 0
    while i < len(text) - 1:
        two = text[i:i + 2]
        if two == '{{':
            if depth == 0:
                start = i
            depth += 1
            i += 2
        elif two == '}}' and depth:
            depth -= 1
            i += 2
            if depth == 0:
                found.append((start, i, text[start + 2:i - 2]))
        else:
            i += 1
    return found


def _without_templates(text):
    for start, end, _ in reversed(_templates(text)):
        text = text[:start] + text[end:]
    return text


def plain(text):
    """Wikitext as words: links as their words, templates, files and markup gone."""
    text = re.sub(r'<!--.*?-->', '', text, flags=re.S)
    text = re.sub(r'<ref\b[^>]*/>|<ref\b[^>]*>.*?</ref>', '', text, flags=re.S | re.I)
    text = _without_templates(text)
    text = re.sub(r'\[\[(?:File|Image|Category|Media):[^\[\]]*(?:\[\[[^\]]*\]\][^\[\]]*)*\]\]', '', text, flags=re.I)
    text = re.sub(r'\[\[([^\]|]*)\|([^\]]*)\]\]', r'\2', text)
    text = re.sub(r'\[\[([^\]]*)\]\]', lambda m: m.group(1).split(':')[-1] if m.group(1).count(':') else m.group(1), text)
    text = re.sub(r'\[https?://\S+\s+([^\]]*)\]', r'\1', text)
    text = re.sub(r'\[https?://\S+\]', '', text)
    text = re.sub(r"'{2,}", '', text)
    text = re.sub(r'<[^>]+>', '', text)
    return re.sub(r'\s+', ' ', html.unescape(text)).strip()


def _sentences(text, limit=SUMMARY_CHARS):
    """As much of `text` as fits, ended at a sentence where one ends in time."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind('. '), cut.rfind('! '), cut.rfind('? '))
    return cut[:end + 1] if end > limit // 3 else cut.rsplit(' ', 1)[0] + '…'


def infobox(text):
    """{key: value} of the page's first ...Info... template, its <pre> art kept as is; {} if none."""
    for _, _, inside in _templates(text):
        parts = _split_top(inside)
        if 'info' not in parts[0].strip().lower():
            continue
        found = {}
        for part in parts[1:]:
            key, eq, value = part.partition('=')
            if eq and key.strip():
                found[key.strip().lower()] = value.strip()
        return found
    return {}


def summary(text):
    """The opening prose: paragraphs after the templates, joined until there's a sentence's worth."""
    text = _PRE.sub('', re.sub(r'<!--.*?-->', '', text, flags=re.S))
    text = _without_templates(text)
    said = []
    for para in re.split(r'\n\s*\n', text):
        para = para.strip()
        if para.startswith('=='):
            if said:
                break  # the first section ends the opening
            continue
        if not para or para[0] in '{|!*#:;' or para.startswith(('[[File:', '[[Image:', '[[Category:', '__')):
            continue
        words = plain(para)
        if words:
            said.append(words)
        if sum(len(s) for s in said) >= 60:
            break
    return _sentences(' '.join(said))


def first_file(text):
    m = re.search(r'\[\[(?:File|Image):([^|\]]+)', text, re.I)
    return m.group(1).strip() if m else ''


def from_wikitext(text):
    """{'summary', 'facts', 'art', 'image_file'} for a page's wikitext."""
    text = text.replace('{{!}}', '|')
    # Art is set aside first: an ASCII portrait may well have a | or an = in it.
    arts = []

    def aside(m):
        arts.append(m.group(1))
        return f'\x00{len(arts) - 1}\x00'
    box = infobox(_PRE.sub(aside, text))
    art = ''
    image = box.get('image', '')
    held = re.search(r'\x00(\d+)\x00', image)
    if held:
        art, image = arts[int(held.group(1))].strip('\n').rstrip(), ''
    box = {k: re.sub(r'\x00\d+\x00', '', v) for k, v in box.items()}
    facts = []
    for key, value in box.items():
        if key in NOT_FACTS or re.match(r'^does\d*$', key):
            continue
        words = plain(_PRE.sub('', value))
        if words and len(words) <= FACT_CHARS:
            facts.append([key.replace('_', ' '), words])
        if len(facts) >= FACTS:
            break
    said = summary(text)
    if not said and box.get('does1'):  # {{MusicianInfo}}: its "does" lines finish a sentence begun with the name
        said = _sentences(f"{plain(box.get('name', ''))} {plain(box['does1'])}".strip())
    if not said:  # all in the infobox otherwise: its first long line
        said = _sentences(next((plain(v) for k, v in box.items() if k not in NOT_FACTS and len(plain(v)) > FACT_CHARS), ''))
    from urllib.parse import unquote
    image_file = plain(image) if image and not art else ('' if art else first_file(text))
    return {'summary': said, 'facts': facts, 'art': art, 'image_file': unquote(image_file)}


def from_release(text):
    """A Release: page's YAML, in brief."""
    import yaml
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        data = None
    if not isinstance(data, dict):
        return {'summary': '', 'facts': [], 'art': '', 'image_file': ''}
    facts = []
    for key, label in (('release_type', 'type'), ('venue', 'venue'), ('uploaded_by', 'uploaded by')):
        value = str(data.get(key) or '').strip().removeprefix('wiki:')
        if value:
            facts.append([label, value[:FACT_CHARS]])
    people = data.get('performers') or data.get('participants') or []
    if isinstance(people, list) and people:
        facts.append(['with', ', '.join(str(p) for p in people)[:FACT_CHARS]])
    return {'summary': _sentences(str(data.get('description') or '').strip()), 'facts': facts, 'art': '',
            'image_file': str(data.get('thumbnail') or ''), 'name': str(data.get('title') or '').strip()}


def preview(title, http=None):
    """{'title', 'url', 'summary', 'facts', 'art', 'image'} for a page, or None if there's no such page."""
    import requests
    from . import embeds
    from .mood_view import pickipedia_url
    title = re.sub(r'\s+', ' ', (title or '').replace('_', ' ')).strip()
    if not _TITLE_OK.match(title):
        return None
    key = 'wiki-preview:' + hashlib.sha1(title.encode()).hexdigest()
    found = cache.get(key)
    if found is not None:
        return found or None
    http = http or requests
    try:
        page = http.get(f'{pickipedia_url()}/api.php', timeout=6, headers={'User-Agent': 'memory-lane (magenta; previews)'},
                        params={'action': 'query', 'titles': title, 'prop': 'revisions', 'rvprop': 'content',
                                'rvslots': 'main', 'redirects': 1, 'format': 'json', 'formatversion': 2}
                        ).json()['query']['pages'][0]
    except Exception:
        cache.set(key, {}, MISS_FOR)
        return None
    if page.get('missing') or not page.get('revisions'):
        cache.set(key, {}, MISS_FOR)
        return None
    text = page['revisions'][0]['slots']['main']['content']
    name = page['title']
    brief = from_release(text) if name.startswith('Release:') else from_wikitext(text)
    image = (embeds.wiki_file(brief['image_file']) or {}).get('src', '') if brief['image_file'] else ''
    found = {'title': brief.get('name') or name, 'url': f"{pickipedia_url()}/wiki/{name.replace(' ', '_')}",
             'summary': brief['summary'], 'facts': brief['facts'], 'art': brief['art'], 'image': image}
    cache.set(key, found, KEEP_FOR)
    return found
