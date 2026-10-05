"""0016: Moods renamed before renaming moved the URL get the slug their title gives now."""

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

BEFORE = [('conversations', '0015_mood_names_in_the_record')]
AFTER = [('conversations', '0016_slugs_follow_titles')]


class SlugsFollowTitlesTest(TransactionTestCase):

    def migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(targets)
        return executor.loader.project_state(targets).apps

    def tearDown(self):
        self.migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())

    def test_old_slugs_become_aliases_and_taken_names_are_avoided(self):
        apps = self.migrate(BEFORE)
        Mood = apps.get_model('conversations', 'Mood')
        MoodAlias = apps.get_model('conversations', 'MoodAlias')
        interface = Mood.objects.create(slug='magenta-26-million', title='magenta-interface')
        general = Mood.objects.create(slug='general', title='general')
        untitled = Mood.objects.create(slug='scratch', title='')
        holder = Mood.objects.create(slug='holder', title='holder')
        MoodAlias.objects.create(slug='uploads', mood=holder)  # a name someone else once had
        uploads = Mood.objects.create(slug='delivery-kid', title='Uploads')

        apps = self.migrate(AFTER)
        Mood = apps.get_model('conversations', 'Mood')
        MoodAlias = apps.get_model('conversations', 'MoodAlias')
        slug = lambda m: Mood.objects.get(pk=m.pk).slug
        self.assertEqual(slug(interface), 'magenta-interface')
        self.assertEqual(slug(general), 'general')
        self.assertEqual(slug(untitled), 'scratch')
        self.assertEqual(slug(uploads), 'uploads-2')  # 'uploads' was another Mood's name
        aliases = dict(MoodAlias.objects.values_list('slug', 'mood_id'))
        self.assertEqual(aliases['magenta-26-million'], interface.pk)
        self.assertEqual(aliases['delivery-kid'], uploads.pk)
        self.assertNotIn('general', aliases)

        self.migrate(BEFORE)  # and back, leaving the new slugs: the old ones still find them
