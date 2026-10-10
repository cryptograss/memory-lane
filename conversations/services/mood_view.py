"""Turn a Mood into something a person can read.

A Mood is mostly machinery. Of the first 42 messages in the first Mood,
30 were tool calls, their results, system notices and thinking blocks. A
conversation view keeps the prose from thinking entities and drops the rest.
What it drops is a product decision, made here in one place.

The record holds whatever the agent wrote -- usually markdown, because the
agent composes for a terminal. Each view translates for itself; this one
produces HTML, escaped first so nothing in a message can inject markup, with
[[wikilinks]] resolving to PickiPedia.
"""

import html
import re
from urllib.parse import unquote, urlparse

from django.conf import settings

MACHINERY_SENDERS = {'tool-result', 'system'}

# Command scaffolding, system reminders and interruption markers are text,
# but they are not conversation.
# The summary Claude Code starts a session with after compacting it. Not
# anyone's words -- the harness writes it, as a prompt -- so never a turn or
# a mention; the Mood shows it folded, as the moment a context was compacted.
COMPACTION_PREFIX = 'This session is being continued from a previous conversation'
INTERRUPT_SOURCE = 'interrupt'
# System rows shown as a line in the thread.
NEW_MOOD_SOURCE = 'mood-new'
EVENT_SOURCES = ('deploy', INTERRUPT_SOURCE, NEW_MOOD_SOURCE, 'wiki', 'release', 'access', 'wiki-upload', 'handoff', 'merged', 'release-draft')  # wiki: services/wiki_feed.py; access: services/access.py; wiki-upload: services/wiki_upload.py; handoff: services/handoff.py; merged: services/todo.py; release-draft: services/delivery_kid.py
# Words posted into a Mood directly, not typed into a session: from the
# composer, or attested with a key (magenta.sh attest).
POSTED = ('mood-web', 'mood-attest')
_WRAPPER_PREFIXES = ('<', '[Request interrupted', COMPACTION_PREFIX)


def pickipedia_url():
    return getattr(settings, 'PICKIPEDIA_URL', 'https://pickipedia.xyz').rstrip('/')


def prose(content):
    """Human-readable text from a message's content, or '' if it is machinery."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, dict):
        if content.get('type') == 'attestation':
            return str(content.get('text', '')).strip()  # a statement signed with someone's key
        return ''  # a tool call
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get('type') == 'text' or ('text' in block and 'type' not in block):
                parts.append(block.get('text', ''))
        return '\n'.join(p for p in parts if p).strip()
    return ''


# An agent's choice not to speak: <silent/>, or <silent>why</silent>.
# by="screen": the runner's quick screen let it pass, not the agent itself.
_QUIET = re.compile(r'^<silent\s*/>$|^<silent(?:\s+by="(\w+)")?>(.*)</silent>$', re.S)


def quiet_reason(text):
    """{'reason', 'by'} for a silent reply ('by' is '' when the agent itself
    chose), or None if the text isn't one."""
    match = _QUIET.match(text)
    if not match:
        return None
    return {'reason': (match.group(2) or '').strip(), 'by': match.group(1) or ''}


def thought_text(content):
    """What a thinking block says, if the harness kept any of it ('' if not).

    Claude Code often stores thinking with its text emptied, but sometimes
    keeps a short note -- the dimmed lines a terminal shows between tool
    calls. Those are part of how the agent got where it did.
    """
    if not isinstance(content, list):
        return ''
    parts = [b.get('thinking', '') for b in content if isinstance(b, dict) and b.get('type') == 'thinking']
    return '\n'.join(p for p in parts if p).strip()


def is_wrapper(text):
    return text.startswith(_WRAPPER_PREFIXES)


def is_compaction(text):
    return text.startswith(COMPACTION_PREFIX)


def turns(mood, after=None):
    """Yield (message, text) for the readable conversation in a Mood.

    `after` is a Message; only turns created after it are yielded, which is
    what a polling client needs.
    """
    from conversations.models import ThinkingEntity

    speakers = set(ThinkingEntity.objects.values_list('name', flat=True))
    messages = mood.messages.filter(is_sidechain=False).select_related('sender').order_by('created_at')
    if after is not None:
        messages = messages.filter(created_at__gt=after.created_at)

    for msg in messages:
        if msg.sender_id in MACHINERY_SENDERS or msg.sender_id not in speakers:
            continue
        text = prose(msg.content)
        if not text or is_wrapper(text):
            continue
        yield msg, text


# --- markdown-ish to HTML ---------------------------------------------------

_FENCE = re.compile(r'```[^\n]*\n(.*?)```', re.S)
_INLINE_CODE = re.compile(r'`([^`\n]+)`')
_BOLD = re.compile(r'\*\*([^*\n]+)\*\*')
_ITALIC = re.compile(r'(?<![*\w])\*([^*\n]+)\*(?!\*)')
_WIKILINK = re.compile(r'\[\[([^\]|]+)(?:\|([^\]]+))?\]\]')
# @name, but not inside an email, a URL path, or another handle.
_MENTION = re.compile(r'(?<![\w@/.])@([A-Za-z][\w.-]*)')
# ![alt](url): our own stored media, or https from hosts we trust to serve
# images (settings.MOOD_IMAGE_HOSTS); any other host renders as a link,
# so a viewer's browser never fetches from somewhere nobody chose.
_IMAGE = re.compile(r'!\[([^\]\n]*)\]\((/(?:moods|motions)/media/[0-9a-f]{64}\.(?:png|jpg|gif|webp)|https://[^)\s]+)\)')
_MD_LINK = re.compile(r'\[([^\]]+)\]\((https?://[^)\s]+)\)')
# [label](a stored recording): a voice memo, played in place (services/voice.py).
_AUDIO = re.compile(r'\[([^\]\n]*)\]\((/(?:moods|motions)/media/[0-9a-f]{64}\.(?:webm|ogg|m4a|mp3|wav))\)')
_URL = re.compile(r'(https?://(?:(?!&quot;|&#x27;|&lt;|&gt;)[^\s<>"\x01\x02])+)')
# Placeholders the renderer uses for markup it has already made; never input.
_PLACEHOLDER_CHARS = re.compile(r'[\x00\x01\x02]')
_TRAILING_PUNCT = '.,;:!?)]\'"'
_HEADING = re.compile(r'^#{1,6}\s+(.+)$')
_BULLET = re.compile(r'^\s*[-*–]\s+(.*)$')
_NUMBERED = re.compile(r'^\s*\d+[.)]\s+(.*)$')
_TABLE_SEP_CELL = re.compile(r'^:?-+:?$')


