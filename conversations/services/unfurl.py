"""A link's preview card: its page's title, description and picture.

Paste a link to an article, a video, a festival's page, and like other chat
services the Mood shows a card under the message: the page's own title,
a line of its description, its picture, its site. Pages say these about
themselves in OpenGraph tags (og:title, og:description, og:image,
og:site_name; Twitter's, or <title> and the description meta, where those
are missing).

Fetching a page someone named is the part to be careful with:

- Only links in a message said in a Mood, asked for by the message
  (views_moods.api_unfurl): never a URL a reader names, so magenta is no one's
  fetcher.
- Only the public internet: every hop, redirects included, is resolved
  first, and a private, loopback, link-local or reserved address is refused
  -- nothing on hunter's or maybelle's own networks is reachable this way.
- A few seconds, half a megabyte, HTML only; three cards a message at most.
- Kept a day (a miss, an hour): a page is fetched once, not per reader.

The picture is the page's own, shown from where it is (https only, no
referrer). Links that already show in place -- PickiPedia, Commons,
delivery-kid, Yarn, a message here -- get no card (mood_view._link_url).
"""

import html
import ipaddress
import re
import socket
from urllib.parse import urljoin, urlparse

from django.core.cache import cache

MAX_BYTES = 512 * 1024
TIMEOUT = 4
MAX_HOPS = 4
PER_MESSAGE = 3
FOUND_FOR = 86400
MISSED_FOR = 3600
_META = re.compile(r'<meta\s[^>]*>', re.I | re.S)
_ATTR = re.compile(r'([\w:-]+)\s*=\s*("([^"]*)"|\'([^\']*)\'|([^\s>]+))', re.S)
_TITLE = re.compile(r'<title[^>]*>(.*?)</title>', re.I | re.S)


class Refused(Exception):
    """Not a page this fetches: why."""


def public(host):
    """Whether every address `host` resolves to is on the public internet."""
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError):
        return False
    addresses = {info[4][0] for info in infos}
    for address in addresses:
        ip = ipaddress.ip_address(address.split('%')[0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast
                or ip.is_unspecified or getattr(ip, 'is_site_local', False)):
            return False
    return bool(addresses)


def _fetch(url, http):
    """(final url, html) for a public web page; Refused if it isn't one."""
    for _ in range(MAX_HOPS):
        parts = urlparse(url)
        if parts.scheme not in ('http', 'https') or not parts.hostname:
            raise Refused('not a web address')
        if parts.port not in (None, 80, 443):
            raise Refused('not the usual port')
        if not public(parts.hostname):
            raise Refused('not on the public internet')
        response = http.get(url, timeout=TIMEOUT, stream=True, allow_redirects=False,
                            headers={'User-Agent': 'memory-lane (magenta; link previews)', 'Accept': 'text/html'})
        try:
            if response.status_code in (301, 302, 303, 307, 308) and response.headers.get('Location'):
                url = urljoin(url, response.headers['Location'])
                continue
            if response.status_code != 200:
                raise Refused(f'answered {response.status_code}')
            if 'html' not in (response.headers.get('Content-Type') or '').lower():
                raise Refused('not a web page')
            body = b''
            for chunk in response.iter_content(16384):
                body += chunk
                if len(body) >= MAX_BYTES:
                    break
            return url, body[:MAX_BYTES].decode(response.encoding or 'utf-8', errors='replace')
        finally:
            response.close()
    raise Refused('too many redirects')


def _unescape(text):
    """HTML entities as characters -- twice at most, as some pages (GitHub's) encode them twice."""
    once = html.unescape(text)
    return html.unescape(once) if '&' in once else once


def read(page, url):
    """{'url', 'title', 'description', 'image', 'site'} from a page's HTML; None if it says nothing."""
    tags = {}
    for meta in _META.findall(page.split('</head>', 1)[0] if '</head>' in page else page[:200_000]):
        attrs = {m.group(1).lower(): m.group(3) if m.group(3) is not None else m.group(4) if m.group(4) is not None
                 else m.group(5) for m in _ATTR.finditer(meta)}
        key = (attrs.get('property') or attrs.get('name') or '').lower()
        if key and attrs.get('content') and key not in tags:
            tags[key] = _unescape(attrs['content']).strip()
    title_tag = _TITLE.search(page)
    title = tags.get('og:title') or tags.get('twitter:title') or (_unescape(title_tag.group(1)).strip() if title_tag else '')
    if not title:
        return None
    image = tags.get('og:image') or tags.get('og:image:url') or tags.get('twitter:image') or ''
    image = urljoin(url, image) if image else ''
    return {'url': url, 'title': ' '.join(title.split())[:200],
            'description': ' '.join((tags.get('og:description') or tags.get('twitter:description')
                                     or tags.get('description') or '').split())[:300],
            'image': image if image.startswith('https://') else '',
            'site': (tags.get('og:site_name') or urlparse(url).hostname or '')[:80]}


def card(url, http=None):
    """A link's card, kept a day; None if there isn't one to show."""
    key = 'unfurl:' + url[:400]
    found = cache.get(key)
    if found is None:
        import requests
        try:
            final, page = _fetch(url, http or requests)
            found = read(page, final) or {}
            if found:
                found['url'] = url  # the card goes where the link said
        except Exception:  # noqa: BLE001 -- refused, unreachable, unreadable: no card
            found = {}
        cache.set(key, found, FOUND_FOR if found else MISSED_FOR)
    return found or None


def links_in(text):
    """The links in what was said that show as plain links, so get a card: at most PER_MESSAGE."""
    from .mood_view import plain_links
    return plain_links(text)[:PER_MESSAGE]
