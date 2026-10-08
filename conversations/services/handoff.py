"""Context taken from one Mood to another: an agent's handoff.

"Let's take this over to #jams-and-events" -- and the agent, in its reply
here, writes a block anywhere in what it says:

    ```handoff
    to: jams-and-events
    about: the Saturday jam's setlist, as far as we got
    for: magent
    ---
    What was settled, what's open, links to the messages that matter (#m-...).
    ```

`to` is the Mood (its name, or #name); `about`, a few words; `for`, the
agent it informs there (the writer, if not said); after `---`, the
context. It's taken out of the message where it's written (split), and
leaves two lines (events): there, "magent informed magent about ... from
#magenta-interface", folded to that line, the context beneath; here,
"magent took this to #jams-and-events". The agent's next wake in that Mood
carries it (the poller reads 'handoff' events), so whoever picks it up
there starts knowing.

No new way in: the block rides in what the agent says, which reaches its
Mood signed already (the runner's key, or the transcript importer) -- so a
handoff is only ever an agent's, into a Mood that exists, once per message.
"""

import re
import time
import uuid

SOURCE = 'handoff'
MAX_ABOUT = 100
MAX_CONTEXT = 8000
_BLOCK = re.compile(r'^```handoff[ \t]*\n(.*?)\n```[ \t]*$\n?', re.S | re.M)


def split(text):
    """(the text without its handoff blocks, [{'to', 'about', 'for', 'context'}, ...])."""
    blocks = [_parse(m.group(1)) for m in _BLOCK.finditer(text or '')]
    if not blocks:
        return text, []
    return re.sub(r'\n{3,}', '\n\n', _BLOCK.sub('\n\n', text)).strip(), [b for b in blocks if b]


def _parse(body):
    head, sep, context = body.partition('\n---\n')
    if not sep and body.startswith('---\n'):
        head, context = '', body[4:]
    fields = {}
    for line in head.splitlines():
        key, colon, value = line.partition(':')
        if colon and key.strip().lower() in ('to', 'about', 'for'):
            fields[key.strip().lower()] = value.strip()
    to = fields.get('to', '').lstrip('#').strip().lower()
    if not to:
        return None
    return {'to': to, 'about': fields.get('about', '')[:MAX_ABOUT], 'for': fields.get('for', '').lower(),
            'context': context.strip()[:MAX_CONTEXT]}


def from_message(message):
    """The handoffs an agent's message carries, made: an event in each Mood it
    goes to, and one here. Once per message; [] if it carries none (or isn't an agent's)."""
    from conversations.models import ConversationParticipant, Message, Mood, ThinkingEntity
    from .mood_view import prose
    if not message.mood_id or message.source_file == SOURCE:
        return []
    text = prose(message.content)
    if '```handoff' not in (text or ''):
        return []
    agents = set(ThinkingEntity.objects.filter(is_biological_human=False).values_list('name', flat=True))
    if message.sender_id not in agents:
        return []  # a person's message: a person can just say so
    if Message.objects.filter(source_file=SOURCE, content__message=str(message.id)).exists():
        return []
    _, blocks = split(text)
    here = message.mood
    system, _ = ConversationParticipant.objects.get_or_create(name='system', defaults={'participant_type': 'system'})
    made = []
    for block in blocks:
        there = Mood.by_slug(block['to'])
        if there is None or there.pk == here.pk:
            continue
        informed = block['for'] if block['for'] in agents else message.sender_id
        now = int(time.time() * 1000)
        made.append(Message.objects.create(
            id=uuid.uuid4(), sender=system, mood=there, source_file=SOURCE, timestamp=now,
            content={'type': 'handoff', 'by': message.sender_id, 'for': informed, 'from_mood': here.slug,
                     'about': block['about'], 'context': block['context'], 'message': str(message.id)}))
        Message.objects.create(
            id=uuid.uuid4(), sender=system, mood=here, source_file=SOURCE, timestamp=now,
            content={'type': 'handoff-sent', 'by': message.sender_id, 'for': informed, 'to_mood': there.slug,
                     'about': block['about'], 'message': str(message.id)})
    return made