def _table_cells(line):
    return [c.strip() for c in line.strip().strip('|').split('|')]


def _is_table_separator(cells):
    return all(_TABLE_SEP_CELL.match(c) for c in cells if c) and any(cells)


# Every link in what was said opens beside the Mood, never in its place.
# Installed as an app, a link that navigated in place would take over the
# app's own window; this way it opens in the browser instead.
_OUT = ' target="_blank" rel="noopener"'


# A file or a release, shown in place (services/embeds.py). `name`, `caption`
# and `title` arrive escaped, as all text does here; what PickiPedia says is
# escaped on the way out.
def _file_embed(name, caption=''):
    from . import embeds
    found = embeds.wiki_file(html.unescape(name))
    if not found:
        return None
    page, full, src = (html.escape(found[k], quote=True) for k in ('page', 'full', 'src'))
    said = f'<span class="caption">{caption}</span>' if caption else ''
    # A player always has its page beside it; a picture opens large (the page's
    # lightbox), the full-size file in data-full, its page from there.
    to_page = f'<a class="caption" href="{page}"{_OUT}>{caption or name} ↗</a>'
    if found['mime'].startswith('video/'):
        return f'<span class="embed"><video controls preload="metadata" playsinline src="{full}"></video>{to_page}</span>'
    if found['mime'].startswith('audio/'):
        return f'<span class="embed"><audio controls preload="none" src="{full}"></audio>{to_page}</span>'
    return (f'<a class="img" href="{page}" data-full="{full}"{_OUT}><img src="{src}" alt="{caption or name}" '
            f'loading="lazy"></a>{said}')


def _release_embed(cid):
    from . import embeds
    found = embeds.release(html.unescape(cid))
    if not found:
        return None
    src, page, title = (html.escape(found[k], quote=True) for k in ('src', 'page', 'title'))
    poster = f' poster="{html.escape(found["poster"], quote=True)}"' if found['poster'] else ''
    # A video's stream is attached by the page (hls.js, or the browser's own HLS), its plain file the fallback.
    hls = html.escape(found.get('hls') or '', quote=True)
    player = (f'<video controls preload="none" playsinline data-hls="{hls}" data-src="{src}"{poster}></video>'
              if found['kind'] == 'video' else f'<audio controls preload="none" src="{src}"></audio>')
    mark = '🎬' if found['kind'] == 'video' else '🎵'
    return f'<span class="embed release">{player}<a class="caption" href="{page}"{_OUT}>{mark} {title}</a></span>'


def _page_embed(title):
    """A File: or Release: page's title, shown as what it is; None for anything else."""
    kind, _, rest = title.partition(':')
    if kind.lower() in ('file', 'image') and rest:
        return _file_embed(rest)
    if kind.lower() == 'release' and rest:
        return _release_embed(rest)
    return None


_COMMONS_FILE = re.compile(r'^https://commons\.wikimedia\.org/wiki/((?:File|Image):[^\s?#<>"]+)$')
_GATEWAY_CID = re.compile(r'^https://ipfs\.delivery-kid\.cryptograss\.live/ipfs/([A-Za-z0-9]{46,64})/?$')


def _wikilink(match):
    target = match.group(1).strip()
    kind = target.partition(':')[0].lower()
    if kind in ('file', 'image', 'release'):
        params = (match.group(2) or '').split('|')
        from .embeds import file_caption
        shown = (_file_embed(target.partition(':')[2], file_caption(params)) if kind != 'release'
                 else _release_embed(target.partition(':')[2]))
        if shown:
            return shown
    label = (match.group(2) or target).strip()
    href = f"{pickipedia_url()}/wiki/{target.replace(' ', '_')}"
    return f'<a class="wikilink" href="{href}"{_OUT}>{label}</a>'


# A PickiPedia page's full address is a wikilink written longhand: shown as
# one, titled by its page, and counted as one. Only an article's own
# address -- /wiki/<Title>, perhaps with a #section -- not an edit, a diff
# or anything else with a query.
PICKIPEDIA_HOSTS = {'pickipedia.xyz', 'www.pickipedia.xyz', 'pickipedia.cryptograss.live'}
_PAGE_URL = re.compile(r'https?://([A-Za-z0-9.-]+)/wiki/([^\s?#<>"]+)(?:#([^\s<>"]*))?$')


def pickipedia_page(url):
    """(title, section) when url is a PickiPedia article's address, else None."""
    match = _PAGE_URL.match(html.unescape(url))
    hosts = PICKIPEDIA_HOSTS | {urlparse(pickipedia_url()).netloc.lower()}
    if not match or match.group(1).lower() not in hosts:
        return None
    title = wiki_title(unquote(match.group(2)))
    section = unquote(match.group(3) or '').replace('_', ' ').strip()
    return (title, section) if title else None


def _image(match):
    alt, url = match.group(1), match.group(2)
    if url.startswith('/') or _image_host_allowed(url):
        return f'<a class="img" href="{url}"><img src="{url}" alt="{alt}" loading="lazy"></a>'
    return f'<a href="{url}"{_OUT}>{alt or url}</a>'


def _image_host_allowed(url):
    from urllib.parse import urlsplit
    host = (urlsplit(url.replace('&amp;', '&')).hostname or '').lower()
    return host in getattr(settings, 'MOOD_IMAGE_HOSTS', ())


def step_images(step_messages):
    """{step id: [media urls in its result]} for steps whose tool returned images."""
    from conversations.models import ToolResult
    from conversations.services.media import urls_in

    by_tool = {(m.tooluse.tool_id, m.session_id): str(m.id) for m in step_messages}
    if not by_tool:
        return {}
    found = {}
    results = (ToolResult.objects.filter(tool_use_id__in={t for t, _ in by_tool},
                                         content__icontains='/media/')  # /moods/media/, or /motions/ before
               .values_list('tool_use_id', 'session_id', 'content'))
    for tool_id, session_id, content in results:
        step = by_tool.get((tool_id, session_id))
        if step:
            found.setdefault(step, []).extend(urls_in(content if isinstance(content, str) else str(content)))
    return found


def known_names():
    """Names that can be mentioned: every thinking entity, human or agent."""
    from conversations.models import ThinkingEntity
    return set(ThinkingEntity.objects.values_list('name', flat=True))


def wiki_title(target):
    """A link target as MediaWiki names the page: no fragment, spaces, first letter capital."""
    title = target.split('#', 1)[0].replace('_', ' ').strip().lstrip(':').strip()
    title = re.sub(r'\s+', ' ', title)
    return title[:1].upper() + title[1:]


