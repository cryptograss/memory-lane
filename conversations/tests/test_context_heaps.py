"""
Tests for ContextHeap and CompactingAction creation.

Tests the polymorphic message structure where:
- Messages accumulate in a ContextHeap
- Parent chains link messages together
- CompactingAction marks when a heap was closed via compacting
"""

import uuid
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
    """Test context heap creation with messages and compacting actions."""

    def setUp(self):
        """Create thinking entities and era for tests."""
        self.justin = ThinkingEntity.objects.create(name='justin', is_biological_human=True)
        self.magent = ThinkingEntity.objects.create(name='magent', is_biological_human=False)
        self.era = Era.objects.create(name='Test Era')

    def test_create_heap_with_compacting_action(self):
        """Test creating a context heap that ended with a compact event."""

        session_id = uuid.uuid4()

        # First message
        msg1 = Message.objects.create(
            id='00000000-0000-0000-0000-000000000001',
            message_number=0,
            content="Let's build a memory system",
            sender=self.justin,
            timestamp=1726401600,
            session_id=session_id,
            source_file='test.jsonl'
        )
        msg1.recipients.add(self.magent)

        # Create heap with first message
        heap = ContextHeap.objects.create(
            era=self.era,
            type=ContextHeapType.FRESH
        )

        # Link message to heap
        msg1.context_heap = heap
        msg1.save()

        # Chain of messages
        msg2 = Message.objects.create(
            id='00000000-0000-0000-0000-000000000002',
            message_number=1,
            content="Great idea! Let's start.",
            sender=self.magent,
            timestamp=1726401660,
            session_id=session_id,
            parent=msg1,
            context_heap=heap
        )
        msg2.recipients.add(self.justin)

        msg3 = Message.objects.create(
            id='00000000-0000-0000-0000-000000000003',
            message_number=2,
            content="Show me the code",
            sender=self.justin,
            timestamp=1726401720,
            session_id=session_id,
            parent=msg2,
            context_heap=heap
        )
        msg3.recipients.add(self.magent)

        # Create CompactingAction to mark heap as compacted
        compacting = CompactingAction.objects.create(
            context_heap=heap,
            ending_message_id=msg3.id,
            compact_trigger='user_initiated',
            pre_compact_tokens=145000
        )

        # Verify heap structure
        self.assertEqual(str(heap.first_message().id), str(msg1.id))
        self.assertEqual(heap.type, ContextHeapType.FRESH)
        self.assertEqual(heap.era, self.era)

        # Verify message chain
        self.assertEqual(msg2.parent, msg1)
        self.assertEqual(msg2.context_heap, heap)
        self.assertEqual(msg3.parent, msg2)
        self.assertEqual(msg3.context_heap, heap)

        # Verify all messages in heap
        heap_messages = heap.messages.all().order_by('message_number')
        self.assertEqual(heap_messages.count(), 3)
        message_ids = [str(msg.id) for msg in heap_messages]
        self.assertIn(str(msg1.id), message_ids)
        self.assertIn(str(msg2.id), message_ids)
        self.assertIn(str(msg3.id), message_ids)

        # Verify compacting action
        self.assertEqual(heap.compacting_action.compact_trigger, 'user_initiated')
        self.assertEqual(heap.compacting_action.pre_compact_tokens, 145000)
        self.assertEqual(str(heap.compacting_action.ending_message_id), str(msg3.id))

        print("✓ Context heap with compacting test passed!")
        print(f"  Heap: {heap}")
        print(f"  Messages in heap: {heap_messages.count()}")
        print(f"  Compacting: {compacting}")

    def test_heap_without_compacting(self):
        """Test creating a context heap that just ended (no compact)."""

        session_id = uuid.uuid4()

        msg1 = Message.objects.create(
            id='00000000-0000-0000-0000-000000000005',
            message_number=0,
            content="Quick question",
            sender=self.justin,
            timestamp=1726405200,
            session_id=session_id
        )
        msg1.recipients.add(self.magent)

        heap = ContextHeap.objects.create(
            era=self.era,
            type=ContextHeapType.FRESH
        )

        msg1.context_heap = heap
        msg1.save()

        msg2 = Message.objects.create(
            id='00000000-0000-0000-0000-000000000006',
            message_number=1,
            content="Sure, what is it?",
            sender=self.magent,
            timestamp=1726405260,
            session_id=session_id,
            parent=msg1,
            context_heap=heap
        )
        msg2.recipients.add(self.justin)

        # Verify no compacting action
        self.assertFalse(hasattr(heap, 'compacting_action'))

        # Verify messages exist
        heap_messages = heap.messages.all()
        self.assertEqual(heap_messages.count(), 2)

        print("✓ Context heap without compacting test passed!")
        print(f"  Heap: {heap}")
        print(f"  Messages: {heap_messages.count()}")

    def test_post_compacting_heap(self):
        """Test creating a POST_COMPACTING heap after a compact."""

        session_id = uuid.uuid4()

        # Create a FRESH heap that ends with compact
        msg1_pre = Message.objects.create(
            id='00000000-0000-0000-0000-000000000010',
            message_number=0,
            content="Starting conversation",
            sender=self.justin,
            timestamp=1726400000,
            session_id=session_id
        )
        msg1_pre.recipients.add(self.magent)

        heap_fresh = ContextHeap.objects.create(
            era=self.era,
            type=ContextHeapType.FRESH
        )

        msg1_pre.context_heap = heap_fresh
        msg1_pre.save()

        # Create compacting action
        CompactingAction.objects.create(
            context_heap=heap_fresh,
            ending_message_id=msg1_pre.id,
            compact_trigger='user_initiated'
        )

        # Create POST_COMPACTING heap
        msg1_post = Message.objects.create(
            id='00000000-0000-0000-0000-000000000011',
            message_number=0,  # Renumbered from 0
            content="This session is being continued...",
            sender=self.magent,
            timestamp=1726400100,
            session_id=session_id
        )
        msg1_post.recipients.add(self.justin)

        heap_post = ContextHeap.objects.create(
            era=self.era,
            type=ContextHeapType.POST_COMPACTING
        )

        msg1_post.context_heap = heap_post
        msg1_post.save()

        # Verify heap types
        self.assertEqual(heap_fresh.type, ContextHeapType.FRESH)
        self.assertEqual(heap_post.type, ContextHeapType.POST_COMPACTING)

        # Verify fresh heap has compacting action
        self.assertTrue(hasattr(heap_fresh, 'compacting_action'))
        self.assertEqual(str(heap_fresh.compacting_action.ending_message_id), str(msg1_pre.id))

        # Verify post-compact heap has no compacting (yet)
        self.assertFalse(hasattr(heap_post, 'compacting_action'))

        print("✓ POST_COMPACTING heap test passed!")
        print(f"  Fresh heap: {heap_fresh}")
        print(f"  Post-compact heap: {heap_post}")


if __name__ == '__main__':
    import django
    import sys
    from pathlib import Path

    # Setup Django
    project_root = Path(__file__).resolve().parent.parent.parent
    sys.path.insert(0, str(project_root))

    import os
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'magenta.settings')
    django.setup()

    # Run tests
    from django.test.utils import get_runner
    from django.conf import settings

    TestRunner = get_runner(settings)
    test_runner = TestRunner(verbosity=2, interactive=False, keepdb=False)
    failures = test_runner.run_tests(["conversations.tests.test_context_heaps"])

    sys.exit(bool(failures))
