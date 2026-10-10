"""Yarn clips, kept here: each clip's video fetched once, by the server, and
played from magenta from then on.

Pressing ▶ on a Yarn card used to send the browser to Yarn for the file, and
Yarn's Cloudflare now and then stops a person with a human check (Justin,
2026-10-10) -- which a <video> can't pass, so the card said "Couldn't play it
here". The server asks instead, once per clip: as soon as a clip is said (in
the background), or when ▶ is pressed before that's done. The video is
stored as Media (video/mp4, by its hash) and the clip's id is mapped to it
(models.YarnClip), so the same clip, said again or posted from /clips, plays
from here without asking Yarn at all.

If Yarn refuses the server too, nothing is kept and the page asks Yarn
itself as before; the server tries that clip again after an hour.

The card's picture, Yarn's captioned GIF of the clip, is stopped the same
way (a card that can't load it says only "Yarn clip"), so it's kept too, as
the clip's still: fetched with the video, or -- for a clip kept before
stills were -- when a card asks for it (api_yarn).
"""

import re
import threading

from django.core.cache import cache

SOURCES = ('https://y.getyarn.io/{}.mp4', 'https://y.yarn.co/{}.mp4')  # Yarn's player's address, then its older one
STILLS = ('https://y.getyarn.io/{}_text.gif', 'https://y.yarn.co/{}_text.gif')
MAX_BYTES = 25 * 1024 * 1024
TRY_AGAIN = 3600     # seconds before a clip Yarn refused is asked for again
AGENT = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36'
_URL = re.compile(r'https?://[^\s<>()"\']+')


def kept(clip):
    """The kept video's URL here, or None."""
    from conversations.models import YarnClip
    try:
        found = YarnClip.objects.select_related('media').filter(clip=clip).first()
    except Exception:  # not a UUID
        return None
    return found.media.url if found else None


def kept_still(clip):
    """(video URL, still URL) kept here for the clip, either None."""
    from conversations.models import YarnClip
    try:
        found = YarnClip.objects.select_related('media', 'still').filter(clip=clip).first()
    except Exception:
        return None, None
    if found is None:
        return None, None
    return found.media.url, (found.still.url if found.still else None)


def kept_of(clips):
    """{clip: url} for those of `clips` kept here: one question for a page of cards."""
    from conversations.models import YarnClip
    return {str(y.clip): y.media.url for y in YarnClip.objects.select_related('media').filter(clip__in=list(clips))}


def _is_mp4(data):
    return len(data) > 12 and data[4:8] == b'ftyp'


def _fetch(url, http, looks_right=_is_mp4, limit=MAX_BYTES):
    response = http.get(url, timeout=15, stream=True, headers={'User-Agent': AGENT, 'Accept': '*/*'})
    if response.status_code not in (200, 206):
        return None
    data = bytearray()
    for chunk in response.iter_content(256 * 1024):
        data.extend(chunk)
        if len(data) > limit:
            return None
    return bytes(data) if looks_right(data) else None  # a challenge page is HTML, not a video or a picture


def keep_still(clip, http=None):
    """Fetch and keep the clip's captioned GIF, for a clip whose video is kept; its URL here, or None."""
    import hashlib
    import requests
    from conversations.models import Media, YarnClip
    from . import media as media_store
    clip = str(clip).lower()
    row = YarnClip.objects.select_related('still').filter(clip=clip).first()
    if row is None:
        return None
    if row.still is not None:
        return row.still.url
    if cache.get(f'yarn-kept:still-refused:{clip}') or not cache.add(f'yarn-kept:still-fetching:{clip}', 1, 60):
        return None
    http = http or requests
    try:
        for source in STILLS:
            try:
                data = _fetch(source.format(clip), http, looks_right=lambda d: media_store.sniff(bytes(d)) is not None,
                              limit=media_store.MAX_BYTES)
            except Exception:
                data = None
            if data:
                media, _ = Media.objects.get_or_create(
                    sha256=hashlib.sha256(data).hexdigest(),
                    defaults={'mime': media_store.sniff(data), 'data': data, 'size': len(data), 'license': ''})
                YarnClip.objects.filter(clip=clip).update(still=media)
                return media.url
        cache.set(f'yarn-kept:still-refused:{clip}', 1, TRY_AGAIN)
        return None
    finally:
        cache.delete(f'yarn-kept:still-fetching:{clip}')


def keep(clip, http=None):
    """Fetch the clip's video and keep it, unless it's kept already; its URL here, or None."""
    import hashlib
    import requests
    from conversations.models import Media, YarnClip
    clip = str(clip).lower()
    if (url := kept(clip)) is not None:
        keep_still(clip, http)  # a clip kept before stills were: its picture now, once
        return url
    if cache.get(f'yarn-kept:refused:{clip}') or not cache.add(f'yarn-kept:fetching:{clip}', 1, 60):
        return None  # refused lately, or being fetched right now
    http = http or requests
    try:
        for source in SOURCES:
            try:
                data = _fetch(source.format(clip), http)
            except Exception:
                data = None
            if data:
                sha = hashlib.sha256(data).hexdigest()
                # Not ours to license: a Yarn clip carries no license of ours ('').
                media, _ = Media.objects.get_or_create(
                    sha256=sha, defaults={'mime': 'video/mp4', 'data': data, 'size': len(data), 'license': ''})
                YarnClip.objects.get_or_create(clip=clip, defaults={'media': media})
                keep_still(clip, http)
                return media.url
        cache.set(f'yarn-kept:refused:{clip}', 1, TRY_AGAIN)
        return None
    finally:
        cache.delete(f'yarn-kept:fetching:{clip}')


def clips_in(text):
    from .mood_view import yarn_clip
    found = []
    for url in _URL.findall(text or ''):
        clip = yarn_clip(url.rstrip('.,;:!?'))
        if clip and clip not in found:
            found.append(clip)
    return found


def keep_later(text):
    """Keep, in the background, every Yarn clip said in `text` that isn't kept yet."""
    clips = [c for c in clips_in(text) if None in kept_still(c)]  # its video, or its picture, not kept yet
    if not clips:
        return

    def run():
        from django.db import connection
        try:
            for clip in clips:
                keep(clip)
        except Exception:
            pass  # Yarn, or the network: ▶ asks again
        finally:
            connection.close()
    threading.Thread(target=run, daemon=True).start()