def wikilinks_in(text):
    """Ordered, de-duplicated PickiPedia titles a text links to, with [[...]] or a page's full address.

    Code is literal, as in the renderer: a [[link]] inside backticks is an
    example, not a link.
    """
    text = _INLINE_CODE.sub('', _FENCE.sub('', text))
    found = [(m.start(), wiki_title(m.group(1))) for m in _WIKILINK.finditer(text)]
    # A page's full address counts too, labelled or not.
    for match in _URL.finditer(text):
        page = pickipedia_page(_trim_url(match.group(1))[0])
        if page:
            found.append((match.start(), page[0]))
    seen, out = set(), []
    for _, title in sorted(found):
        if title and title not in seen:
            seen.add(title)
            out.append(title)
    return out


def mentions_in(text, mentionable):
    """Ordered, de-duplicated names mentioned in text, restricted to known ones.

    Restricting to known names is what keeps an email address or a stray
    handle from becoming a mention. Matching is case-insensitive; the
    canonical (lowercase) name is returned.
    """
    lowered = {n.lower() for n in mentionable}
    known_as = _aliases(lowered)  # PickiPedia names: @JMyles is @justin
    # Code is literal for the renderer, so it must be literal here too, or
    # the mention count and the highlighted text disagree.
    text = _INLINE_CODE.sub('', _FENCE.sub('', text))
    seen, out = set(), []
    for match in _MENTION.finditer(text):
        name = match.group(1).rstrip('.').lower()
        name = known_as.get(name, name)
        if name in lowered and name not in seen:
            seen.add(name)
            out.append(name)
    return out


# --- replies -----------------------------------------------------------------
# A reply is a post that opens with '↩ #m-<id>' (the composer writes it). It
# answers that message, and addresses its author if that's a person: they're
# notified as if @mentioned. A reply to an agent's message wakes nobody --
# only an @mention does -- though the agent may take it up, like any post.
# It lives in the post itself, like any #m- link, so a woken agent reads from
# the message replied to.
_REPLY = re.compile(r'^↩ #m-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})[ \t]*\n?')


def reply_to(text):
    """(the id a reply answers, the rest of its text), or (None, text)."""
    match = _REPLY.match(text or '')
    return (match[1], text[match.end():]) if match else (None, text)


def snippet_of(text):
    """A message's first words as a line of plain text: links as their words, an image as 🖼,
    code and voice blocks left out -- for a reply's quote and a linked message's card."""
    from .handoff import split as split_handoffs
    from .voice import split_voices
    text, _ = split_voices(text or '')
    text, _ = split_handoffs(text)
    text = re.sub(r'```.*?(```|$)', ' ', text, flags=re.S)
    text = re.sub(r'!\[[^\]]*\]\([^)]*\)', '🖼', text)
    text = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', text)
    text = re.sub(r'[*_`]+', '', text)
    return re.sub(r'\s+', ' ', text).strip()


def replied(message_id):
    """What a reply answers, as {'id', 'sender', 'snippet'}; None if it's gone."""
    from conversations.models import Message
    target = Message.objects.filter(id=message_id).first()
    if target is None:
        return None
    from conversations.models import ThinkingEntity
    _, said = reply_to(prose(target.content) or '')
    return {'id': str(target.id), 'sender': target.sender_id, 'snippet': snippet_of(said)[:140],
            'is_human': ThinkingEntity.objects.filter(name=target.sender_id, is_biological_human=True).exists()}


def addressed_in(text, mentionable, answered=None, by=None):
    """Who a post addresses: whoever it @mentions and, for a reply, the person
    who wrote what it answers -- not an agent (only an @mention wakes one), and
    not `by`, its own author (replying to yourself addresses nobody). What
    notifications, wakes and the PickiPedia-tier check all go by, so they can't
    disagree. `answered`: replied(), if already looked up."""
    out = mentions_in(text, mentionable)
    target, _ = reply_to(text)
    if target:
        answered = answered or replied(target)
        sender = (answered or {}).get('sender') if (answered or {}).get('is_human') else None
        if sender and sender != by and sender in {n.lower() for n in mentionable} and sender not in out:
            out = [sender] + out
    return out


def _aliases(lowered):
    """{lowercased PickiPedia name: name here}, for the names that can be mentioned."""
    from .wiki_auth import aliases
    return {wiki: here for wiki, here in aliases().items() if here in lowered}


def mention_page(who):
    """A name's PickiPedia user page: by the PickiPedia name hunter's inventory gives,
    else by its own (a PickiPedia sign-in is named here as it is there)."""
    from .wiki_auth import names, user_page
    return user_page(names().get(who) or who[:1].upper() + who[1:])


def _mention(mentionable, stash=lambda markup: markup):
    lowered = {n.lower() for n in mentionable}
    known_as = _aliases(lowered)

    def repl(match):
        raw = match.group(1)
        trailing = ''
        if raw.endswith('.'):
            raw, trailing = raw[:-1], '.'
        who = known_as.get(raw.lower(), raw.lower())
        if who not in lowered:
            return match.group(0)
        # A link to who it is, on PickiPedia; still a .mention, highlighted when it's you.
        return stash(f'<a class="mention" data-who="{who}" href="{html.escape(mention_page(who))}"{_OUT}>@{raw}</a>') + trailing
    return repl


def client_parts(msg):
    """What wrote a web post, from its client_version: {'mobile', 'wiki'} or fewer."""
    return set((msg.client_version or '').split('/')[1:])


def from_wiki_tier(msg):
    """Posted from a PickiPedia (wiki-tier) device: its @agent is just text."""
    return 'wiki' in client_parts(msg)


def _trim_url(url):
    """(url, tail): sentence punctuation after a URL isn't part of it. A
    closing parenthesis is, when it closes one the URL opened, as in
    /wiki/Tony_Rice_(guitarist)."""
    tail = ''
    while url and url[-1] in _TRAILING_PUNCT:
        if url[-1] == ')' and url.count('(') >= url.count(')'):
            break
        tail, url = url[-1] + tail, url[:-1]
    return url, tail


# A Yarn clip (a line from a film or show, as a short video): a card that
# plays the clip in place when pressed. Nothing is fetched from Yarn until
# someone presses it -- a reader's browser doesn't call on a site nobody
# chose -- and the page builds the player from the id alone (moods.html).
YARN_HOSTS = {'yarn.co', 'www.yarn.co', 'getyarn.io', 'www.getyarn.io'}
_YARN_CLIP = re.compile(r'https://([a-z.]+)/yarn-clip/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/?$')


