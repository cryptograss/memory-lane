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
    messages = motion.messages.select_related('sender').order_by('created_at')
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
_URL = re.compile(r'(?<!["\'>])(https?://[^\s<]+)')
_HEADING = re.compile(r'^#{1,6}\s+(.+)$')
_BULLET = re.compile(r'^\s*[-*–]\s+(.*)$')
_NUMBERED = re.compile(r'^\s*\d+[.)]\s+(.*)$')


def _wikilink(match):
    target = match.group(1).strip()
    label = (match.group(2) or target).strip()
    href = f"{pickipedia_url()}/wiki/{target.replace(' ', '_')}"
    return f'<a class="wikilink" href="{href}">{label}</a>'


def _inline(text):
    text = _INLINE_CODE.sub(r'<code>\1</code>', text)
    text = _BOLD.sub(r'<strong>\1</strong>', text)
    text = _ITALIC.sub(r'<em>\1</em>', text)
    text = _WIKILINK.sub(_wikilink, text)
    text = _URL.sub(r'<a href="\1">\1</a>', text)
    return text


def render_html(text):
    """Escape, then translate the markdown the agent writes into HTML."""
    text = html.escape(text, quote=False)

    # Lift fenced code out before anything else can touch it.
    fences = []

    def keep(match):
        fences.append(match.group(1).rstrip('\n'))
        return f'\x00{len(fences) - 1}\x00'

    text = _FENCE.sub(keep, text)

    blocks = []
    paragraph, list_items, list_tag = [], [], None

    def flush_paragraph():
        if paragraph:
            blocks.append('<p>' + _inline('<br>'.join(paragraph)) + '</p>')
            paragraph.clear()

    def flush_list():
        nonlocal list_tag
        if list_items:
            items = ''.join(f'<li>{_inline(i)}</li>' for i in list_items)
            blocks.append(f'<{list_tag}>{items}</{list_tag}>')
            list_items.clear()
        list_tag = None

    for line in text.split('\n'):
        stripped = line.strip()
        if not stripped:
            flush_paragraph()
            flush_list()
            continue
        if stripped.startswith('\x00'):
            flush_paragraph()
            flush_list()
            blocks.append(stripped)
            continue
        heading = _HEADING.match(stripped)
        if heading:
            flush_paragraph()
            flush_list()
            blocks.append(f'<h4>{_inline(heading.group(1))}</h4>')
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

    out = '\n'.join(blocks)
    for i, code in enumerate(fences):
        out = out.replace(f'\x00{i}\x00', f'<pre><code>{code}</code></pre>')
    return out


def turn_payload(msg, text):
    return {
        'id': str(msg.id),
        'sender': msg.sender_id,
        'is_human': bool(getattr(getattr(msg.sender, 'thinkingentity', None),
                                 'is_biological_human', False)),
        'created_at': msg.created_at.isoformat(),
        'html': render_html(text),
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
