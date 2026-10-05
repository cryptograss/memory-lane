"""Tests for Moods: the subject a conversation belongs to."""

import json
import tempfile
import uuid
from io import StringIO

from django.core.management import CommandError, call_command
from django.test import TestCase

from conversations.models import (
    ContextHeap, ContextHeapType, ConversationParticipant, Era, Message,
    Mood, ThinkingEntity,
)


class MoodModelTest(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.justin = ThinkingEntity.objects.create(name="justin", is_biological_human=True)
        cls.magent = ThinkingEntity.objects.create(name="magent", is_biological_human=False)
        cls.tool = ConversationParticipant.objects.create(name="tool-result")

        cls.era = Era.objects.create(name="Test Era")
        cls.heap_a = ContextHeap.objects.create(era=cls.era, type=ContextHeapType.FRESH)
        cls.heap_b = ContextHeap.objects.create(era=cls.era, type=ContextHeapType.POST_COMPACTING)

        cls.mood = Mood.objects.create(slug="delivery-kid", title="Delivery Kid")

        # One Mood spanning two heaps: heaps mark where context filled up,
        # the Mood marks what the conversation was about.
        for heap, sender, block in (
            (cls.heap_a, cls.justin, 25_900_000),
            (cls.heap_a, cls.magent, 25_900_010),
            (cls.heap_b, cls.magent, 25_990_000),
            (cls.heap_b, cls.tool, None),
        ):
            Message.objects.create(
                id=uuid.uuid4(), sender=sender, content="x",
                context_heap=heap, mood=cls.mood, eth_blockheight=block,
            )

        # Unattached message, from before Moods existed.
        Message.objects.create(id=uuid.uuid4(), sender=cls.justin, content="older")

    def test_mood_spans_context_heaps(self):
        heaps = {m.context_heap_id for m in self.mood.messages.all()}
        self.assertEqual(heaps, {self.heap_a.id, self.heap_b.id})

    def test_messages_may_have_no_mood(self):
        self.assertEqual(Message.objects.filter(mood__isnull=True).count(), 1)

    def test_blockheight_span(self):
        self.assertEqual(self.mood.earliest_blockheight(), 25_900_000)
        self.assertEqual(self.mood.latest_blockheight(), 25_990_000)

    def test_thinking_entities_excludes_tools(self):
        names = sorted(e.name for e in self.mood.thinking_entities())
        self.assertEqual(names, ["justin", "magent"])

    def test_retiring_a_mood_keeps_its_messages(self):
        self.mood.delete()
        self.assertEqual(Message.objects.count(), 5)
        self.assertEqual(Message.objects.filter(mood__isnull=True).count(), 5)

    def test_str_prefers_title(self):
        self.assertEqual(str(self.mood), "Delivery Kid")
        self.assertEqual(str(Mood.objects.create(slug="untitled")), "untitled")


class MoodAssignCommandTest(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.justin = ThinkingEntity.objects.create(name="justin", is_biological_human=True)
        cls.session_one = uuid.uuid4()
        cls.session_two = uuid.uuid4()
        for session in (cls.session_one, cls.session_two):
            for _ in range(3):
                Message.objects.create(
                    id=uuid.uuid4(), sender=cls.justin, content="x", session_id=session,
                )

    def run_command(self, *args, **kwargs):
        out = StringIO()
        call_command('mood_assign', *args, stdout=out, **kwargs)
        return out.getvalue()

    def test_opens_mood_and_attaches_several_sessions(self):
        # A Mood outlives any one session, so it takes more than one.
        self.run_command('paseo', '--session', str(self.session_one),
                         '--session', str(self.session_two), '--title', 'Paseo')
        mood = Mood.objects.get(slug='paseo')
        self.assertEqual(mood.title, 'Paseo')
        self.assertEqual(mood.messages.count(), 6)

    def test_dry_run_writes_nothing(self):
        output = self.run_command('paseo', '--session', str(self.session_one), dry_run=True)
        self.assertIn('would attach 3', output)
        self.assertFalse(Mood.objects.filter(slug='paseo').exists())
        self.assertEqual(Message.objects.filter(mood__isnull=False).count(), 0)

    def test_rerunning_is_idempotent(self):
        self.run_command('paseo', '--session', str(self.session_one))
        output = self.run_command('paseo', '--session', str(self.session_one))
        self.assertIn('already attached', output)
        self.assertEqual(Mood.objects.get(slug='paseo').messages.count(), 3)

    def test_will_not_steal_from_another_mood_without_reassign(self):
        self.run_command('paseo', '--session', str(self.session_one))
        self.run_command('moods', '--session', str(self.session_one))
        self.assertEqual(Mood.objects.get(slug='paseo').messages.count(), 3)
        self.assertEqual(Mood.objects.get(slug='moods').messages.count(), 0)

        self.run_command('moods', '--session', str(self.session_one), '--reassign')
        self.assertEqual(Mood.objects.get(slug='moods').messages.count(), 3)
        self.assertEqual(Mood.objects.get(slug='paseo').messages.count(), 0)

    def test_unknown_session_is_an_error(self):
        with self.assertRaises(CommandError):
            self.run_command('paseo', '--session', str(uuid.uuid4()))

    def transcript(self, *records):
        path = tempfile.NamedTemporaryFile('w', suffix='.jsonl', delete=False)
        path.write('\n'.join(json.dumps(r) for r in records) + '\nnot json\n')
        path.close()
        return path.name

    def test_jsonl_restores_lost_sessions_and_attaches(self):
        # The early part of a long session was stored with no session id at all.
        session = uuid.uuid4()
        lost = [Message.objects.create(id=uuid.uuid4(), sender=self.justin, content="x") for _ in range(2)]
        kept = Message.objects.filter(session_id=self.session_one).first()
        path = self.transcript(*[{'uuid': str(m.id), 'sessionId': str(session), 'type': 'user'} for m in lost],
                               {'uuid': str(kept.id), 'sessionId': str(session), 'type': 'user'},
                               {'uuid': str(uuid.uuid4()), 'sessionId': str(session), 'type': 'user'},
                               {'type': 'custom-title', 'sessionId': str(session)})

        output = self.run_command('m26', '--jsonl', path)

        mood = Mood.objects.get(slug='m26')
        self.assertEqual(mood.messages.count(), 3)
        for m in lost:
            m.refresh_from_db()
            self.assertEqual(m.session_id, session)
        kept.refresh_from_db()
        self.assertEqual(kept.session_id, self.session_one)  # an existing session is never changed
        self.assertIn('3 messages (2 had lost their session)', output)

    def test_jsonl_dry_run_and_rerun(self):
        lost = Message.objects.create(id=uuid.uuid4(), sender=self.justin, content="x")
        path = self.transcript({'uuid': str(lost.id), 'sessionId': str(uuid.uuid4())})

        self.assertIn('would attach 1', self.run_command('m26', '--jsonl', path, dry_run=True))
        lost.refresh_from_db()
        self.assertIsNone(lost.session_id)

        self.run_command('m26', '--jsonl', path)
        self.assertIn('attached 0', self.run_command('m26', '--jsonl', path))

    def test_jsonl_with_no_messages_is_an_error(self):
        with self.assertRaises(CommandError):
            self.run_command('m26', '--jsonl', self.transcript({'type': 'custom-title'}))


class RenamingTest(TestCase):
    """Renamed, a Mood gets the slug its title calls for; the old one still finds it."""

    def test_a_new_title_a_new_slug_and_the_old_one_an_alias(self):
        from conversations.models import Mood, MoodAlias
        mood = Mood.objects.create(slug='delivery-kid', title='delivery-kid')
        other = Mood.objects.create(slug='general', title='#general')
        mood.rename('Uploads and embeds')
        self.assertEqual(mood.slug, 'uploads-and-embeds')
        self.assertEqual(Mood.by_slug('delivery-kid'), mood)  # its runner, its old links
        self.assertEqual(Mood.by_slug('uploads-and-embeds'), mood)
        # Nobody else may take the old name...
        self.assertEqual(Mood.free_slug('delivery kid'), 'delivery-kid-2')
        self.assertEqual(Mood.free_slug('#general'), 'general-2')
        # ...but it may have it back.
        mood.rename('delivery-kid')
        self.assertEqual(mood.slug, 'delivery-kid')
        self.assertEqual(sorted(MoodAlias.objects.values_list('slug', flat=True)), ['uploads-and-embeds'])
        self.assertIsNone(Mood.by_slug('nowhere'))
        self.assertEqual(other.slug, 'general')

    def test_the_page_and_the_runner_follow_a_rename(self):
        from conversations.models import Mood
        mood = Mood.objects.create(slug='delivery-kid', title='delivery-kid')
        mood.rename('Uploads and embeds')
        self.assertRedirects(self.client.get('/moods/delivery-kid/'), '/moods/uploads-and-embeds/',
                             fetch_redirect_response=False)
        pulse = {m['slug']: m for m in self.client.get('/api/moods/pulse/').json()['moods']}
        self.assertEqual(pulse['uploads-and-embeds']['aliases'], ['delivery-kid'])
        from poller.mood_poller import MoodPoller
        poller = MoodPoller.__new__(MoodPoller)
        poller.moods, poller.known_as = {'delivery-kid'}, {'uploads-and-embeds': {'delivery-kid'}}
        self.assertTrue(poller.mine('uploads-and-embeds'))  # a container told the old name answers it still
        self.assertFalse(poller.mine('general'))