def yarn_clip(url):
    """The clip id when url is a Yarn clip's page, else None."""
    match = _YARN_CLIP.match(url)
    return match.group(2) if match and match.group(1) in YARN_HOSTS else None


# A message's own link (its time copies it): a card, not a URL (services/links.py).
_PERMALINK = re.compile(r'^https?://[^/\s]+/moods/[\w-]+/#m-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$')
# '#general' (a Mood) or '#m-<id>' (a message, or the id's first 8). Not '&#x27;'
# (an escaped quote), not 'C#', not a URL's fragment (those are linked already).
_HASH = re.compile(r'(?<![\w&#/;=])#(m-(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{8})'
                   r'|[a-z0-9][a-z0-9-]{0,79})(?![\w-])', re.I)


def _message_link(ref):
    """A card for a linked message -- who, where, its first words -- or None if it's not in a Mood."""
    from .links import card
    from .wiki_auth import names
    found = card(ref)
    if not found:
        return None
    who = names().get(found['sender'], found['sender'])
    return (f'<a class="msglink" href="/moods/{found["mood"]}/#m-{found["id"]}" data-mood="{found["mood"]}" '
            f'data-reveal="{found["id"]}">↗ <strong>{html.escape(who)}</strong> in #{found["mood"]}: '
            f'“{html.escape(found["snippet"])}”</a>')


def _hash_link(match):
    ref = match.group(1)
    if ref[:2].lower() == 'm-' and re.fullmatch(r'm-[0-9a-f-]{8,36}', ref, re.I):
        return _message_link(ref[2:]) or match.group(0)
    from .links import mood_names
    slug = mood_names().get(ref.lower())
    if not slug:
        return match.group(0)  # '#96', '#fff', a Mood nobody has: as written
    return f'<a class="moodlink" href="/moods/{slug}/" data-mood="{slug}">#{ref}</a>'


def _link_url(match):
    """Link a bare URL, leaving sentence punctuation outside the anchor."""
    url, tail = _trim_url(match.group(1))
    # A picture, a recording, a release: shown in place (services/embeds.py).
    commons, gateway = _COMMONS_FILE.match(url), _GATEWAY_CID.match(url)
    shown = (_page_embed(unquote(commons.group(1)).replace('_', ' ')) if commons
             else _release_embed(gateway.group(1)) if gateway else None)
    if shown is None and (page := pickipedia_page(url)) and not page[1]:
        shown = _page_embed(page[0])
    if shown:
        return shown + tail
    own = _PERMALINK.match(url)
    if own and (card := _message_link(own.group(1))):
        return card + tail
    clip = yarn_clip(url)
    if clip:
        # Kept here already (services/yarn_kept.py): ▶ plays our copy, never asking Yarn.
        from .yarn_kept import kept_still
        here, still = kept_still(clip)
        played = (f' data-kept="{here}"' if here else '') + (f' data-still="{still}"' if still else '')
        return f'<a class="yarn" href="{url}" data-yarn="{clip}"{played}{_OUT}>▶ Yarn clip</a>{tail}'
    page = pickipedia_page(url)
    if page:
        title, section = page
        label = html.escape(title + (f' § {section}' if section else ''), quote=False)
        return f'<a class="wikilink" href="{url}"{_OUT}>{label}</a>{tail}'
    return f'<a href="{url}"{_OUT}>{url}</a>{tail}'


def plain_links(text):
    """The web links in what was said that show as plain links -- not a picture, a release, a page
    of PickiPedia, a Yarn clip or a message here -- in order, each once: the ones a card is for
    (services/unfurl.py)."""
    text = re.sub(r'```.*?(```|$)', ' ', text or '', flags=re.S)
    text = _INLINE_CODE.sub(' ', text)
    found = []
    for match in _URL.finditer(text):
        url, _ = _trim_url(match.group(1))
        if url in found or urlparse(url).hostname in OWN_HOSTS:
            continue
        if _link_url(match).startswith(f'<a href="{url}"'):
            found.append(url)
    return found


OWN_HOSTS = {'magenta.cryptograss.live', 'memory-lane.maybelle.cryptograss.live'}


def _inline(text, mentionable=()):
    # Inline code is literal: lift it out so nothing below formats it.
    codes = []

    def keep(match):
        codes.append(match.group(1))
        return f'\x01{len(codes) - 1}\x01'

    text = _INLINE_CODE.sub(keep, text)

    # Each linker's output is lifted out too, so no later pass ever reads
    # markup: a URL inside an href, say, must not be linked again -- that
    # would let text break out of the attribute.
    made = []

    def stash(markup):
        made.append(markup)
        return f'\x02{len(made) - 1}\x02'

    text = _IMAGE.sub(lambda m: stash(_image(m)), text)
    text = _WIKILINK.sub(lambda m: stash(_wikilink(m)), text)
    text = _AUDIO.sub(lambda m: stash(f'<span class="memo">{m.group(1)}</span>'
                                      f'<audio controls preload="none" src="{m.group(2)}" title="{m.group(1)}"></audio>'), text)
    text = _MD_LINK.sub(lambda m: stash(f'<a href="{m.group(2)}"{_OUT}>{m.group(1)}</a>'), text)
    text = _URL.sub(lambda m: stash(_link_url(m)), text)
    text = _HASH.sub(lambda m: stash(_hash_link(m)), text)
    if mentionable:
        text = _MENTION.sub(_mention(mentionable, stash), text)
    text = _BOLD.sub(r'<strong>\1</strong>', text)
    text = _ITALIC.sub(r'<em>\1</em>', text)
    text = re.sub(r'\x02(\d+)\x02', lambda m: made[int(m.group(1))], text)
    for i, code in enumerate(codes):
        text = text.replace(f'\x01{i}\x01', f'<code>{code}</code>')
    return text


# The team's saved clips (services/clips.py) are posts too: a save or a
# forget shows as one line, without the clip; a use shows just the clip (its
# name stays in the record). '🎬 name <link>' is how uses were first posted.
_YARN_SAVED = re.compile(r'^/(?:clips|yarn) save ([a-z0-9][a-z0-9_-]{0,34}) (https://\S+)$')
_YARN_FORGOTTEN = re.compile(r'^/(?:clips|yarn) forget ([a-z0-9][a-z0-9_-]{0,34})$')
_YARN_USED = re.compile(r'^(?:/clips|/yarn|🎬) [a-z0-9][a-z0-9_-]{0,34} (https://www\.yarn\.co/yarn-clip/[0-9a-f-]{36})$')


