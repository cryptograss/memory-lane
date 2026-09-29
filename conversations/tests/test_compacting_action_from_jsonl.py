"""
Tests for importing Claude Code v2 compact_boundary and summary lines.

compact_boundary lines become CompactingActions; summary lines become Summaries.
"""

import json
import unittest
import uuid
from django.test import TestCase
from conversations.models import (
    CompactingAction, ThinkingEntity, Era, ContextHeap, ContextHeapType, Message, Summary
)
from importers_and_parsers.claude_code_v2 import import_line_from_claude_code_v2


def compact_boundary_record(logical_parent_uuid, trigger='manual', pre_tokens=145000):
    return {
        'uuid': str(uuid.uuid4()),
        'parentUuid': None,
        'logicalParentUuid': str(logical_parent_uuid),
        'type': 'system',
        'subtype': 'compact_boundary',
        'content': 'Conversation compacted',
        'userType': 'external',
        'sessionId': str(uuid.uuid4()),
        'timestamp': '2025-10-15T14:30:00.000Z',
        'compactMetadata': {'trigger': trigger, 'preTokens': pre_tokens}
    }


def summary_record(leaf_uuid, summary='Discussion about memory systems and database design'):
    return {
        'type': 'summary',
        'summary': summary,
        'leafUuid': str(leaf_uuid)
    }


class ImportTestCase(TestCase):

    def setUp(self):
        """Create test entities and context."""
        self.justin = ThinkingEntity.objects.create(name='justin', is_biological_human=True)
        self.magent = ThinkingEntity.objects.create(name='magent', is_biological_human=False)

        self.era = Era.objects.create(name='Test Era')

    def import_record(self, record):
        return import_line_from_claude_code_v2(json.dumps(record), self.era, 'test.jsonl')

    def message_in_heap(self):
        message = Message.objects.create(id=uuid.uuid4(), content='Last message before compact', sender=self.justin)
        message.recipients.add(self.magent)
        heap = ContextHeap.objects.create(era=self.era, type=ContextHeapType.FRESH)
        heap.add_event(message)
        return message, heap


class CompactingActionFromJsonlTests(ImportTestCase):
    """Test compact_boundary import deduplication and instantiation."""

    def test_creates_new_compacting_action(self):
        """Creating a new CompactingAction returns (action, True)."""
        compact, created = self.import_record(compact_boundary_record(uuid.uuid4()))

        self.assertTrue(created)
        self.assertIsInstance(compact, CompactingAction)
        self.assertEqual(compact.compact_trigger, 'manual')
        self.assertEqual(compact.pre_compact_tokens, 145000)

    def test_reimport_returns_existing_compacting_action(self):
        """Importing the same boundary twice returns the same CompactingAction."""
        record = compact_boundary_record('00000000-0000-0000-0000-000000000001')

        compact1, created1 = self.import_record(record)
        compact2, created2 = self.import_record(record)

        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(compact1.id, compact2.id)
        self.assertEqual(CompactingAction.objects.count(), 1)

    def test_different_boundaries_get_different_compacting_actions(self):
        """Boundaries for different ending messages create different CompactingActions."""
        compact1, _ = self.import_record(compact_boundary_record('00000000-0000-0000-0000-000000000001'))
        compact2, _ = self.import_record(compact_boundary_record('00000000-0000-0000-0000-000000000002'))

        self.assertNotEqual(compact1.id, compact2.id)

    def test_allows_orphaned_compacting_action(self):
        """A boundary whose ending message isn't imported yet creates an orphaned CompactingAction."""
        ending_msg_id = uuid.uuid4()

        compact, created = self.import_record(compact_boundary_record(ending_msg_id))

        self.assertTrue(created)
        self.assertIsNone(compact.context_heap)
        self.assertIsNone(compact.ending_message)
        self.assertEqual(compact.looking_for_ending_message, ending_msg_id)

    def test_links_existing_ending_message_and_heap(self):
        """A boundary whose ending message exists links to it and its heap."""
        ending_msg, heap = self.message_in_heap()

        compact, created = self.import_record(compact_boundary_record(ending_msg.id))

        self.assertTrue(created)
        self.assertEqual(compact.ending_message_id, ending_msg.id)
        self.assertEqual(compact.context_heap, heap)
        self.assertIsNone(compact.looking_for_ending_message)

    def test_deduplication_preserves_original(self):
        """Re-importing a boundary returns the original and doesn't update it."""
        ending_msg_id = uuid.uuid4()

        compact1, created1 = self.import_record(compact_boundary_record(ending_msg_id, trigger='manual'))
        compact2, created2 = self.import_record(compact_boundary_record(ending_msg_id, trigger='auto'))

        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(compact1.id, compact2.id)
        self.assertEqual(CompactingAction.objects.get(id=compact1.id).compact_trigger, 'manual')

    # get_or_create_by_id_or_message sets context_heap on the orphan but leaves
    # it out of save(update_fields=...), so the heap link is never written.
    @unittest.expectedFailure
    def test_orphan_gets_heap_when_reimported_after_ending_message(self):
        """An orphaned CompactingAction linked on re-import is saved with its heap."""
        ending_msg_id = uuid.uuid4()
        record = compact_boundary_record(ending_msg_id)
        compact, _ = self.import_record(record)

        ending_msg = Message.objects.create(id=ending_msg_id, content='Late arrival', sender=self.justin)
        heap = ContextHeap.objects.create(era=self.era, type=ContextHeapType.FRESH)
        heap.add_event(ending_msg)
        self.import_record(record)

        compact.refresh_from_db()
        self.assertEqual(compact.ending_message_id, ending_msg_id)
        self.assertEqual(compact.context_heap, heap)


