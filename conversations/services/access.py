"""Comings and goings, said in #general: every new sign-in, and every admin action.

A line in the thread, like a redeploy's ('🔑 skyler signed in with their SSH
key · "laptop"'), from a system row (source 'access'). The room sees who has
come in and how -- a sign-in nobody expected, on someone's account, is just
what it should see -- and when an admin has acted. Ordinary sign-outs aren't
announced. Announcing never stands in the way of what it announces.
"""

import logging
import time
import uuid

SOURCE = 'access'
ROOM = 'general'

logger = logging.getLogger(__name__)


def announce(kind, who, **details):
    """A line in #general; None if there's no #general, or it couldn't be written."""
    from conversations.models import ConversationParticipant, Message, Mood
    try:
        mood = Mood.by_slug(ROOM)
        if mood is None:
            return None
        system, _ = ConversationParticipant.objects.get_or_create(name='system', defaults={'participant_type': 'system'})
        content = {'type': 'access', 'kind': kind, 'who': who,
                   **{k: v for k, v in details.items() if v not in (None, '')}}
        return Message.objects.create(id=uuid.uuid4(), sender=system, mood=mood, content=content,
                                      timestamp=int(time.time() * 1000), source_file=SOURCE)
    except Exception as e:  # a sign-in or a kick mustn't fail for want of its announcement
        logger.warning(f'could not announce {kind} for {who}: {e}')
        return None


def signed_in(device):
    return announce('signed-in', device.entity_id, tier=device.tier, label=device.label)