def render_html(text, mentionable=()):
    """Escape, then translate the markdown the agent writes into HTML."""
    plain = text.strip()
    saved, forgotten, used = _YARN_SAVED.match(plain), _YARN_FORGOTTEN.match(plain), _YARN_USED.match(plain)
    if saved:
        text = f'🎬 saved **{saved[1]}**'
    elif forgotten:
        text = f'🎬 forgot **{forgotten[1]}**'
    elif used:
        text = used[1]
    text = html.escape(_PLACEHOLDER_CHARS.sub('', text), quote=True)

    def inline(s):
        return _inline(s, mentionable)

    # Lift fenced code out before anything else can touch it.
    fences = []

    def keep(match):
        fences.append(match.group(1).rstrip('\n'))
        return f'\x00{len(fences) - 1}\x00'

    text = _FENCE.sub(keep, text)

    blocks = []
    paragraph, list_items, list_tag = [], [], None
    table_rows = []

    def flush_paragraph():
        if paragraph:
            blocks.append('<p>' + inline('<br>'.join(paragraph)) + '</p>')
            paragraph.clear()

    def flush_list():
        nonlocal list_tag
        if list_items:
            items = ''.join(f'<li>{inline(i)}</li>' for i in list_items)
            blocks.append(f'<{list_tag}>{items}</{list_tag}>')
            list_items.clear()
        list_tag = None

    def flush_table():
        rows = [r for r in table_rows if not _is_table_separator(r)]
        if rows:
            head = ''.join(f'<th>{inline(c)}</th>' for c in rows[0])
            body = ''.join(
                '<tr>' + ''.join(f'<td>{inline(c)}</td>' for c in r) + '</tr>'
                for r in rows[1:]
            )
            blocks.append(f'<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>')
        table_rows.clear()

    for line in text.split('\n'):
        stripped = line.strip()
        if not stripped:
            flush_paragraph()
            flush_list()
            flush_table()
            continue
        if stripped.startswith('|') and stripped.endswith('|'):
            flush_paragraph()
            flush_list()
            table_rows.append(_table_cells(stripped))
            continue
        flush_table()
        if stripped.startswith('\x00'):
            flush_paragraph()
            flush_list()
            blocks.append(stripped)
            continue
        heading = _HEADING.match(stripped)
        if heading:
            flush_paragraph()
            flush_list()
            blocks.append(f'<h4>{inline(heading.group(1))}</h4>')
            continue
        bullet, numbered = _BULLET.match(line), _NUMBERED.match(line)
        if bullet or numbered:
            flush_paragraph()
            tag = 'ul' if bullet else 'ol'
            if list_tag != tag:
                flush_list()
                list_tag = tag
            list_items.append((bullet or numbered).group(1))
            continue
        flush_list()
        paragraph.append(stripped)

    flush_paragraph()
    flush_list()
    flush_table()

    out = '\n'.join(blocks)
    for i, code in enumerate(fences):
        out = out.replace(f'\x00{i}\x00', f'<pre><code>{code}</code></pre>')
    return out


def timeline(mood, after=None, before=None, limit=None, start=None):
    """Readable turns and the agent's tool steps, oldest first.

    Yields ('turn', message, text), ('quiet', message, reason) for an
    agent's choice not to speak, ('thought', message, text) for thinking the
    harness kept, ('compaction', message, summary) where a session was
    compacted, and ('step', message, None). A step is
    one tool call; its result is fetched on demand (step_detail), so the
    thread stays light. `limit` keeps the newest that many items -- a first
    load, or a page further back with `before`. `start` includes that message
    and everything after it (a runner reading from a linked message).
    """
    from conversations.models import ThinkingEntity

    speakers = set(ThinkingEntity.objects.values_list('name', flat=True))
    rows = mood.messages.filter(is_sidechain=False).select_related('sender', 'tooluse', 'thought')
    if after is not None:
        rows = rows.filter(created_at__gt=after.created_at)
    if start is not None:
        rows = rows.filter(created_at__gte=start.created_at)
    if before is not None:
        rows = rows.filter(created_at__lt=before.created_at)

    def item(msg):
        if msg.source_file in EVENT_SOURCES and isinstance(msg.content, dict):
            return ('event', msg, msg.content)  # a server redeployed, someone stopped an agent: a line in the thread
        if msg.sender_id in MACHINERY_SENDERS or msg.sender_id not in speakers:
            return None
        if hasattr(msg, 'tooluse'):
            return ('step', msg, None)
        if hasattr(msg, 'thought'):
            thinking = thought_text(msg.content)
            return ('thought', msg, thinking) if thinking else None
        text = prose(msg.content)
        if not text:
            return None
        reason = quiet_reason(text)
        if reason is not None:
            return ('quiet', msg, reason)
        if is_compaction(text):
            return ('compaction', msg, text)
        if is_wrapper(text):
            return None
        return ('turn', msg, text)

    if limit is None:
        for msg in rows.order_by('created_at'):
            found = item(msg)
            if found:
                yield found
        return

    # Newest first, in chunks, until there are enough.
    found, last = [], None
    while len(found) < limit:
        chunk = rows.order_by('-created_at')
        if last is not None:
            chunk = chunk.filter(created_at__lt=last)
        chunk = list(chunk[:500])
        if not chunk:
            break
        last = chunk[-1].created_at
        for msg in chunk:
            got = item(msg)
            if got:
                found.append(got)
                if len(found) == limit:
                    break
    yield from reversed(found)


_STEP_WORDS = {
    'Bash': 'ran', 'Read': 'read', 'Edit': 'edited', 'Write': 'wrote', 'Grep': 'searched', 'Glob': 'searched',
    'WebFetch': 'fetched', 'WebSearch': 'searched the web', 'Agent': 'asked a helper', 'Task': 'asked a helper',
    'ToolSearch': 'looked up tools', 'Skill': 'read a skill', 'TodoWrite': 'planned', 'NotebookEdit': 'edited',
    'TaskCreate': 'tracked', 'TaskUpdate': 'tracked', 'TaskList': 'tracked', 'SendMessage': 'messaged a helper',
    'SendUserFile': 'sent a file', 'AskUserQuestion': 'asked',
}


def step_summary(tool, args):
    """A line saying what one tool call was for."""
    if not isinstance(args, dict):
        return ''
    for key in ('description', 'file_path', 'path', 'pattern', 'url', 'query', 'subject', 'prompt', 'command'):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().splitlines()[0][:160]
    for value in args.values():
        if isinstance(value, str) and value.strip():
            return value.strip().splitlines()[0][:160]
    return ''


