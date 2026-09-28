"""Tests for the read-only Motion view: rendering, filtering, and the API."""

import json
import uuid
from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone

from conversations.models import ConversationParticipant, Message, Motion, ThinkingEntity
from conversations.services.motion_view import prose, render_html, turns


class RenderHtmlTest(TestCase):

    def test_escapes_markup_before_anything_else(self):
        out = render_html('<script>alert(1)</script> & done')
        self.assertNotIn('<script>', out)
        self.assertIn('&lt;script&gt;', out)
        self.assertIn('&amp; done', out)

    def test_inline_markdown(self):
        out = render_html('say **bold** and *soft* and `code`')
        self.assertIn('<strong>bold</strong>', out)
        self.assertIn('<em>soft</em>', out)
        self.assertIn('<code>code</code>', out)

    @override_settings(PICKIPEDIA_URL='https://pickipedia.xyz')
    def test_wikilinks_resolve_to_pickipedia(self):
        out = render_html('see [[Tony Rice]] and [[Kuba Hejhal|Kuba]]')
        self.assertIn('href="https://pickipedia.xyz/wiki/Tony_Rice"', out)
        self.assertIn('>Tony Rice</a>', out)
        self.assertIn('href="https://pickipedia.xyz/wiki/Kuba_Hejhal"', out)
        self.assertIn('>Kuba</a>', out)

    def test_lists_and_paragraphs(self):
        out = render_html('intro\n\n- one\n- two\n\n1. first\n2. second\n\nouter')
        self.assertIn('<p>intro</p>', out)
        self.assertIn('<ul><li>one</li><li>two</li></ul>', out)
        self.assertIn('<ol><li>first</li><li>second</li></ol>', out)
        self.assertIn('<p>outer</p>', out)

    def test_fenced_code_is_left_alone(self):
        out = render_html('before\n\n```\n**not bold** [[not a link]]\n```\n\nafter')
        self.assertIn('<pre><code>**not bold** [[not a link]]</code></pre>', out)
        self.assertNotIn('<strong>', out)
        self.assertNotIn('wikilink', out)

    def test_headings_become_small(self):
        self.assertIn('<h4>Where we are</h4>', render_html('## Where we are'))

    def test_bare_urls_link(self):
        out = render_html('at https://example.com/x?a=1&b=2 now')
        self.assertIn('<a href="https://example.com/x?a=1&amp;b=2">', out)


class ProseAndTurnsTest(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.justin = ThinkingEntity.objects.create(name="justin", is_biological_human=True)
        cls.magent = ThinkingEntity.objects.create(name="magent", is_biological_human=False)
        cls.tool = ConversationParticipant.objects.create(name="tool-result")
        cls.system = ConversationParticipant.objects.create(name="system")
        cls.motion = Motion.objects.create(slug="m", title="M")

        script = [
            (cls.justin, "hello there"),
            (cls.magent, {"command": "ls", "description": "list"}),          # tool call
            (cls.tool, "file1\nfile2"),                                         # tool result
            (cls.magent, [{"type": "thinking", "thinking": "hmm"}]),           # thinking
            (cls.magent, [{"type": "text", "text": "reply **one**"}]),
            (cls.system, "System notice"),
            (cls.justin, "<command-name>/clear</command-name>"),               # wrapper
            (cls.magent, [{"text": "untyped block"}]),
        ]
        base = timezone.now() - timedelta(minutes=10)
        cls.ids = []
        for i, (sender, content) in enumerate(script):
            m = Message.objects.create(id=uuid.uuid4(), sender=sender, content=content, motion=cls.motion)
            Message.objects.filter(id=m.id).update(created_at=base + timedelta(seconds=i))
            cls.ids.append(m.id)

    def test_prose_shapes(self):
        self.assertEqual(prose("plain"), "plain")
        self.assertEqual(prose({"command": "ls"}), "")
        self.assertEqual(prose([{"type": "text", "text": "a"}, {"type": "tool_use"}]), "a")
        self.assertEqual(prose([{"type": "thinking", "thinking": "x"}]), "")
        self.assertEqual(prose(None), "")

    def test_turns_keep_only_readable_conversation(self):
        texts = [t for _, t in turns(self.motion)]
        self.assertEqual(texts, ["hello there", "reply **one**", "untyped block"])

    def test_turns_after(self):
        first = Message.objects.get(id=self.ids[0])
        texts = [t for _, t in turns(self.motion, after=first)]
        self.assertEqual(texts, ["reply **one**", "untyped block"])


class MotionApiTest(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.justin = ThinkingEntity.objects.create(name="justin", is_biological_human=True)
        cls.magent = ThinkingEntity.objects.create(name="magent", is_biological_human=False)
        cls.old = Motion.objects.create(slug="old-motion", title="Old")
        cls.new = Motion.objects.create(slug="new-motion", title="New", eth_blockheight=26_071_421)
        base = timezone.now() - timedelta(hours=1)
        for i, (motion, sender, text) in enumerate([
            (cls.old, cls.justin, "older"),
            (cls.new, cls.justin, "hi [[Tony Rice]]"),
            (cls.new, cls.magent, [{"type": "text", "text": "hello **you**"}]),
        ]):
            m = Message.objects.create(id=uuid.uuid4(), sender=sender, content=text, motion=motion)
            Message.objects.filter(id=m.id).update(created_at=base + timedelta(minutes=i))

    def test_list_is_most_recent_first(self):
        data = self.client.get('/api/motions/').json()
        self.assertEqual([m['slug'] for m in data['motions']], ['new-motion', 'old-motion'])
        new = data['motions'][0]
        self.assertEqual(new['participants'], ['justin', 'magent'])
        self.assertEqual(new['message_count'], 2)
        self.assertEqual(new['eth_blockheight'], 26_071_421)

    def test_turns_render_and_attribute(self):
        data = self.client.get('/api/motions/new-motion/turns/').json()
        self.assertEqual(data['motion']['slug'], 'new-motion')
        senders = [(t['sender'], t['is_human']) for t in data['turns']]
        self.assertEqual(senders, [('justin', True), ('magent', False)])
        self.assertIn('wikilink', data['turns'][0]['html'])
        self.assertIn('<strong>you</strong>', data['turns'][1]['html'])

    def test_turns_after_and_unknown_after_recovers(self):
        first = self.client.get('/api/motions/new-motion/turns/').json()['turns']
        since = self.client.get(f"/api/motions/new-motion/turns/?after={first[0]['id']}").json()['turns']
        self.assertEqual([t['sender'] for t in since], ['magent'])
        stale = self.client.get(f"/api/motions/new-motion/turns/?after={uuid.uuid4()}").json()['turns']
        self.assertEqual(len(stale), 2)

    def test_unknown_motion_is_404(self):
        self.assertEqual(self.client.get('/api/motions/nope/turns/').status_code, 404)
        self.assertEqual(self.client.get('/motions/nope/').status_code, 404)

    def test_page_renders(self):
        r = self.client.get('/motions/new-motion/')
        self.assertEqual(r.status_code, 200)
        # escapejs renders the hyphen as -; the browser decodes it.
        self.assertContains(r, 'const initialSlug = "new\\u002Dmotion"')
        self.assertEqual(self.client.get('/motions/').status_code, 200)

    def test_read_only(self):
        self.assertEqual(self.client.post('/api/motions/').status_code, 405)
        self.assertEqual(self.client.post('/api/motions/new-motion/turns/').status_code, 405)
