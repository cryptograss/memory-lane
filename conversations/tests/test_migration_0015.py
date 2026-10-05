"""0015 renames the poller's participant everywhere it appears, recipients included.

The first version updated a `recipient_id` column that doesn't exist --
recipients are many-to-many -- and failed on prod, where the poller has
been addressed. Tests had passed only because no test database held one.
"""

import uuid

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

BEFORE = [('conversations', '0014_moods_replace_motions')]
AFTER = [('conversations', '0015_mood_names_in_the_record')]


class RenameThePollerTest(TransactionTestCase):

    def migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(targets)
        return executor.loader.project_state(targets).apps

    def tearDown(self):
        self.migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())

    def test_sender_recipients_and_sources_follow_the_rename_and_back(self):
        apps = self.migrate(BEFORE)
        Participant = apps.get_model('conversations', 'ConversationParticipant')
        Message = apps.get_model('conversations', 'Message')
        poller = Participant.objects.create(name='motion-poller', participant_type='system')
        justin = Participant.objects.create(name='justin', participant_type='human')
        asked = Message.objects.create(id=uuid.uuid4(), sender=poller, content='wake', source_file='motion-web')
        told = Message.objects.create(id=uuid.uuid4(), sender=justin, content='@magent hi')
        told.recipients.add(poller)

        apps = self.migrate(AFTER)
        Participant = apps.get_model('conversations', 'ConversationParticipant')
        Message = apps.get_model('conversations', 'Message')
        self.assertFalse(Participant.objects.filter(name='motion-poller').exists())
        self.assertEqual(Message.objects.get(id=asked.id).sender_id, 'mood-poller')
        self.assertEqual(Message.objects.get(id=asked.id).source_file, 'mood-web')
        self.assertEqual(list(Message.objects.get(id=told.id).recipients.values_list('name', flat=True)),
                         ['mood-poller'])

        apps = self.migrate(BEFORE)
        Message = apps.get_model('conversations', 'Message')
        self.assertEqual(Message.objects.get(id=asked.id).sender_id, 'motion-poller')
        self.assertEqual(list(Message.objects.get(id=told.id).recipients.values_list('name', flat=True)),
                         ['motion-poller'])