def step_payload(msg):
    tool = msg.tooluse.tool_name
    return {
        **how_payload(msg),
        'id': str(msg.id),
        'sender': msg.sender_id,
        'created_at': msg.created_at.isoformat(),
        'tool': tool,
        'verb': _STEP_WORDS.get(tool, 'used ' + (tool.split('__')[1] if tool.startswith('mcp__') else tool)),
        'summary': step_summary(tool, msg.content),
    }


def step_detail(msg):
    """One tool call in full: what was asked and what came back."""
    from conversations.models import ToolResult

    result = (ToolResult.objects.filter(tool_use_id=msg.tooluse.tool_id, session_id=msg.session_id)
              .order_by('created_at').first())
    return {
        **step_payload(msg),
        'input': msg.content if isinstance(msg.content, dict) else {'value': msg.content},
        'result': None if result is None else {
            'text': result.content if isinstance(result.content, str) else prose(result.content),
            'is_error': result.is_error,
        },
    }


def model_label(model):
    """'claude-opus-5-5' -> 'Opus 5.5'; anything not Claude's naming as it is."""
    if not model or model.startswith('<'):
        return ''
    if not model.startswith('claude-'):
        return model
    parts = re.sub(r'-\d{8}$', '', model[len('claude-'):]).split('-')
    family = next((p for p in parts if not p.isdigit()), '')
    version = '.'.join(p for p in parts if p.isdigit())
    return f'{family.capitalize()} {version}'.strip()


def how_payload(msg):
    """Model and effort an agent's message ran on ('' when unknown); what it
    wrote and read, in tokens; and whether it ended its turn.

    One response the model gives can be stored as several rows (its thinking,
    its words, a tool call), each with that response's usage: the page counts
    a repeat of the same usage once."""
    out = {'model': model_label(msg.model_backend), 'effort': msg.effort or ''}
    if msg.output_tokens is not None or msg.input_tokens is not None:
        out['out'] = msg.output_tokens or 0
        out['ctx'] = sum(n or 0 for n in (msg.input_tokens, msg.cache_read_input_tokens,
                                          msg.cache_creation_input_tokens))
        out['cached'] = msg.cache_read_input_tokens or 0  # of ctx, read back from the cache: the cheap part
    if msg.stop_reason:
        out['stop'] = msg.stop_reason
    return out


def attestation_of(msg):
    """What makes a turn an attestation -- exactly what was signed, the
    signature, the key -- or None."""
    c = msg.content
    if msg.source_file != 'mood-attest' or not isinstance(c, dict) or c.get('type') != 'attestation':
        return None
    return {k: c.get(k, '') for k in ('signed', 'signature', 'key', 'namespace')}


def turn_payload(msg, text, mentionable=()):
    from .links import linked_from
    # A ```voice block is how its writer wants it read aloud: performed, not shown.
    # A ```handoff block went to another Mood (services/handoff.py): a line says so, not the block.
    from .handoff import split as split_handoffs
    from .voice import split_voices
    text, directions = split_voices(text)
    text, _ = split_handoffs(text)
    target, said = reply_to(text)
    answered = replied(target) if target else None
    return {
        'voiced': bool(directions),
        'mobile': 'mobile' in client_parts(msg),  # posted from a phone or tablet
        # Signed in with PickiPedia, not an SSH key: chat and mentions of people only.
        'tier': 'wiki' if from_wiki_tier(msg) else None,
        # Several blocks (auditions, a dialogue): one ▶ each, named for its voice.
        'voices': [d['voice'] or 'the house voice' for d in directions] if len(directions) > 1 else [],
        'attested': attestation_of(msg),
        **how_payload(msg),
        'id': str(msg.id),
        'sender': msg.sender_id,
        'is_human': bool(getattr(getattr(msg.sender, 'thinkingentity', None),
                                 'is_biological_human', False)),
        'created_at': msg.created_at.isoformat(),
        # Typed into the web composer rather than a runtime session: nobody
        # live is listening for it, so the poller need not wait.
        'via': 'web' if msg.source_file in POSTED else 'session',
        'text': text,
        # A reply: what it answers, shown above it; its author is addressed.
        'reply': answered if target else None,
        'mentions': addressed_in(text, mentionable, answered, by=msg.sender_id),
        'html': render_html(said, mentionable),
        # Who linked here, or replied (services/links.py): shown under it.
        'linked_from': linked_from(msg.id),
    }


# --- activity: what an agent is doing right now ------------------------------

TURN_ENDS = {'end_turn', 'refusal', 'stop_sequence', 'max_tokens'}
ACTIVITY_WINDOW = 600  # seconds; a streak older than this is a session that died
RECENT = 60  # rows read to see what's happening now
_TOOL_WORDS = {
    'Bash': 'running a command', 'Read': 'reading', 'Grep': 'searching', 'Glob': 'searching',
    'Edit': 'editing', 'Write': 'writing a file', 'NotebookEdit': 'editing',
    'WebFetch': 'reading the web', 'WebSearch': 'searching the web',
    'Agent': 'working with helpers', 'Task': 'working with helpers',
    'ToolSearch': 'finding a tool', 'Skill': 'reading up', 'TodoWrite': 'planning',
}
_MCP_WORDS = {'playwright': 'using the browser', 'pickipedia': 'on PickiPedia',
              'magenta-memory-v2': 'remembering', 'magenta-memory': 'remembering'}


def _doing(msg):
    """A few words for what a machinery message says the agent is doing."""
    if hasattr(msg, 'tooluse'):
        name = msg.tooluse.tool_name
        if name.startswith('mcp__'):
            server = name.split('__')[1]
            return _MCP_WORDS.get(server, f'using {server}')
        return _TOOL_WORDS.get(name, f'using {name}')
    return 'thinking'


def _when(msg):
    return msg.timestamp / 1000 if msg.timestamp else msg.created_at.timestamp()


# --- how full an agent's context is ------------------------------------------
# Every assistant line records what its model read to write it: fresh input
# plus cache reads and writes. The newest such line in a Mood's newest
# session is how full that agent's context is now -- what a wake would
# resume into. Windows by model; the record shows Opus 5.5 sessions passing
# 865k tokens, so its window is 1M. Anything unlisted is taken as 200k,
# and a session seen past its window is taken to have the larger one.
CONTEXT_WINDOWS = (('claude-opus-5', 1_000_000),)
DEFAULT_WINDOW = 200_000


def context_window(model, tokens=0):
    window = next((w for prefix, w in CONTEXT_WINDOWS if (model or '').startswith(prefix)), DEFAULT_WINDOW)
    return window if tokens <= window else max(window, 1_000_000)


