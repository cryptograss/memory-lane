"""Emoji reactions to what was said in a Mood: a 👍 instead of a line.

Anyone signed in may react to any message -- a person's, an agent's -- with
any emoji, and take it back by pressing it again. Each reaction is a system
row in the Mood (source 'reaction': who, which message, which emoji), kept
out of the thread like the voice's records; the page asks for a Mood's
reactions with every look it takes (/turns/), so everyone sees them as they
change. Nobody is notified of one: a reaction is a nod, not a message.
"""

import time
import uuid

SOURCE = 'reaction'
PALETTE = ['👍', '❤️', '😂', '🎉', '🙏', '👀', '🔥', '💯', '🪕', '🎻']
MAX_EMOJI = 16  # characters: an emoji with its modifiers and joiners, never words


def valid(emoji):
    """An emoji, not words or markup: non-ASCII throughout, short."""
    return bool(emoji) and len(emoji) <= MAX_EMOJI and all(ord(c) > 127 for c in emoji) and not emoji.isspace()


def toggle(message, by, emoji):
    """React to a message, or take that reaction back. True if it's on now."""
    from conversations.models import ConversationParticipant, Message
    mine = Message.objects.filter(source_file=SOURCE, mood=message.mood, content__message=str(message.id),
                                  content__by=by, content__emoji=emoji)
    if mine.exists():
        mine.delete()
        return False
    system, _ = ConversationParticipant.objects.get_or_create(name='system', defaults={'participant_type': 'system'})
    Message.objects.create(id=uuid.uuid4(), sender=system, mood=message.mood, source_file=SOURCE,
                           timestamp=int(time.time() * 1000),
                           content={'type': 'reaction', 'message': str(message.id), 'by': by, 'emoji': emoji})
    return True


def in_mood(mood, message_ids=None):
    """{message id: {emoji: [who, ...]}}, emoji in the order first used, people in the order they reacted."""
    from conversations.models import Message
    rows = Message.objects.filter(source_file=SOURCE, mood=mood)
    if message_ids is not None:
        rows = rows.filter(content__message__in=[str(i) for i in message_ids])
    out = {}
    for c in rows.order_by('created_at').values_list('content', flat=True):
        if isinstance(c, dict) and c.get('message') and c.get('emoji'):
            people = out.setdefault(c['message'], {}).setdefault(c['emoji'], [])
            if c.get('by') not in people:
                people.append(c.get('by'))
    return out
