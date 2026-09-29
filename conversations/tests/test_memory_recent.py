"""bootstrap_memory's recent-context window survives big messages and ignores tool output."""

import uuid

from django.test import TestCase

from conversations.models import ConversationParticipant, Message, ThinkingEntity
from conversations.services.memory import MemoryService


class RecentByCharsTest(TestCase):

    def test_one_huge_message_no_longer_empties_the_window(self):
        justin = ThinkingEntity.objects.create(name='justin', is_biological_human=True)
        tool = ConversationParticipant.objects.create(name='tool-result', participant_type='tool')
        older = Message.objects.create(id=uuid.uuid4(), sender=justin, content='the thread so far')
        Message.objects.create(id=uuid.uuid4(), sender=justin, content='x' * 20_000)
        Message.objects.create(id=uuid.uuid4(), sender=tool, content='y' * 500)

        messages, total = MemoryService.get_recent_messages_by_chars(10_000)

        self.assertEqual(messages, [older])
        self.assertEqual(total, len('the thread so far'))
