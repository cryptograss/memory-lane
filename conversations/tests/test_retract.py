"""Taking back what was said: deleted for real, edited without history, the record's copies scrubbed too."""

import json
import uuid
from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from conversations.models import Media, Message, MessageChange, Mood, ThinkingEntity
from conversations.services import media, mood_auth, sealing

SECRET = 'ghp_0123456789abcdefghijklmnopqrstuvwxyzAB'  # the shape the redactor knows, so pasted as a picture's caption below
PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 64


PRIVATE, PUBLIC = sealing.new_pair()


@override_settings(MOOD_ADMINS=('justin',), MOOD_RECOVERY_PUBLIC_KEY=PUBLIC)
class RetractTest(TestCase):

    def setUp(self):
        cache.clear()
        self.justin = ThinkingEntity.objects.create(name='justin', is_biological_human=True)
        self.skyler = ThinkingEntity.objects.create(name='skyler', is_biological_human=True)
        self.magent = ThinkingEntity.objects.create(name='magent', is_biological_human=False)
        self.poller = ThinkingEntity.objects.create(name='mood-poller', is_biological_human=False)
        self.mood = Mood.objects.create(slug='general', title='general')

    def client_for(self, who, tier='key'):
        _, token = mood_auth.enrol_device(who, 'laptop', tier=tier)
        client = Client()
        client.cookies[mood_auth.COOKIE] = token
        return client

    def post(self, who, text, source='mood-web'):
        return Message.objects.create(id=uuid.uuid4(), sender=who, mood=self.mood, timestamp=1, content=text,
                                      source_file=source)

    def act(self, client, message, what, body=None):
        return client.post(f'/api/moods/general/messages/{message.id}/{what}/', json.dumps(body or {}),
                           content_type='application/json')

    def turn(self, message, **params):
        page = Client().get('/api/moods/general/turns/', params).json()
        return next((t for t in page['turns'] if t['id'] == str(message.id)), None), page

    def test_deleted_its_words_leave_the_record_and_a_line_stays(self):
        oops = self.post(self.skyler, 'the deploy key is hunter2-sky-blue-banjo, use it carefully')
        done = self.act(self.client_for(self.skyler, 'wiki'), oops, 'delete').json()
        self.assertEqual((done['kind'], done['reached']), ('deleted', []))
        oops.refresh_from_db()
        self.assertNotIn('hunter2', json.dumps(oops.content))
        turn, _ = self.turn(oops)
        self.assertEqual((turn['deleted']['by'], turn['text'], turn['html']), ('skyler', '', ''))
        self.assertEqual(MessageChange.objects.get(message=oops).kind, 'deleted')

    def test_the_wakes_that_quoted_it_are_scrubbed_and_who_they_reached_is_said(self):
        oops = self.post(self.skyler, 'the deploy key is hunter2-sky-blue-banjo')
        session = uuid.uuid4()
        wake = Message.objects.create(id=uuid.uuid4(), sender=self.poller, mood=self.mood, timestamp=2, session_id=session,
                                      content=[{'type': 'text', 'text': '<mood-wake>\n[skyler, now] the deploy key is hunter2-sky-blue-banjo\n</mood-wake>'}])
        Message.objects.create(id=uuid.uuid4(), sender=self.magent, mood=self.mood, timestamp=3, session_id=session,
                               content=[{'type': 'text', 'text': 'Noted.'}])
        done = self.act(self.client_for(self.skyler), oops, 'delete').json()
        wake.refresh_from_db()
        self.assertNotIn('hunter2', json.dumps(wake.content))
        self.assertIn('[deleted]', wake.content[0]['text'])
        self.assertEqual((done['scrubbed'], done['reached']), (1, ['magent']))

    def test_its_picture_and_any_reading_aloud_go_with_it_unless_used_elsewhere(self):
        alone, shared = media.store(PNG + b'\x01'), media.store(PNG + b'\x02')
        oops = self.post(self.justin, f'oops ![image]({alone.url}) and ![image]({shared.url})')
        self.post(self.justin, f'this one stays ![image]({shared.url})')
        reading = media.store(b'ID3' + b'\x00' * 64, audio=True)
        Message.objects.create(id=uuid.uuid4(), sender=self.poller, mood=self.mood, timestamp=1, source_file='voice',
                               content={'type': 'spoken', 'message': str(oops.id), 'media': reading.sha256})
        self.act(self.client_for(self.justin), oops, 'delete')
        self.assertFalse(Media.objects.filter(sha256=alone.sha256).exists())
        self.assertTrue(Media.objects.filter(sha256=shared.sha256).exists())
        self.assertFalse(Media.objects.filter(sha256=reading.sha256).exists())
        self.assertFalse(Message.objects.filter(source_file='voice', content__message=str(oops.id)).exists())

    def test_whose_to_delete(self):
        theirs = self.post(self.skyler, 'mine to take back')
        agents = Message.objects.create(id=uuid.uuid4(), sender=self.magent, mood=self.mood, timestamp=1,
                                        content=[{'type': 'text', 'text': 'I quoted the key: hunter2-sky'}])
        self.assertEqual(self.act(Client(), theirs, 'delete').status_code, 401)
        self.assertEqual(self.act(self.client_for(self.justin, 'wiki'), theirs, 'delete').status_code, 403)  # an admin, not by key
        self.assertEqual(self.act(self.client_for(self.justin), agents, 'delete').status_code, 200)  # an admin by key: an agent's too
        agents.refresh_from_db()
        self.assertEqual(agents.content, [{'type': 'text', 'text': '[deleted]'}])

    def test_edited_without_history_and_the_old_words_scrubbed(self):
        mine = self.post(self.justin, 'line one\nthe token is ' + 'pa55word-for-the-jam-site')
        wake = Message.objects.create(id=uuid.uuid4(), sender=self.poller, mood=self.mood, timestamp=2,
                                      content='[justin, now] line one\nthe token is pa55word-for-the-jam-site')
        client = self.client_for(self.justin)
        self.assertEqual(self.act(client, mine, 'edit', {'text': 'line one\n(gone)'}).status_code, 200)
        mine.refresh_from_db()
        wake.refresh_from_db()
        self.assertEqual(mine.content, 'line one\n(gone)')
        self.assertEqual(wake.content, '[justin, now] line one\n[edited]')
        turn, _ = self.turn(mine)
        self.assertTrue(turn['edited'])
        self.act(client, mine, 'edit', {'text': f'and {SECRET}'})
        mine.refresh_from_db()
        self.assertNotIn(SECRET, mine.content)  # the redactor's patterns, as when posting

    def test_whose_to_edit(self):
        theirs = self.post(self.skyler, 'skyler said this')
        signed = self.post(self.justin, 'signed', source='mood-attest')
        justin = self.client_for(self.justin)
        self.assertEqual(self.act(justin, theirs, 'edit', {'text': 'no'}).status_code, 403)  # not even an admin
        self.assertEqual(self.act(justin, signed, 'edit', {'text': 'no'}).status_code, 403)
        mine = self.post(self.justin, 'hi')
        self.assertEqual(self.act(justin, mine, 'edit', {'text': '  '}).status_code, 400)
        wiki = self.client_for(self.skyler, 'wiki')
        self.assertEqual(self.act(wiki, theirs, 'edit', {'text': '@magent wake up'}).status_code, 403)

    def test_whats_changed_since_the_page_last_asked(self):
        mine = self.post(self.justin, 'before')
        since = (timezone.now() - timedelta(seconds=1)).isoformat()
        self.act(self.client_for(self.justin), mine, 'edit', {'text': 'after'})
        _, page = self.turn(mine, changes_since=since)
        [change] = page['changes']
        self.assertEqual((change['id'], change['kind'], change['turn']['text'], bool(change['turn']['edited'])),
                         (str(mine.id), 'edited', 'after', True))
        self.assertTrue(page['now'])
        _, later = self.turn(mine, changes_since=page['now'])
        self.assertEqual(later['changes'], [])


