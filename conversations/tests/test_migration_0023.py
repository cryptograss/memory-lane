"""0023 gives back to the harness the task notices filed under a person."""

import uuid

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

BEFORE = [('conversations', '0022_yarn_stills')]
AFTER = [('conversations', '0023_task_notices_are_not_people')]


class TaskNoticesAreNotPeopleTest(TransactionTestCase):

    def migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(targets)
        return executor.loader.project_state(targets).apps

    def tearDown(self):
        self.migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())

    def test_a_notice_filed_under_a_person_becomes_systems_and_their_words_stay_theirs(self):
        apps = self.migrate(BEFORE)
        Entity = apps.get_model('conversations', 'ThinkingEntity')
        Message = apps.get_model('conversations', 'Message')
        justin = Entity.objects.create(name='justin', is_biological_human=True)
        notice = Message.objects.create(id=uuid.uuid4(), sender=justin,
                                        content='<task-notification>\n<status>failed</status>\n</task-notification>')
        words = Message.objects.create(id=uuid.uuid4(), sender=justin, content='what is a <task-notification>?')

        apps = self.migrate(AFTER)
        Message = apps.get_model('conversations', 'Message')
        self.assertEqual(Message.objects.get(id=notice.id).sender_id, 'system')
        self.assertEqual(Message.objects.get(id=words.id).sender_id, 'justin')
