"""Pictures and recordings shown in place: files on PickiPedia (and, through
it, Wikimedia Commons), and releases delivery-kid serves.

    [[File:Earl Scruggs.jpg]]  [[File:X.jpg|thumb|300px|a caption]]
    https://pickipedia.xyz/wiki/File:X.jpg
    https://commons.wikimedia.org/wiki/File:X.jpg
        -> the picture (a video or a sound if the file is one), linked to its page

    [[Release:<CID>]]  https://pickipedia.xyz/wiki/Release:<CID>
    https://ipfs.delivery-kid.cryptograss.live/ipfs/<CID>
        -> a player for the release: a video (its thumbnail the poster) or a
           sound, titled, linked to its Release page

PickiPedia answers both: its imageinfo knows its own uploads and, through
InstantCommons, Commons'; its release list knows every Release's CID and
type. What it says is kept a while (a file a day; the release list ten
minutes), so a page of turns doesn't ask it again and again; a miss is kept
briefly, and anything it can't say stays a link.
"""

import re

from django.core.cache import cache

GATEWAY = 'https://ipfs.delivery-kid.cryptograss.live'
FILE_FOR = 86400
MISS_FOR = 600
RELEASES_FOR = 600
THUMB_WIDTH = 800
# [[File:X|thumb|300px|left|caption]]: everything but the caption is how MediaWiki lays it out.
_LAYOUT = re.compile(r'^(thumb|thumbnail|frame|frameless|border|left|right|center|centre|none|upright(=[\d.]+)?'
                     r'|\d+(x\d+)?px|x\d+px|link=.*|alt=.*|page=\d+|class=.*|lang=.*)$', re.I)
# Kinds a browser plays in a <video> or an <audio> (HLS needs a player of its own: a link).
VIDEO_TYPES = {'video/mp4', 'video/webm', 'video/ogg', 'video/quicktime', 'video/mov'}
AUDIO_TYPES = {'audio/mpeg', 'audio/mp3', 'audio/ogg', 'audio/flac', 'audio/wav', 'audio/x-wav', 'audio/webm',
               'audio/mp4', 'audio/aac'}


def _ask(params, timeout=4):
    import requests
    from .mood_view import pickipedia_url
    answer = requests.get(f'{pickipedia_url()}/api.php', params={**params, 'format': 'json'}, timeout=timeout,
                          headers={'User-Agent': 'memory-lane (magenta; embeds)'})
    answer.raise_for_status()
    return answer.json()


def wiki_file(name):
    """{'src', 'full', 'page', 'mime'} for a file PickiPedia or Commons has, or None."""
    name = (name or '').strip().replace('_', ' ')
    if not name:
        return None
    key = 'embeds:file:' + re.sub(r'\s+', '_', name.lower())[:200]
    found = cache.get(key)
    if found is None:
        try:
            pages = _ask({'action': 'query', 'titles': f'File:{name}', 'prop': 'imageinfo',
                          'iiprop': 'url|mime', 'iiurlwidth': THUMB_WIDTH})['query']['pages']
            info = next(iter(pages.values())).get('imageinfo', [{}])[0]
        except Exception:
            info = {}
        found = ({'src': info.get('thumburl') or info['url'], 'full': info['url'], 'mime': info.get('mime', ''),
                  'page': info.get('descriptionurl') or info['url']} if info.get('url') else {})
        cache.set(key, found, FILE_FOR if found else MISS_FOR)
    return found or None


def file_caption(params):
    """The caption of [[File:X|...]]: its last part that isn't layout."""
    words = [p.strip() for p in params if p.strip() and not _LAYOUT.match(p.strip())]
    return words[-1] if words else ''


def normal_cid(cid):
    """A CID as the gateway wants it: a base32 CIDv1 (b...) is lowercase -- MediaWiki capitalises
    page titles, and some releases kept the capital."""
    cid = (cid or '').strip()
    return cid.lower() if cid[:1] in 'bB' else cid


def releases():
    """{CID: {'title', 'type', 'thumbnail', 'page'}}, every Release on PickiPedia."""
    found = cache.get('embeds:releases')
    if found is None:
        try:
            listed = _ask({'action': 'releaselist'}, timeout=6).get('releases', [])
        except Exception:
            listed = None
        if listed is None:
            cache.set('embeds:releases', {}, MISS_FOR // 10)
            return {}
        found = {}
        for r in listed:
            if not r.get('ipfs_cid'):
                continue
            found[normal_cid(r['ipfs_cid'])] = {
                'title': r.get('title') or r['ipfs_cid'], 'type': (r.get('file_type') or '').lower(),
                'page': r.get('page_title') or r['ipfs_cid'], 'thumbnail': r.get('thumbnail') or ''}
        cache.set('embeds:releases', found, RELEASES_FOR)
    return found


def _release_thumbnail(page):
    """A release's 'thumbnail:' (a PickiPedia file), from its page: the release list doesn't carry it."""
    key = 'embeds:thumb:' + page[:120]
    found = cache.get(key)
    if found is None:
        import requests
        from .mood_view import pickipedia_url
        try:
            raw = requests.get(f'{pickipedia_url()}/index.php', params={'title': f'Release:{page}', 'action': 'raw'},
                               timeout=4, headers={'User-Agent': 'memory-lane (magenta; embeds)'}).text
            line = re.search(r'^thumbnail:\s*[\'"]?([^\'"\n]+?)[\'"]?\s*$', raw, re.M)
            found = line.group(1).strip() if line else ''
        except Exception:
            found = ''
        cache.set(key, found, FILE_FOR if found else MISS_FOR)
    return found


def release(cid):
    """A playable release: {'cid', 'title', 'kind': 'video'|'audio', 'src', 'poster', 'page'}, or None."""
    found = releases().get(normal_cid(cid))
    if not found:
        return None
    kind = 'video' if found['type'] in VIDEO_TYPES else 'audio' if found['type'] in AUDIO_TYPES else None
    if not kind:
        return None  # HLS, or a type nobody said: a link
    from .mood_view import pickipedia_url
    thumbnail = found['thumbnail'] or (_release_thumbnail(found['page']) if kind == 'video' else '')
    poster = (wiki_file(thumbnail) or {}).get('src', '') if thumbnail and kind == 'video' else ''
    return {'cid': normal_cid(cid), 'title': found['title'], 'kind': kind, 'poster': poster,
            'src': f'{GATEWAY}/ipfs/{normal_cid(cid)}',
            'page': f"{pickipedia_url()}/wiki/Release:{found['page'].replace(' ', '_')}"}