@override_settings(MOOD_ADMINS=('justin',), MOOD_RECOVERY_PUBLIC_KEY=PUBLIC)
class GuardedTest(TestCase):
    """Taken back, never lost: sealed first; within a window; a few at a time; said aloud when it's someone else's."""

    def setUp(self):
        cache.clear()
        self.justin = ThinkingEntity.objects.create(name='justin', is_biological_human=True)
        self.skyler = ThinkingEntity.objects.create(name='skyler', is_biological_human=True)
        self.mood = Mood.objects.create(slug='general', title='general')
        self.justin_key, self.skyler_wiki = self.client_for(self.justin), self.client_for(self.skyler, 'wiki')

    def client_for(self, who, tier='key'):
        _, token = mood_auth.enrol_device(who, 'laptop', tier=tier)
        client = Client()
        client.cookies[mood_auth.COOKIE] = token
        return client

    def post(self, who, text, days_ago=0):
        message = Message.objects.create(id=uuid.uuid4(), sender=who, mood=self.mood, timestamp=1, content=text,
                                         source_file='mood-web')
        if days_ago:
            Message.objects.filter(pk=message.pk).update(created_at=timezone.now() - timedelta(days=days_ago))
            message.refresh_from_db()
        return message

    def act(self, client, message, what, body=None):
        return client.post(f'/api/moods/general/messages/{message.id}/{what}/', json.dumps(body or {}),
                           content_type='application/json')

    def test_sealed_before_it_goes_unreadable_here_and_put_back_with_the_key(self):
        from io import StringIO
        from django.core.management import call_command
        from conversations.models import SealedCopy
        picture = media.store(PNG + b'\x07', added_by=self.skyler)
        oops = self.post(self.skyler, f'the key is hunter2-sky-blue-banjo ![image]({picture.url})')
        self.assertEqual(self.act(self.skyler_wiki, oops, 'delete').status_code, 200)
        copy = SealedCopy.objects.get(message_id=oops.id)
        self.assertEqual((copy.by, copy.kind), ('skyler', 'deleted'))
        self.assertNotIn('hunter2', copy.sealed)
        self.assertFalse(Media.objects.filter(sha256=picture.sha256).exists())
        with mock.patch('sys.stdin', StringIO(PRIVATE + '\n')):
            call_command('unseal', str(oops.id), '--restore', stdout=StringIO())
        oops.refresh_from_db()
        self.assertIn('hunter2-sky-blue-banjo', oops.content)
        self.assertTrue(Media.objects.filter(sha256=picture.sha256).exists())
        self.assertEqual(MessageChange.objects.get(message=oops).kind, 'restored')
        with self.assertRaises(sealing.SealingError):
            sealing.unseal(copy.sealed, sealing.new_pair()[0])  # another key opens nothing

    def test_put_back_as_it_was_before_the_first_takeback_and_all_of_someones_at_once(self):
        from io import StringIO
        from django.core.management import call_command
        first = self.post(self.skyler, 'the original words')
        second = self.post(self.skyler, 'another of hers')
        thief = self.client_for(self.skyler)  # her account, captured
        self.act(thief, first, 'edit', {'text': 'A'})
        self.act(thief, first, 'edit', {'text': 'B'})
        self.act(thief, second, 'delete')
        listed = StringIO()
        call_command('unseal', '--list', '--by', 'skyler', stdout=listed)
        self.assertEqual(len(listed.getvalue().strip().splitlines()), 3)
        with mock.patch('sys.stdin', StringIO(PRIVATE + '\n')):
            call_command('unseal', '--restore', '--by', 'skyler', '--since', '2000-01-01', stdout=StringIO())
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual((first.content, second.content), ('the original words', 'another of hers'))  # not 'A'

    def test_a_chosen_copy(self):
        from io import StringIO
        from django.core.management import call_command
        from conversations.models import SealedCopy
        mine = self.post(self.justin, 'one')
        self.act(self.justin_key, mine, 'edit', {'text': 'two'})
        self.act(self.justin_key, mine, 'edit', {'text': 'three'})
        later = SealedCopy.objects.filter(message_id=mine.id).order_by('at', 'id').last()
        with mock.patch('sys.stdin', StringIO(PRIVATE + '\n')):
            call_command('unseal', str(mine.id), '--restore', '--copy', str(later.id), stdout=StringIO())
        mine.refresh_from_db()
        self.assertEqual(mine.content, 'two')

    def test_without_a_recovery_key_nothing_can_be_taken_back(self):
        mine = self.post(self.justin, 'stays')
        with override_settings(MOOD_RECOVERY_PUBLIC_KEY=''):
            self.assertEqual(self.act(self.justin_key, mine, 'delete').status_code, 503)
            self.assertEqual(self.act(self.justin_key, mine, 'edit', {'text': 'no'}).status_code, 503)
        mine.refresh_from_db()
        self.assertEqual(mine.content, 'stays')

    def test_your_own_within_a_window_older_only_an_admin(self):
        old_one = self.post(self.skyler, 'last month', days_ago=30)
        yesterday = self.post(self.skyler, 'yesterday', days_ago=2)
        self.assertEqual(self.act(self.skyler_wiki, old_one, 'delete').status_code, 403)
        self.assertEqual(self.act(self.skyler_wiki, yesterday, 'edit', {'text': 'changed'}).status_code, 403)
        self.assertEqual(self.act(self.skyler_wiki, yesterday, 'delete').status_code, 200)
        self.assertEqual(self.act(self.justin_key, old_one, 'delete').status_code, 200)

    def test_a_few_at_a_time_even_for_an_admin(self):
        from conversations.services import retract
        made = [self.post(self.skyler, f'message {n}') for n in range(retract.PER_HOUR + 1)]
        codes = [self.act(self.justin_key, m, 'delete').status_code for m in made]
        self.assertEqual(codes, [200] * retract.PER_HOUR + [429])
        made[-1].refresh_from_db()
        self.assertEqual(made[-1].content, f'message {retract.PER_HOUR}')

    def test_someone_elses_words_or_a_burst_said_in_general(self):
        from conversations.services import retract
        for n in range(retract.BURST):
            self.act(self.justin_key, self.post(self.skyler, f'm{n}'), 'delete')
        said = [m.content for m in Message.objects.filter(source_file='access', mood=self.mood).order_by('created_at')]
        took = [c for c in said if c['kind'] == 'took-back']
        self.assertEqual((len(took), took[0]['who'], took[0]['by']), (retract.BURST, 'skyler', 'justin'))
        self.assertEqual([c['who'] for c in said if c['kind'] == 'taking-back-a-lot'], ['justin'])  # once a burst
