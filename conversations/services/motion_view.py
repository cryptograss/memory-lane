"""Turn a Motion into something a person can read.

A Motion is mostly machinery. Of the first 42 messages in the first Motion,
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

from django.conf import settings

MACHINERY_SENDERS = {'tool-result', 'system'}

# Command scaffolding, system reminders and interruption markers are text,
# but they are not conversation.
_WRAPPER_PREFIXES = ('<', '[Request interrupted')


def pickipedia_url():
    return getattr(settings, 'PICKIPEDIA_URL', 'https://pickipedia.xyz').rstrip('/')


def prose(content):
    """Human-readable text from a message's content, or '' if it is machinery."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, dict):
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


def is_wrapper(text):
    return text.startswith(_WRAPPER_PREFIXES)


def turns(motion, after=None):
    """Yield (message, text) for the readable conversation in a Motion.

    `after` is a Message; only turns created after it are yielded, which is
    what a polling client needs.
    """
    from conversations.models import ThinkingEntity

    speakers = set(ThinkingEntity.objects.values_list('name', flat=True))
    messages = motion.messages.filter(is_sidechain=False).select_related('sender').order_by('created_at')
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
_MD_LINK = re.compile(r'\[([^\]]+)\]\((https?://[^)\s]+)\)')
_URL = re.compile(r'(?<!["\'>=])(https?://(?:(?!&quot;|&#x27;)[^\s<])+)')
_TRAILING_PUNCT = '.,;:!?)]\'"'
_HEADING = re.compile(r'^#{1,6}\s+(.+)$')
_BULLET = re.compile(r'^\s*[-*–]\s+(.*)$')
_NUMBERED = re.compile(r'^\s*\d+[.)]\s+(.*)$')
_TABLE_SEP_CELL = re.compile(r'^:?-+:?$')


def _table_cells(line):
    return [c.strip() for c in line.strip().strip('|').split('|')]


def _is_table_separator(cells):
    return all(_TABLE_SEP_CELL.match(c) for c in cells if c) and any(cells)


def _wikilink(match):
    target = match.group(1).strip()
    label = (match.group(2) or target).strip()
    href = f"{pickipedia_url()}/wiki/{target.replace(' ', '_')}"
    return f'<a class="wikilink" href="{href}">{label}</a>'


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
    """Ordered, de-duplicated PickiPedia titles a text links to with [[...]].

    Code is literal, as in the renderer: a [[link]] inside backticks is an
    example, not a link.
    """
    text = _INLINE_CODE.sub('', _FENCE.sub('', text))
    seen, out = set(), []
    for match in _WIKILINK.finditer(text):
        title = wiki_title(match.group(1))
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
    # Code is literal for the renderer, so it must be literal here too, or
    # the mention count and the highlighted text disagree.
    text = _INLINE_CODE.sub('', _FENCE.sub('', text))
    seen, out = set(), []
    for match in _MENTION.finditer(text):
        name = match.group(1).rstrip('.').lower()
        if name in lowered and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def _mention(mentionable):
    lowered = {n.lower() for n in mentionable}

    def repl(match):
        raw = match.group(1)
        trailing = ''
        if raw.endswith('.'):
            raw, trailing = raw[:-1], '.'
        if raw.lower() not in lowered:
            return match.group(0)
        return f'<span class="mention" data-who="{raw.lower()}">@{raw}</span>{trailing}'
    return repl


def _link_url(match):
    """Link a bare URL, leaving sentence punctuation outside the anchor."""
    url, tail = match.group(1), ''
    while url and url[-1] in _TRAILING_PUNCT:
        tail, url = url[-1] + tail, url[:-1]
    return f'<a href="{url}">{url}</a>{tail}'


def _inline(text, mentionable=()):
    # Inline code is literal: lift it out so nothing below formats it.
    codes = []

    def keep(match):
        codes.append(match.group(1))
        return f'\x01{len(codes) - 1}\x01'

    text = _INLINE_CODE.sub(keep, text)
    text = _BOLD.sub(r'<strong>\1</strong>', text)
    text = _ITALIC.sub(r'<em>\1</em>', text)
    text = _WIKILINK.sub(_wikilink, text)
    text = _MD_LINK.sub(r'<a href="\2">\1</a>', text)
    text = _URL.sub(_link_url, text)
    if mentionable:
        text = _MENTION.sub(_mention(mentionable), text)
    for i, code in enumerate(codes):
        text = text.replace(f'\x01{i}\x01', f'<code>{code}</code>')
    return text


def render_html(text, mentionable=()):
    """Escape, then translate the markdown the agent writes into HTML."""
    text = html.escape(text, quote=True)

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


def turn_payload(msg, text, mentionable=()):
    return {
        'id': str(msg.id),
        'sender': msg.sender_id,
        'is_human': bool(getattr(getattr(msg.sender, 'thinkingentity', None),
                                 'is_biological_human', False)),
        'created_at': msg.created_at.isoformat(),
        'text': text,
        'mentions': mentions_in(text, mentionable),
        'html': render_html(text, mentionable),
    }


def motion_payload(motion):
    from django.db.models import Max
    last = motion.messages.aggregate(last=Max('created_at'))['last']
    return {
        'slug': motion.slug,
        'title': motion.title or motion.slug,
        'description': motion.description,
        'eth_blockheight': motion.eth_blockheight,
        'message_count': motion.messages.count(),
        'last_at': last.isoformat() if last else None,
        'participants': sorted(e.name for e in motion.thinking_entities()),
    }
