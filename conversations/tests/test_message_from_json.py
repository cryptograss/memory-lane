"""
Tests for importing Claude Code v2 message lines via import_line_from_claude_code_v2().
"""

import json
import unittest
import uuid
from django.test import TestCase, override_settings
from conversations.models import (
    Message, ThinkingEntity, Era, Thought, ToolUse, ToolResult
)
from importers_and_parsers.claude_code_v2 import import_line_from_claude_code_v2


def jsonl_record(role, content, **overrides):
    """A minimal Claude Code v2 user/assistant event."""
    record = {
        'uuid': str(uuid.uuid4()),
        'parentUuid': None,
        'type': role,
        'userType': 'external',
        'sessionId': str(uuid.uuid4()),
        'timestamp': '2025-10-15T14:30:00.000Z',
        'message': {
            'role': role,
            'content': content
        }
    }
    record.update(overrides)
    return record


class MessageFromJsonTests(TestCase):
    """Test import_line_from_claude_code_v2() deduplication and instantiation."""

    def setUp(self):
        """Create test entities and era."""
        self.justin = ThinkingEntity.objects.create(name='justin', is_biological_human=True)
        self.magent = ThinkingEntity.objects.create(name='magent', is_biological_human=False)

        self.era = Era.objects.create(name='Test Era')

    def import_record(self, record):
        return import_line_from_claude_code_v2(json.dumps(record), self.era, 'test.jsonl')

    def test_creates_new_message_from_json(self):
        """Creating a new message returns (message, True)."""
        json_data = jsonl_record(
            'user',
            [
                {
                    'type': 'text',
                    'text': 'Hello, this is a test message'
                }
            ],
            cwd='/home/test',
            gitBranch='main',
            version='1.0.0',
            isSidechain=False
        )

        message, created = self.import_record(json_data)

        self.assertTrue(created)
        self.assertEqual(message.id, uuid.UUID(json_data['uuid']))
        self.assertEqual(message.content, json_data['message']['content'])
        self.assertEqual(message.sender.name, 'justin')
        self.assertEqual(list(message.recipients.values_list('name', flat=True)), ['magent'])
        self.assertEqual(str(message.session_id), json_data['sessionId'])
        self.assertEqual(message.cwd, '/home/test')
        self.assertEqual(message.git_branch, 'main')
        self.assertEqual(message.client_version, '1.0.0')
        self.assertFalse(message.is_sidechain)

    def test_deduplicates_existing_message(self):
        """Importing a line whose UUID already exists returns (existing_message, False)."""
        msg_uuid = uuid.uuid4()
        session_uuid = uuid.uuid4()

        original = Message.objects.create(
            id=msg_uuid,
            content='Original message',
            sender=self.justin,
            session_id=session_uuid
        )
        original.recipients.add(self.magent)

        json_data = jsonl_record(
            'user',
            [{'type': 'text', 'text': 'Different content'}],
            uuid=str(msg_uuid),
            sessionId=str(session_uuid)
        )

        message, created = self.import_record(json_data)

        self.assertFalse(created)
        self.assertEqual(message.id, original.id)
        self.assertEqual(message.content, 'Original message')  # Keeps original content
        self.assertEqual(Message.objects.get(id=msg_uuid).content, 'Original message')

    def test_handles_string_content(self):
        """Handles content as plain string instead of array."""
        json_data = jsonl_record('user', 'Plain string content')

        message, created = self.import_record(json_data)

        self.assertTrue(created)
        self.assertEqual(message.content, 'Plain string content')
        self.assertEqual(message.sender.name, 'justin')

    # detect_event_type_claude_code_v2 reads content[0] without checking for an
    # empty list, so this line raises IndexError instead of being stored.
    @unittest.expectedFailure
    def test_handles_empty_content(self):
        """An empty content array is stored rather than aborting the import."""
        json_data = jsonl_record('user', [])

        message, created = self.import_record(json_data)

        self.assertTrue(created)
        self.assertEqual(message.id, uuid.UUID(json_data['uuid']))

    def test_parses_timestamp_correctly(self):
        """Converts ISO timestamp to milliseconds since epoch."""
        json_data = jsonl_record('user', 'Test', timestamp='2025-10-15T14:30:45.123Z')

        message, created = self.import_record(json_data)

        self.assertTrue(created)
        self.assertEqual(message.timestamp, 1760538645123)

    def test_handles_missing_optional_fields(self):
        """Creates message successfully with minimal JSON data."""
        json_data = {
            'uuid': str(uuid.uuid4()),
            'parentUuid': None,
            'type': 'user',
            'message': {
                'role': 'user',
                'content': 'Minimal message'
            }
        }

        message, created = self.import_record(json_data)

        self.assertTrue(created)
        self.assertIsNone(message.timestamp)
        self.assertIsNone(message.session_id)
        self.assertIsNone(message.cwd)
        self.assertFalse(message.is_sidechain)

    def test_creates_thought_from_assistant_thinking(self):
        """Assistant message with a thinking block creates a Thought."""
        content = [
            {
                'type': 'thinking',
                'thinking': 'Let me think about this problem...',
                'signature': 'sig-abc123'
            }
        ]
        json_data = jsonl_record('assistant', content)

        thought, created = self.import_record(json_data)

        self.assertTrue(created)
        self.assertIsInstance(thought, Thought)
        self.assertEqual(thought.content, content)
        self.assertEqual(thought.signature, 'sig-abc123')
        self.assertEqual(thought.sender.name, 'magent')
        self.assertEqual(list(thought.recipients.values_list('name', flat=True)), ['magent'])

    def test_creates_tool_use_from_assistant_tool_call(self):
        """Assistant message with tool_use creates a ToolUse addressed to the tool."""
        json_data = jsonl_record('assistant', [
            {
                'type': 'tool_use',
                'id': 'toolu_01ABC123',
                'name': 'Read',
                'input': {'file_path': '/test/file.txt'}
            }
        ])

        tool_use, created = self.import_record(json_data)

        self.assertTrue(created)
        self.assertIsInstance(tool_use, ToolUse)
        self.assertEqual(tool_use.tool_name, 'Read')
        self.assertEqual(tool_use.tool_id, 'toolu_01ABC123')
        self.assertEqual(tool_use.content, {'file_path': '/test/file.txt'})
        self.assertEqual(tool_use.sender.name, 'magent')
        self.assertEqual(list(tool_use.recipients.values_list('name', flat=True)), ['Read'])

    def test_keeps_thinking_preamble_of_assistant_response(self):
        """Assistant message with thinking + text keeps both in one Message."""
        json_data = jsonl_record('assistant', [
            {'type': 'thinking', 'thinking': 'I need to read this file'},
            {'type': 'text', 'text': 'Let me check that file for you'}
        ])

        message, created = self.import_record(json_data)

        self.assertTrue(created)
        self.assertEqual(message.content, {
            'text': 'Let me check that file for you',
            'preamble': {'thinking': ['I need to read this file']}
        })
        self.assertEqual(message.sender.name, 'magent')
        self.assertEqual(list(message.recipients.values_list('name', flat=True)), ['justin'])

    @override_settings(TOOL_RESULT_CONTENT_CHARS=10_000)
    def test_creates_tool_result_from_user_message(self):
        """User message with tool_result creates ToolResult object."""
        json_data = jsonl_record(
            'user',
            [
                {
                    'type': 'tool_result',
                    'tool_use_id': 'toolu_01ABC123',
                    'content': 'File contents here',
                    'is_error': False
                }
            ],
            toolUseResult={'stdout': 'File contents here', 'stderr': '', 'interrupted': False}
        )

        message, created = self.import_record(json_data)

        self.assertTrue(created)
        self.assertIsInstance(message, ToolResult)
        self.assertEqual(message.tool_use_id, 'toolu_01ABC123')
        self.assertEqual(message.content, 'File contents here')
        self.assertFalse(message.is_error)

    @override_settings(TOOL_RESULT_CONTENT_CHARS=10_000)
    def test_tool_result_list_content_keeps_text_and_marks_the_rest(self):
        json_data = jsonl_record('user', [{
            'type': 'tool_result', 'tool_use_id': 'toolu_img', 'is_error': True,
            'content': [{'type': 'text', 'text': 'screenshot saved'},
                        {'type': 'image', 'source': {'type': 'base64', 'data': 'AAAA'}}],
        }])

        message, _ = self.import_record(json_data)

        self.assertEqual(message.content, 'screenshot saved\n[image omitted]')
        self.assertTrue(message.is_error)

    def tool_result_record(self, content='restored output'):
        return jsonl_record('user', [{'type': 'tool_result', 'tool_use_id': 'toolu_fill',
                                      'content': content, 'is_error': False}])

    @override_settings(TOOL_RESULT_CONTENT_CHARS=10_000)
    def test_reimport_fills_a_tool_result_the_old_importer_left_empty(self):
        record = self.tool_result_record()
        ToolResult.objects.create(id=record['uuid'], sender=self.magent, content='', tool_use_id='')

        message, created = self.import_record(record)

        self.assertFalse(created)
        message = ToolResult.objects.get(id=record['uuid'])
        self.assertEqual(message.content, 'restored output')
        self.assertEqual(message.tool_use_id, 'toolu_fill')

    @override_settings(TOOL_RESULT_CONTENT_CHARS=10_000)
    def test_reimport_never_overwrites_a_populated_tool_result(self):
        record = self.tool_result_record(content='different')
        ToolResult.objects.create(id=record['uuid'], sender=self.magent,
                                  content='original', tool_use_id='toolu_fill')

        self.import_record(record)

        self.assertEqual(ToolResult.objects.get(id=record['uuid']).content, 'original')

    def test_by_default_a_tool_result_keeps_its_link_but_no_content(self):
        message, _ = self.import_record(self.tool_result_record(content='SECRET=hunter2'))
        self.assertEqual(message.tool_use_id, 'toolu_fill')
        self.assertEqual(message.content, '')

    @override_settings(TOOL_RESULT_CONTENT_CHARS=5)
    def test_content_beyond_the_limit_is_cut_and_says_so(self):
        message, _ = self.import_record(self.tool_result_record(content='0123456789'))
        self.assertEqual(message.content, '01234\n[5 more characters not kept]')

    @override_settings(TOOL_RESULT_CONTENT_CHARS=10_000)
    def test_null_bytes_and_tool_references(self):
        record = jsonl_record('user', [{'type': 'tool_result', 'tool_use_id': 'toolu_ref', 'content': [
            {'type': 'text', 'text': 'a\x00b'}, {'type': 'tool_reference', 'tool_name': 'WebFetch'}, 'stray']}])
        message, _ = self.import_record(record)
        self.assertEqual(message.content, 'ab\n[tool_reference: WebFetch]\nstray')

    def test_raising_the_limit_later_lets_a_replay_add_content(self):
        record = self.tool_result_record()
        self.import_record(record)
        with self.settings(TOOL_RESULT_CONTENT_CHARS=10_000):
            self.import_record(record)
        self.assertEqual(ToolResult.objects.get(id=record['uuid']).content, 'restored output')

    def test_preserves_original_uuid_for_tool_use_only_message(self):
        """Messages with ONLY tool_use (no text) preserve original UUID."""
        msg_uuid = uuid.uuid4()
        json_data = jsonl_record(
            'assistant',
            [
                {
                    'type': 'tool_use',
                    'id': 'toolu_test123',
                    'name': 'Bash',
                    'input': {'command': 'ls -la'}
                }
            ],
            uuid=str(msg_uuid),
            timestamp='2025-10-15T16:00:00.000Z'
        )

        tool_use, created = self.import_record(json_data)

        self.assertTrue(created)
        self.assertEqual(tool_use.id, msg_uuid)
        self.assertTrue(hasattr(Message.objects.get(id=msg_uuid), 'tooluse'))

    def test_preserves_original_uuid_for_thinking_only_message(self):
        """Messages with ONLY thinking (no text) preserve original UUID."""
        msg_uuid = uuid.uuid4()
        json_data = jsonl_record(
            'assistant',
            [
                {
                    'type': 'thinking',
                    'thinking': 'Let me consider this carefully...',
                    'signature': 'sig-abc123'
                }
            ],
            uuid=str(msg_uuid),
            timestamp='2025-10-15T16:00:00.000Z'
        )

        thought, created = self.import_record(json_data)

        self.assertTrue(created)
        self.assertEqual(thought.id, msg_uuid)
        self.assertTrue(hasattr(Message.objects.get(id=msg_uuid), 'thought'))

    def test_preserves_original_uuid_for_mixed_content_message(self):
        """Messages with thinking + text + tool_use preserve original UUID and every block."""
        msg_uuid = uuid.uuid4()
        json_data = jsonl_record(
            'assistant',
            [
                {'type': 'thinking', 'thinking': 'I should run this command...'},
                {'type': 'text', 'text': 'Running command to check directory'},
                {'type': 'tool_use', 'id': 'toolu_abc', 'name': 'Bash', 'input': {'command': 'pwd'}}
            ],
            uuid=str(msg_uuid),
            timestamp='2025-10-15T16:00:00.000Z'
        )

        tool_use, created = self.import_record(json_data)

        self.assertTrue(created)
        self.assertEqual(tool_use.id, msg_uuid)
        self.assertEqual(tool_use.content, {
            'tool_input': {'command': 'pwd'},
            'preamble': {
                'thinking': ['I should run this command...'],
                'text': ['Running command to check directory']
            }
        })
