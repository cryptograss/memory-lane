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


def _mention(mentionable, stash=lambda markup: markup):
    lowered = {n.lower() for n in mentionable}

    def repl(match):
        raw = match.group(1)
        trailing = ''
        if raw.endswith('.'):
            raw, trailing = raw[:-1], '.'
        if raw.lower() not in lowered:
            return match.group(0)
        return stash(f'<span class="mention" data-who="{raw.lower()}">@{raw}</span>') + trailing
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

    # Each linker's output is lifted out too, so no later pass ever reads
    # markup: a URL inside an href, say, must not be linked again -- that
    # would let text break out of the attribute.
    made = []

    def stash(markup):
        made.append(markup)
        return f'\x02{len(made) - 1}\x02'

    text = _WIKILINK.sub(lambda m: stash(_wikilink(m)), text)
    text = _MD_LINK.sub(lambda m: stash(f'<a href="{m.group(2)}">{m.group(1)}</a>'), text)
    text = _URL.sub(lambda m: stash(_link_url(m)), text)
    if mentionable:
        text = _MENTION.sub(_mention(mentionable, stash), text)
    text = _BOLD.sub(r'<strong>\1</strong>', text)
    text = _ITALIC.sub(r'<em>\1</em>', text)
    text = re.sub(r'\x02(\d+)\x02', lambda m: made[int(m.group(1))], text)
    for i, code in enumerate(codes):
        text = text.replace(f'\x01{i}\x01', f'<code>{code}</code>')
    return text


def render_html(text, mentionable=()):
    """Escape, then translate the markdown the agent writes into HTML."""
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


def turn_payload(msg, text, mentionable=()):
    return {
        'id': str(msg.id),
        'sender': msg.sender_id,
        'is_human': bool(getattr(getattr(msg.sender, 'thinkingentity', None),
                                 'is_biological_human', False)),
        'created_at': msg.created_at.isoformat(),
        # Typed into the web composer rather than a runtime session: nobody
        # live is listening for it, so the poller need not wait.
        'via': 'web' if msg.source_file == 'motion-web' else 'session',
        'text': text,
        'mentions': mentions_in(text, mentionable),
        'html': render_html(text, mentionable),
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


def activity(motion, now=None):
    """What an agent in this Motion is doing now, or None if nothing is underway.

    Read straight from the record, so no process has to report in: every
    thought, tool call and prompt streams into the Motion as it happens, and
    an assistant line whose stop_reason is end_turn closes the turn. So an
    agent is working from the first line after its last finished turn until
    the next one, and the newest line says what it's doing. A web post that
    names an agent and has nothing after it means the agent is being woken.
    """
    from conversations.models import ThinkingEntity
    import time

    now = now or time.time()
    agents = set(ThinkingEntity.objects.filter(is_biological_human=False).values_list('name', flat=True))
    # Helpers' lines (sidechains) are the agent's own call still running, so
    # they neither describe nor end its turn.
    recent = list(motion.messages.filter(is_sidechain=False)
                  .select_related('sender', 'tooluse', 'thought', 'toolresult')
                  .order_by('-created_at')[:RECENT])
    if not recent or now - _when(recent[0]) > ACTIVITY_WINDOW:
        return None

    newest = recent[0]
    if newest.sender_id in agents and newest.stop_reason in TURN_ENDS:
        return None
    # Rows imported before stop_reason was kept: a plain reply that has sat
    # for half a minute with nothing after it is taken as the end of a turn.
    if (newest.sender_id in agents and newest.stop_reason is None and now - _when(newest) > 30
            and not hasattr(newest, 'tooluse') and not hasattr(newest, 'thought')):
        return None

    if newest.source_file == 'motion-web':
        named = [n for n in mentions_in(prose(newest.content), agents)]
        if not named:
            return None
        return {'agent': named[0], 'doing': 'waking', 'since': _when(newest)}

    streak = []
    for msg in recent:
        if msg.sender_id in agents and msg.stop_reason in TURN_ENDS:
            break
        if msg.source_file == 'motion-web':
            break
        streak.append(msg)
    agent = next((m.sender_id for m in streak if m.sender_id in agents), None) or sorted(agents or {'magent'})[0]
    start = streak[-1]
    if len(streak) == len(recent) == RECENT:
        # A long turn runs past the rows read above: find where it began.
        from django.db.models import Q
        mine = motion.messages.filter(is_sidechain=False)
        boundary = (mine.filter(created_at__lt=start.created_at)
                    .filter(Q(sender_id__in=agents, stop_reason__in=TURN_ENDS) | Q(source_file='motion-web'))
                    .order_by('-created_at').first())
        if boundary is not None:
            start = mine.filter(created_at__gt=boundary.created_at).order_by('created_at').first() or start
    return {'agent': agent, 'doing': _doing(newest), 'since': _when(start)}


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