class SummaryFromJsonlTests(ImportTestCase):
    """Test summary import deduplication and instantiation."""

    def test_creates_summary_for_unimported_leaf(self):
        """A summary whose leaf message isn't imported yet is stored looking for it."""
        leaf_uuid = uuid.uuid4()

        summary, created = self.import_record(summary_record(leaf_uuid))

        self.assertTrue(created)
        self.assertIsInstance(summary, Summary)
        self.assertEqual(summary.summary_text, 'Discussion about memory systems and database design')
        self.assertIsNone(summary.leaf_message)
        self.assertEqual(summary.looking_for_leaf_message, leaf_uuid)

    def test_links_summary_to_existing_leaf(self):
        """A summary whose leaf message exists links to it."""
        leaf_msg, _ = self.message_in_heap()

        summary, created = self.import_record(summary_record(leaf_msg.id))

        self.assertTrue(created)
        self.assertEqual(summary.leaf_message_id, leaf_msg.id)
        self.assertIsNone(summary.looking_for_leaf_message)

    def test_deduplication_preserves_original(self):
        """Re-importing a summary for the same leaf returns the original, doesn't update."""
        leaf_uuid = uuid.uuid4()

        summary1, created1 = self.import_record(summary_record(leaf_uuid, 'Original summary'))
        summary2, created2 = self.import_record(summary_record(leaf_uuid, 'Regenerated summary'))

        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(summary1.id, summary2.id)
        self.assertEqual(Summary.objects.get(id=summary1.id).summary_text, 'Original summary')

    # handle_summary looks up by leaf_message first once the leaf exists, misses
    # the orphan (stored under looking_for_leaf_message), and creates a second row.
    @unittest.expectedFailure
    def test_orphan_is_not_duplicated_when_reimported_after_leaf(self):
        """Re-importing an orphaned summary after its leaf arrives links the original."""
        leaf_uuid = uuid.uuid4()
        record = summary_record(leaf_uuid)
        summary1, _ = self.import_record(record)

        Message.objects.create(id=leaf_uuid, content='Late arrival', sender=self.justin)
        summary2, created2 = self.import_record(record)

        self.assertFalse(created2)
        self.assertEqual(summary1.id, summary2.id)
        self.assertEqual(Summary.objects.count(), 1)
        self.assertEqual(Summary.objects.get().leaf_message_id, leaf_uuid)
