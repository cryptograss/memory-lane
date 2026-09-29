"""The ingest endpoint refuses without a key (#12) and skips session metadata quietly (#13)."""

import json
import os
from unittest import mock

from constant_sorrow.constants import EVENT_TYPE_WE_DO_NOT_HANDLE_YET
from django.test import TestCase

from conversations.models import Message

KEY = 'k' * 64

# Records newer Claude Code clients write alongside the conversation.
# None has a `message` key; each used to raise KeyError('message').
METADATA_LINES = [
    {'type': 'custom-title', 'customTitle': 'magenta-26-million', 'sessionId': 's'},
    {'type': 'agent-name', 'agentName': 'magent', 'sessionId': 's'},
    {'type': 'attachment', 'uuid': '00000000-0000-0000-0000-00000000000a', 'attachment': {}},
    {'type': 'pr-link', 'url': 'https://github.com/jMyles/memory-lane/pull/14'},
    {'type': 'queue-operation', 'operation': 'enqueue'},
    {'type': 'last-prompt', 'lastPrompt': 'hello'},
]


class MetadataDetectionTest(TestCase):

    def test_metadata_records_are_not_messages(self):
        for record in METADATA_LINES:
            with self.subTest(type=record['type']):
                event_type, _ = Message.detect_event_type_claude_code_v2(json.dumps(record))
                self.assertIs(event_type, EVENT_TYPE_WE_DO_NOT_HANDLE_YET)


class IngestGuardTest(TestCase):

    def post(self, lines, key=KEY, env_key=KEY):
        env = {'INGEST_API_KEY': env_key} if env_key else {}
        headers = {'HTTP_AUTHORIZATION': f'Bearer {key}'} if key else {}
        with mock.patch.dict(os.environ, env, clear=False):
            if not env_key:
                os.environ.pop('INGEST_API_KEY', None)
            return self.client.post(
                '/api/ingest/', data=json.dumps({'lines': lines, 'source': 'test'}),
                content_type='application/json', **headers,
            )

    def test_unset_key_refuses_everything(self):
        response = self.post(['{}'], env_key=None)
        self.assertEqual(response.status_code, 503)

    def test_unset_key_refuses_even_without_a_header(self):
        response = self.post(['{}'], key=None, env_key=None)
        self.assertEqual(response.status_code, 503)

    def test_wrong_key_is_unauthorized(self):
        response = self.post(['{}'], key='nope')
        self.assertEqual(response.status_code, 401)

    def test_non_ascii_key_is_unauthorized_not_a_crash(self):
        response = self.post(['{}'], key='clé')
        self.assertEqual(response.status_code, 401)

    def test_missing_header_is_unauthorized(self):
        response = self.post(['{}'], key=None)
        self.assertEqual(response.status_code, 401)

    def test_metadata_batch_is_skipped_without_errors(self):
        response = self.post([json.dumps(record) for record in METADATA_LINES])
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['errors'], [])
        self.assertEqual(body['skipped'], len(METADATA_LINES))
        self.assertEqual(body['imported'], 0)
