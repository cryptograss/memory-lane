"""
Tests for ContextHeap and CompactingAction creation.

Tests the polymorphic message structure where:
- Messages accumulate in a ContextHeap
- Parent chains link messages together
- CompactingAction marks when a heap was closed via compacting
"""

import json
import tempfile
import uuid
from pathlib import Path
from django.test import TestCase
from conversations.models import (
    ThinkingEntity,
    Era,
    ContextHeap,
    ContextHeapType,
    Message,
    CompactingAction
)


class ContextHeapTestCase(TestCase):
    """Test context window creation with new polymorphic structure."""

    def setUp(self):
        """Create thinking entities for tests."""
        self.justin = ThinkingEntity.objects.create(name='justin', is_biological_human=True)
        self.magent = ThinkingEntity.objects.create(name='magent', is_biological_human=False)

    def test_create_context_with_compacting_action(self):
        """Test creating a context window that ended with a compact event."""

        # Create Era
        era = Era.objects.create(name='Test Era')

        # First message opens the context
        opener = Message.objects.create(
            id='00000000-0000-0000-0000-000000000001',
            content="Let's build a memory system",
            sender=self.justin,
            timestamp=1726401600,
            session_id=uuid.uuid4(),
            source_file='test.jsonl'
        )
        opener.recipients.add(self.magent)

        # Create ContextHeap for the opener
        heap = ContextHeap.objects.create(
            era=era,
            type=ContextHeapType.FRESH
        )

        # Set opener's context_heap
        opener.context_heap = heap
        opener.save()

        # Chain of regular messages
        session_id = opener.session_id

        msg2 = Message.objects.create(
            id='00000000-0000-0000-0000-000000000002',
            content="Great idea! Let's start.",
            sender=self.magent,
            timestamp=1726401660,
            session_id=session_id,
            parent=opener,
            context_heap=heap
        )
        msg2.recipients.add(self.justin)

        msg3 = Message.objects.create(
            id='00000000-0000-0000-0000-000000000003',
            content="Show me the code",
            sender=self.justin,
            timestamp=1726401720,
            session_id=session_id,
            parent=msg2,
            context_heap=heap
        )
        msg3.recipients.add(self.magent)

        # Create CompactingAction to mark context as closed
        compacting = CompactingAction.objects.create(
            context_heap=heap,
            ending_message_id='00000000-0000-0000-0000-000000000003',
            compact_trigger='manual',
            pre_compact_tokens=145000
        )

        # Verify structure
        self.assertEqual(opener.sender.name, 'justin')
        self.assertIn(self.magent.name, opener.recipients.values_list('name', flat=True))
        self.assertEqual(opener.session_id, session_id)

        # Verify message chain
        self.assertEqual(msg2.parent, opener)
        self.assertEqual(msg2.context_heap, heap)
        self.assertEqual(msg3.parent, msg2)
        self.assertEqual(msg3.context_heap, heap)

        # Verify all messages in heap
        heap_messages = heap.messages.all()
        self.assertEqual(heap_messages.count(), 3)  # opener, msg2, msg3
        heap_message_ids = [str(msg.id) for msg in heap_messages]
        self.assertIn(str(opener.id), heap_message_ids)
        self.assertIn(str(msg2.id), heap_message_ids)
        self.assertIn(str(msg3.id), heap_message_ids)

        # Verify compacting action
        self.assertEqual(heap.compacting_action.compact_trigger, 'manual')
        self.assertEqual(heap.compacting_action.pre_compact_tokens, 145000)
        self.assertEqual(str(heap.compacting_action.ending_message_id), str(msg3.id))

        print("✓ Context window with compacting test passed!")
        print(f"  Heap: {heap}")
        print(f"  Messages in heap: {heap_messages.count()}")
        print(f"  Compacting: {compacting}")

    def test_context_without_compacting(self):
        """Test creating a context window that just ended (no compact)."""

        # Create Era
        era = Era.objects.create(name='Test Era 2')

        opener = Message.objects.create(
            id='00000000-0000-0000-0000-000000000005',
            content="Quick question",
            sender=self.justin,
            timestamp=1726405200,
            session_id=uuid.uuid4()
        )
        opener.recipients.add(self.magent)

        # Create ContextHeap
        heap = ContextHeap.objects.create(
            era=era,
            type=ContextHeapType.FRESH
        )

        opener.context_heap = heap
        opener.save()

        msg2 = Message.objects.create(
            id='00000000-0000-0000-0000-000000000006',
            content="Sure, what is it?",
            sender=self.magent,
            timestamp=1726405260,
            session_id=opener.session_id,
            parent=opener,
            context_heap=heap
        )
        msg2.recipients.add(self.justin)

        # Verify context works without compacting action
        self.assertEqual(heap.messages.count(), 2)
        self.assertFalse(hasattr(heap, 'compacting_action'))

        print("✓ Non-compacted context test passed!")
        print(f"  Heap: {heap}")
        print(f"  Has compacting action: {hasattr(heap, 'compacting_action')}")

    def test_multiple_recipients(self):
        """Test message with multiple recipients."""

        rj = ThinkingEntity.objects.create(name='rj', is_biological_human=True)

        opener = Message.objects.create(
            id='00000000-0000-0000-0000-000000000007',
            content="Hey team, let's collaborate",
            sender=self.justin,
            timestamp=1726408800,
            session_id=uuid.uuid4()
        )
        opener.recipients.add(self.magent, rj)

        # Verify multiple recipients
        self.assertEqual(opener.recipients.count(), 2)
        recipient_names = opener.recipients.values_list('name', flat=True)
        self.assertIn(self.magent.name, recipient_names)
        self.assertIn(rj.name, recipient_names)

        print("✓ Multiple recipients test passed!")
        print(f"  Recipients: {[r.name for r in opener.recipients.all()]}")


if __name__ == '__main__':
    import django
    django.setup()
    from django.test.utils import get_runner
    from django.conf import settings

    TestRunner = get_runner(settings)
    test_runner = TestRunner()
    failures = test_runner.run_tests(["conversations.tests.test_context_heaps"])