def context_in(mood, agent):
    """{'tokens', 'window', 'model', 'at'} for `agent`'s context here, or None."""
    # A slash command's output (/context, /usage) is a "<synthetic>" message
    # (stored with no model) that read nothing: it says nothing about the context.
    row = (mood.messages.filter(sender_id=agent, is_sidechain=False, input_tokens__isnull=False,
                                  model_backend__isnull=False)
           .order_by('-created_at')
           .values('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens', 'model_backend',
                   'created_at').first())
    if row is None:
        return None
    tokens = sum(row[k] or 0 for k in ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens'))
    window = context_window(row['model_backend'], tokens)
    # Compacted since (`@magent /compact`): no turn has measured the context
    # yet, so it is about the summary's size until the next one does.
    summary = compacted_since(mood, agent, row['created_at'])
    if summary is not None:
        return {'tokens': len(summary.text) // CHARS_PER_TOKEN, 'window': window, 'model': row['model_backend'] or '',
                'at': summary.at.isoformat(), 'compacted': True}
    return {'tokens': tokens, 'window': window, 'model': row['model_backend'] or '', 'at': row['created_at'].isoformat()}


CHARS_PER_TOKEN = 4  # roughly, for English prose


class _Summary:
    def __init__(self, text, at):
        self.text, self.at = text, at


def compacted_since(mood, agent, when):
    """The summary `agent`'s context went on from, if it was compacted after
    `when` (its last measured turn); else None. What follows a summary
    before the next turn is only the command's own echo."""
    for row in (mood.messages.filter(sender_id=agent, is_sidechain=False, created_at__gt=when)
                .order_by('-created_at').values('content', 'created_at')[:20]):
        content = row['content']
        if isinstance(content, list):
            content = ' '.join(b.get('text', '') for b in content if isinstance(b, dict) and b.get('type') == 'text')
        if isinstance(content, str) and is_compaction(content):
            return _Summary(content, row['created_at'])
    return None


# --- a mention the runner is holding ----------------------------------------
# The record can show that a post names an agent and nothing has answered it,
# but not why: a runner may be holding it -- its hourly cap reached, the
# agent hushed here, a turn already under way. The runner says so, and the
# Mood shows "held" instead of an endless "waking". Ephemeral, like typing:
# a shared cache entry per Mood, agent -> {reason, until, at}, which lapses
# unless the runner renews it, so a runner that dies leaves no stale hold.
HELD_FOR = 600  # seconds a hold lasts unless renewed


def _held_key(slug):
    return f'held:{slug}'


def held_in(slug, now=None):
    """agent -> {'reason', 'until', 'at'} for each hold still in force."""
    from django.core.cache import cache
    import time
    now = now or time.time()
    return {agent: h for agent, h in (cache.get(_held_key(slug)) or {}).items() if now - h['at'] < HELD_FOR}


def set_held(slug, agent, reason, until=None, now=None):
    """Hold `agent`'s next turn in `slug` for `reason`; an empty reason lifts it."""
    from django.core.cache import cache
    import time
    now = now or time.time()
    holds = held_in(slug, now)
    if reason:
        holds[agent] = {'reason': reason, 'until': until, 'at': now}
    else:
        holds.pop(agent, None)
    cache.set(_held_key(slug), holds, HELD_FOR)
    return holds


def activity(mood, now=None):
    """What an agent in this Mood is doing now, or None: see _activity.
    Someone stopping the agent since that began ends it at once, without
    waiting for its runner to notice."""
    doing = _activity(mood, now)
    if doing and doing['doing'] != 'held':
        stopped = latest_interrupt(mood, doing['agent'])
        if stopped and stopped['at_ts'] >= doing['since']:
            return None
    return doing


def latest_interrupt(mood, agent=None):
    """{'agent', 'by', 'at', 'at_ts'} for the newest time someone stopped
    `agent` (any agent, if None) here; None if nobody ever has."""
    for msg in (mood.messages.filter(source_file=INTERRUPT_SOURCE).order_by('-created_at')
                .only('content', 'timestamp', 'created_at')[:20]):
        c = msg.content if isinstance(msg.content, dict) else {}
        if agent is None or c.get('agent') == agent:
            at = _when(msg)
            from datetime import datetime, timezone as tz
            return {'agent': c.get('agent'), 'by': c.get('by'), 'at_ts': at,
                    'at': datetime.fromtimestamp(at, tz.utc).isoformat()}
    return None


def _activity(mood, now=None):
    """What an agent in this Mood is doing now, or None if nothing is underway.

    Read straight from the record, so no process has to report in: every
    thought, tool call and prompt streams into the Mood as it happens, and
    an assistant line whose stop_reason is end_turn closes the turn. So an
    agent is working from the first line after its last finished turn until
    the next one, and the newest line says what it's doing. A web post that
    names an agent and has nothing after it means the agent is being woken,
    unless its runner has said it is holding that turn, and why.
    """
    from conversations.models import ThinkingEntity
    import time

    now = now or time.time()
    agents = set(ThinkingEntity.objects.filter(is_biological_human=False).values_list('name', flat=True))
    # Helpers' lines (sidechains) are the agent's own call still running, so
    # they neither describe nor end its turn. System lines are the harness's
    # bookkeeping -- Claude Code writes one just after a turn ends -- and
    # say nothing about whether anyone is working.
    recent = list(mood.messages.filter(is_sidechain=False).exclude(sender_id='system')
                  .select_related('sender', 'tooluse', 'thought', 'toolresult')
                  .order_by('-created_at')[:RECENT])
    if not recent:
        return None
    newest = recent[0]

    if newest.source_file in POSTED:
        # A wiki-tier post's @agent is just text: no agent is waking for it.
        named = [] if from_wiki_tier(newest) else [n for n in addressed_in(prose(newest.content), agents, by=newest.sender_id)]
        if not named:
            return None
        # Held only if the runner said so after this post: a newer post is
        # waking until the runner has looked at it. A hold is shown for as
        # long as the runner keeps it -- an hour, at the hourly cap -- not
        # just the window an unexplained wait is shown for.
        hold = held_in(mood.slug, now).get(named[0])
        if hold and hold['at'] >= _when(newest):
            return {'agent': named[0], 'doing': 'held', 'why': hold['reason'], 'until': hold.get('until'),
                    'since': _when(newest)}
        if now - _when(newest) > ACTIVITY_WINDOW:
            return None
        return {'agent': named[0], 'doing': 'waking', 'since': _when(newest)}

    if now - _when(newest) > ACTIVITY_WINDOW:
        return None
    if newest.sender_id in agents and newest.stop_reason in TURN_ENDS:
        return None
    # Rows imported before stop_reason was kept: a plain reply that has sat
    # for half a minute with nothing after it is taken as the end of a turn.
    if (newest.sender_id in agents and newest.stop_reason is None and now - _when(newest) > 30
            and not hasattr(newest, 'tooluse') and not hasattr(newest, 'thought')):
        return None

    streak = []
    for msg in recent:
        if msg.sender_id in agents and msg.stop_reason in TURN_ENDS:
            break
        if msg.source_file in POSTED:
            break
        streak.append(msg)
    agent = next((m.sender_id for m in streak if m.sender_id in agents), None) or sorted(agents or {'magent'})[0]
    how = next((how_payload(m) for m in streak if m.sender_id in agents and m.model_backend), {'model': '', 'effort': ''})
    start = streak[-1]
    if len(streak) == len(recent) == RECENT:
        # A long turn runs past the rows read above: find where it began.
        from django.db.models import Q
        mine = mood.messages.filter(is_sidechain=False)
        boundary = (mine.filter(created_at__lt=start.created_at)
                    .filter(Q(sender_id__in=agents, stop_reason__in=TURN_ENDS) | Q(source_file__in=POSTED))
                    .order_by('-created_at').first())
        if boundary is not None:
            start = mine.filter(created_at__gt=boundary.created_at).order_by('created_at').first() or start
    return {'agent': agent, 'doing': _doing(newest), 'since': _when(start), **{k: v for k, v in how.items() if v}}


# --- background tasks an agent is supervising ------------------------------

_TASK_STARTED = re.compile(r'Command running in background with ID: (\w+)|Async agent launched successfully.*?agentId: (\w+)', re.S)
_TASK_ENDED = re.compile(r'<task-id>(\w+)</task-id>.*?<status>(\w+)</status>', re.S)
TASK_ENDINGS = {'completed', 'failed', 'stopped', 'killed', 'error', 'cancelled'}
# A task older than this with no word of its end is presumed gone: its notice
# can be lost when the session that started it exits first (in a terminal;
# a runner's turns are handled exactly, below). A background
# command can't outlive Claude Code's two-hour cap on its timeout; a helper
# agent has no cap, so it gets longer.
TASK_HORIZONS = {'command': 2.5 * 3600, 'helper': 12 * 3600}
TASK_HORIZON = max(TASK_HORIZONS.values())


def background_tasks(mood, now=None):
    """Commands and helpers an agent started in the background here and that
    haven't ended, read from the record: the tool result that started each
    one, and the <task-notification> that says it ended.
    """
    import json
    import time
    from datetime import datetime, timezone as tz
    from conversations.models import ToolResult, ToolUse

    now = now or time.time()
    since = datetime.fromtimestamp(now - TASK_HORIZON, tz.utc)
    rows = (mood.messages.filter(is_sidechain=False, created_at__gte=since)
            .filter(models_q(content__icontains='in background with ID')
                    | models_q(content__icontains='Async agent launched')
                    | models_q(content__icontains='<task-id>'))
            .order_by('created_at'))
    started, ended = {}, set()
    for msg in rows:
        text = msg.content if isinstance(msg.content, str) else json.dumps(msg.content)
        # Only a tool result that *is* a start message starts a task, and only
        # a notification ends one: output that merely quotes them (a log, a
        # query of this very table) is neither.
        if not hasattr(msg, 'toolresult'):
            for match in _TASK_ENDED.finditer(text):
                if match.group(2).lower() in TASK_ENDINGS:
                    ended.add(match.group(1))
            continue
        match = _TASK_STARTED.match(text.lstrip())
        if not match:
            continue
        task_id = match.group(1) or match.group(2)
        ended.discard(task_id)  # a helper resumed after it last finished
        use = (ToolUse.objects.filter(tool_id=msg.toolresult.tool_use_id, session_id=msg.session_id)
               .only('content', 'tool_name').first())
        args = use.content if use is not None and isinstance(use.content, dict) else {}
        started[task_id] = {
            'id': task_id,
            'kind': 'helper' if match.group(2) else 'command',
            'label': (args.get('description') or args.get('command') or args.get('prompt') or task_id)[:120],
            'since': _when(msg),
            'step': str(use.pk) if use is not None else None,  # the call that started it, shown on demand
            '_session': msg.session_id, '_at': msg.created_at,
        }
    # A turn the runner launched (claude -p) stops whatever it left running
    # when it ends, so a task started before its session's turn-result
    # (views_runner.finish_turn) is over, whether or not its notice arrived.
    over = {}
    sessions = {t['_session'] for t in started.values() if t['_session']}
    for session, at in (mood.messages.filter(sender_id='system', session_id__in=sessions, content__type='turn-result')
                        .values_list('session_id', 'created_at')):
        over[session] = max(at, over.get(session, at))
    running = []
    for task_id, task in started.items():
        session, at = task.pop('_session'), task.pop('_at')
        if task_id in ended or task['since'] < now - TASK_HORIZONS[task['kind']]:
            continue
        if session in over and over[session] >= at:
            continue
        running.append(task)
    return running


def models_q(**kwargs):
    from django.db.models import Q
    return Q(**kwargs)


def last_said(mood, scan=200):
    """When something was last said here: a person's post, or an agent speaking.
    Not an agent's silences (the dots), nor its thinking and tool calls on the
    way to one, nor the system's rows (a redeploy, a rename, a turn's tally):
    a Mood where an agent quietly looked and let it pass hasn't become active."""
    rows = (mood.messages.filter(is_sidechain=False, thought__isnull=True, tooluse__isnull=True, toolresult__isnull=True)
            .exclude(sender_id__in=MACHINERY_SENDERS).order_by('-created_at').only('content', 'created_at')[:scan])
    oldest = None
    for msg in rows:
        oldest = msg.created_at
        text = prose(msg.content)
        if text and not is_wrapper(text) and quiet_reason(text) is None:
            return msg.created_at
    return oldest  # nothing said in the latest `scan` rows: as good a guess as any


def mood_payload(mood):
    # What was said, not the system's own rows (a redeploy announced in every
    # Mood, a rename, a turn's tally): those mustn't make a Mood look active.
    said = mood.messages.exclude(sender_id='system')
    last = last_said(mood)
    return {
        'slug': mood.slug,
        'title': mood.title or mood.slug,
        'description': mood.description,
        'eth_blockheight': mood.eth_blockheight,
        'message_count': said.count(),
        'last_at': last.isoformat() if last else None,
        'participants': sorted(e.name for e in mood.thinking_entities()),
    }
