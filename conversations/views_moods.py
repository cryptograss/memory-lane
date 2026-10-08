"""Read-only public view of Moods.

Served by memory-lane because memory-lane is where the record lives. This
is a projection: nothing here writes, and nothing a reader does can change
the conversation.
"""

import functools
import hashlib

from django.conf import settings
from django.db.models import Max
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import ensure_csrf_cookie
from django.core.exceptions import ValidationError
from django.views.decorators.http import require_GET, require_POST

from .models import Message, Mood, ThinkingEntity, mood_slug
from .services import push, wiki_auth, wiki_feed, wiki_upload
from .services import mood_auth
from .services.mood_view import (
    from_wiki_tier,
    MACHINERY_SENDERS, how_payload, activity, background_tasks, is_wrapper, known_names, mentions_in, addressed_in, mood_payload,
    prose, render_html, step_detail, step_images, step_payload, timeline, turn_payload, turns, wiki_title, wikilinks_in,
)


@ensure_csrf_cookie
@require_GET
def moods_page(request, slug=None):
    if slug is not None:
        found = Mood.by_slug_or_404(slug)
        if found.slug != slug:  # renamed since: its page now has the new name
            from django.shortcuts import redirect
            return redirect(f'/moods/{found.slug}/', permanent=False)
    device = mood_auth.device_for(request)
    return render(request, 'conversations/moods.html', {
        'initial_slug': slug or '',
        # A preview can pretend to be someone, to show the composer; its
        # database is read-only, so nothing it sends is kept.
        'viewer': device.entity_id if device else getattr(settings, 'PREVIEW_VIEWER', ''),
        'preview_label': getattr(settings, 'PREVIEW_LABEL', ''),
        # [[ in the composer suggests PickiPedia titles, asked of the wiki itself.
        'pickipedia_url': getattr(settings, 'PICKIPEDIA_URL', 'https://pickipedia.xyz').rstrip('/'),
        # Voice memos and reading aloud, if an ElevenLabs key is set (services/voice.py).
        'voice_enabled': bool(getattr(settings, 'ELEVENLABS_API_KEY', '')),
        # 'key' or 'wiki' (services/wiki_auth.py): a wiki sign-in chats and mentions people only.
        'viewer_tier': device.tier if device else '',
        'wiki_signin': wiki_auth.enabled(),
        # "→ PickiPedia" on a picture sent here, if uploading as its sharer is set up (services/wiki_upload.py).
        'wiki_upload': wiki_upload.enabled(),
        # An admin signed in with their SSH key may delete anyone's message (services/retract.py).
        'viewer_admin': bool(device) and device.tier == 'key' and device.entity_id in getattr(settings, 'MOOD_ADMINS', ()),
        # PickiPedia names, shown for the names here (from hunter's inventory).
        'wiki_names': wiki_auth.names(),
        # Notifications with magenta closed (services/push.py): what browsers subscribe with. '' if off.
        'push_key': push_key(),
        'page_version': page_version(),
    })


def push_key():
    """push.public_key(), or '' if anything about push is amiss: the page loads regardless."""
    try:
        return push.public_key()
    except Exception:
        import logging
        logging.getLogger(__name__).exception('push key unusable; push off for this page')
        return ''


@functools.cache
def page_version():
    """This page's code as served now. A page left open compares it (api/servers) and offers a reload."""
    from django.template.loader import get_template
    with open(get_template('conversations/moods.html').origin.name, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()[:12]


@require_GET
def api_moods(request):
    """Every Mood, most recently active first; archived ones flagged (the page lists them apart)."""
    from .services import settings as knobs
    archived, pinned = knobs.archived_slugs(), knobs.mood_flagged('pinned')
    moods = list(Mood.objects.all())
    payloads = [{**mood_payload(m), 'archived': m.slug in archived, 'pinned': m.slug in pinned} for m in moods]
    # A Mood nobody has spoken in yet ranks by when it was started: a new one first.
    started = {m.slug: m.created_at.isoformat() for m in moods}
    payloads.sort(key=lambda p: p['last_at'] or started[p['slug']], reverse=True)
    payloads.sort(key=lambda p: not p['pinned'])  # pinned first, each part still newest first
    people = ThinkingEntity.objects.order_by('name')
    return JsonResponse({'moods': payloads, 'people': [
        {'name': p.name, 'is_human': p.is_biological_human} for p in people]})


@require_GET
def api_mood_turns(request, slug):
    """The readable conversation in one Mood.

    Pass ?after=<message id> to get only what arrived since -- the polling
    contract. An unknown `after` is treated as "everything", so a client
    that has fallen out of sync recovers rather than stalls.
    """
    mood = Mood.by_slug_or_404(slug)
    if mood.slug in wiki_feed.feed_moods():
        wiki_feed.nudge()  # what's new on PickiPedia, as lines here (at most once a minute, in the background)

    after = before = None
    if request.GET.get('after'):
        after = _message_or_none(request.GET['after'])
    if request.GET.get('before'):
        before = _message_or_none(request.GET['before'])
    # ?from=<id>: that message and everything since -- what a runner reads
    # when a post links a message to read from.
    start = _message_or_none(request.GET['from']) if request.GET.get('from') else None
    if request.GET.get('from') and (start is None or start.mood_id != mood.id):
        # Never this Mood read from another's moment: the message must be here.
        return JsonResponse({'error': 'no such message in this Mood',
                             'mood': start.mood_slug if start else None}, status=404)
    # A first load, or a page back, is the newest PAGE items; a poll is
    # everything since.
    limit = None if after is not None or start is not None else PAGE
    if request.GET.get('limit', '').isdigit():  # e.g. the runner wanting recent context only
        limit = max(1, min(int(request.GET['limit']), PAGE))

    names = known_names()
    turns_out, step_msgs, quiet_out, thoughts_out, compactions_out, events_out = [], [], [], [], [], []
    for kind, msg, text in timeline(mood, after=after, before=before, limit=limit, start=start):
        if kind == 'turn':
            turns_out.append(turn_payload(msg, text, names))
        elif kind == 'thought':
            thoughts_out.append({**how_payload(msg), 'id': str(msg.id), 'sender': msg.sender_id,
                                 'created_at': msg.created_at.isoformat(), 'text': text})
        elif kind == 'event':
            events_out.append({'id': str(msg.id), 'created_at': msg.created_at.isoformat(),
                               **{k: text.get(k) for k in ('type', 'server', 'state', 'commit', 'by', 'note', 'took', 'agent',
                                                   'kind', 'title', 'user', 'comment', 'delta', 'revid', 'at',
                                                   'who', 'tier', 'label', 'device', 'devices', 'file', 'page', 'sha',
                                                   'message', 'already', 'for', 'from_mood', 'to_mood', 'about', 'context',
                                                   'pulled')},
                               # A handoff's context, as a message is shown (services/handoff.py).
                               **({'html': render_html(text.get('context') or '')} if text.get('type') == 'handoff' else {})})
        elif kind == 'compaction':
            compactions_out.append({'id': str(msg.id), 'session_id': str(msg.session_id or ''),
                                    'created_at': msg.created_at.isoformat(), 'html': render_html(text)})
        elif kind == 'quiet':
            quiet_out.append({'id': str(msg.id), 'sender': msg.sender_id,
                              'created_at': msg.created_at.isoformat(), **text})
        else:
            step_msgs.append(msg)
    images = step_images(step_msgs)
    steps_out = [{**step_payload(m), 'images': images.get(str(m.id), [])} for m in step_msgs]
    first = min([i['created_at'] for i in turns_out + steps_out + quiet_out + thoughts_out + compactions_out
                 + events_out], default=None)
    agents = agents_in(mood)
    # Edited or deleted (services/retract.py): shown as they stand now.
    from django.utils import timezone
    from .models import MessageChange
    from .services import retract
    changed = {str(c.message_id): c for c in MessageChange.objects.filter(
        mood=mood, message_id__in=[t['id'] for t in turns_out])}
    turns_out = [retract.mark(t, changed.get(t['id'])) for t in turns_out]
    changes = []
    since = parse_datetime(request.GET.get('changes_since') or '') if request.GET.get('changes_since') else None
    if since is not None:  # what changed since the page last asked, as it stands now
        for c in retract.changes_since(mood, since):
            msg = Message.objects.filter(id=c['id']).select_related('sender').first()
            text = prose(msg.content) if msg else ''
            changes.append({**c, 'turn': retract.mark(turn_payload(msg, text, names), retract.change_of(msg))
                            if msg and text else None})
    from .services import reactions
    return JsonResponse({
        'mood': mood_payload(mood),
        # Every message here that has reactions, as they now stand (services/reactions.py).
        'reactions': reactions.in_mood(mood),
        # What was edited or deleted since ?changes_since=, as it stands now; and the time to ask from next.
        'changes': changes,
        'now': timezone.now().isoformat(),
        # Prose only: the poller reads an agent turn here as an answer, so a
        # tool call must never appear in this list.
        'turns': turns_out,
        'steps': steps_out,
        # Choices not to speak, shown as dots. Not turns: they answer nothing.
        'quiet': quiet_out,
        # What the agent thought along the way, where the harness kept it.
        'thoughts': thoughts_out,
        # Where a session's context was compacted, and the summary it went on from.
        'compactions': compactions_out,
        # Things that happened around the conversation: a server redeployed.
        'events': events_out,
        'has_earlier': bool(limit) and first is not None and mood.messages.filter(
            is_sidechain=False, created_at__lt=first).exists(),
        'activity': activity(mood),
        # What an agent started in the background here and is still running.
        'tasks': background_tasks(mood),
        # Whether each agent is listening here (the hush menu in the head).
        'listening': {name: a['listening'] for name, a in agents.items()},
        # Each agent here: listening, model, effort, and how full its context is.
        'agents': agents,
        'scram': settings_scram(),
        'typing': typing_in(mood.slug),
    })


PAGE = 400


def agents_in(mood):
    """name -> {listening, model, effort, context} for each agent, in this Mood."""
    from .services import settings as knobs
    from .services.mood_view import context_in
    from .models import Setting
    agents = ThinkingEntity.objects.filter(is_biological_human=False).values_list('name', flat=True)
    shown = ('listening', 'model', 'mention_effort', 'ultracode', 'catch_up_tokens', 'discretion', 'verbosity')
    rows = list(Setting.objects.filter(key__in=shown))
    out = {}
    for name in agents:
        resolved = knobs.resolve(mood.slug, name)
        out[name] = {'listening': resolved['listening'], 'model': resolved['model'],
                     'effort': resolved['mention_effort'], 'ultracode': bool(resolved['ultracode']),
                     'reads': resolved['catch_up_tokens'],
                     'discretion': resolved['discretion'], 'verbosity': resolved['verbosity'],
                     # Who set each as it stands, and when: the page says so, and flashes one that just changed.
                     'set_by': knobs.set_by(mood.slug, name, shown, rows=rows),
                     'context': context_in(mood, name)}
    return out


def settings_scram():
    from .services import settings as knobs
    return knobs.scram()


def _message_or_none(raw):
    try:
        return Message.objects.filter(id=raw).first()
    except (ValueError, ValidationError):
        return None


@require_GET
def media_file(request, sha256, ext):
    """A stored image. Immutable by construction: the name is its hash."""
    from django.http import HttpResponse
    from .models import Media

    item = Media.objects.filter(sha256=sha256).first()
    if item is None or Media.EXTENSIONS.get(item.mime) != ext:
        raise Http404('no such image')
    response = HttpResponse(bytes(item.data), content_type=item.mime)
    response['Cache-Control'] = 'public, max-age=31536000, immutable'
    response['X-Content-Type-Options'] = 'nosniff'
    response['Content-Security-Policy'] = "default-src 'none'; sandbox"
    return response


@require_GET
def api_pulse(request):
    """Every Mood at a glance, for a runner looking every few seconds.

    Per Mood: its newest message, newest web post and newest word from a
    person; who is typing; what an agent is doing. All of it is readable
    elsewhere already -- this only saves asking Mood by Mood.
    """
    from django.utils import timezone

    def brief(message):
        return message and {'id': str(message['id']), 'created_at': message['created_at'].isoformat(),
                            'sender': message['sender_id']}

    from .models import Setting
    from .services import settings as knobs

    humans = set(ThinkingEntity.objects.filter(is_biological_human=True).values_list('name', flat=True))
    agent = request.GET.get('agent', 'magent')
    rows = list(Setting.objects.exclude(key__in=knobs.MODERATION_KEYS))
    archived = knobs.archived_slugs()
    wiki_feed.nudge()  # asked every second by the runner: the wiki feed's clock (at most once a minute)
    push.nudge()       # and the push clock: notices to send to closed pages (at most every 10 s)
    moods = []
    for mood in Mood.objects.all():
        said = mood.messages.filter(is_sidechain=False).exclude(sender_id__in=MACHINERY_SENDERS)
        fields = ('id', 'created_at', 'sender_id')
        moods.append({
            'slug': mood.slug,
            'newest': brief(said.order_by('-created_at').values(*fields).first()),
            'last_web_post': brief(said.filter(source_file='mood-web').order_by('-created_at').values(*fields).first()),
            'last_human': brief(said.filter(sender_id__in=humans).order_by('-created_at').values(*fields).first()),
            'typing': typing_in(mood.slug),
            'activity': activity(mood),
            # How this agent is to carry itself here (services/settings.py).
            'settings': knobs.resolve(mood.slug, agent, rows=rows),
            # Out of the list: no unprompted looks there (mentions still answered).
            'archived': mood.slug in archived,
            # Names it went by before a rename: a runner told the old one still knows it.
            'aliases': sorted(mood.aliases.values_list('slug', flat=True)),
        })
    from .services import access
    return JsonResponse({
        'now': timezone.now().isoformat(),
        'agent': agent,
        # Who has just signed in, for a runner to greet if it knows them (services/access.py).
        'arrivals': access.recent_arrivals(),
        # An admin's emergency stop: while set, a runner wakes nothing.
        'scram': knobs.scram(),
        'budget': {'consider_usd_per_day': knobs.global_value('consider_usd_per_day')},  # the runner's own
        'moods': moods,
    })


@require_GET
def api_mood_step(request, slug, step_id):
    """One tool call in full, for a step someone opened."""
    msg = _message_or_none(step_id)
    if msg is None or msg.mood_slug != Mood.by_slug_or_404(slug).slug or not hasattr(msg, 'tooluse'):
        raise Http404('no such step in this Mood')
    return JsonResponse(step_detail(msg))


# --- who is typing ---------------------------------------------------------
# Ephemeral, so not in the record: a shared cache entry per Mood holding
# name -> when they last typed. Every gunicorn worker shares the cache.
TYPING_FOR = 8  # seconds a keystroke counts as "typing"


def _typing_key(slug):
    return f'typing:{slug}'


def typing_in(slug):
    from django.core.cache import cache
    import time
    now = time.time()
    return sorted(name for name, at in (cache.get(_typing_key(slug)) or {}).items() if now - at < TYPING_FOR)


@require_POST
def api_typing(request, slug):
    """Say this device's person is (or stopped) typing in a Mood."""
    from django.core.cache import cache
    import json
    import time
    from .views_admin import locked_response
    if locked_response():
        return locked_response()
    device = mood_auth.device_for(request)
    if device is None:
        return JsonResponse({'error': 'sign in to write'}, status=401)
    Mood.by_slug_or_404(slug)
    try:
        typing = bool(json.loads(request.body or b'{}').get('typing', True))
    except (ValueError, AttributeError):
        return JsonResponse({'error': 'expected {"typing": true|false}'}, status=400)
    now = time.time()
    state = {n: at for n, at in (cache.get(_typing_key(slug)) or {}).items() if now - at < TYPING_FOR}
    if typing:
        state[device.entity_id] = now
    else:
        state.pop(device.entity_id, None)
    cache.set(_typing_key(slug), state, TYPING_FOR * 2)
    return JsonResponse({'typing': sorted(state)})


@require_GET
def api_mood_sessions(request, slug):
    """Runtime sessions in a Mood, most recently active first.

    ?sender=<name> keeps only sessions in which that entity wrote. This is
    how the poller finds a session to resume when it wakes an agent: the
    Mood is durable, and whichever process last spoke for the agent is
    the one to continue. A session id is a local handle, not a credential.
    """
    mood = Mood.by_slug_or_404(slug)
    messages = Message.objects.filter(mood=mood, session_id__isnull=False)
    sender = request.GET.get('sender')
    if sender:
        messages = messages.filter(sender_id=sender.lower())
    sessions = (messages.values('session_id')
                .annotate(last_at=Max('created_at'))
                .order_by('-last_at'))
    return JsonResponse({'mood': mood.slug, 'sessions': [
        {'session_id': str(s['session_id']), 'last_at': s['last_at'].isoformat()} for s in sessions
    ]})


@require_GET
def api_wikilinks(request):
    """Turns in Moods that link to PickiPedia pages with [[...]], oldest first.

    This is what PickiPedia reads to reach Moods without storing them.
    ?page=<title> keeps one page's backlinks. ?since=<iso> starts after a
    moment; pass back `next_since` to page forward. A message's links are
    never split across pages. The cursor is ingest time (created_at), so a
    consumer should overlap its cursor by a few minutes and dedupe on
    (turn, page), and resync fully now and then: mood_assign attaches
    old messages to a Mood without changing their created_at.
    Titles are normalised as MediaWiki does.
    """
    page = request.GET.get('page')
    page = wiki_title(page) if page else None
    since_raw = request.GET.get('since')
    since = parse_datetime(since_raw) if since_raw else None
    if since_raw and since is None:
        return JsonResponse({'error': f'unparseable since: {since_raw!r} (URL-encode the +)'}, status=400)
    try:
        limit = min(max(int(request.GET.get('limit', 200)), 1), 1000)
    except ValueError:
        limit = 200

    names = known_names()
    messages = (
        Message.objects.filter(mood__isnull=False, is_sidechain=False)
        .exclude(sender_id__in=MACHINERY_SENDERS)
        .order_by('created_at')
    )
    if since:
        messages = messages.filter(created_at__gt=since)

    links, next_since = [], None
    for msg in messages.iterator():
        if len(links) >= limit:
            break
        next_since = msg.created_at.isoformat()
        if msg.sender_id not in names:
            continue
        text = prose(msg.content)
        if not text or is_wrapper(text):
            continue
        for title in wikilinks_in(text):
            if page and title != page:
                continue
            links.append({'page': title, 'mood': msg.mood_slug, 'turn': str(msg.id),
                          'sender': msg.sender_id, 'created_at': msg.created_at.isoformat(),
                          'eth_blockheight': msg.eth_blockheight})
    else:
        next_since = None  # read to the end

    return JsonResponse({'links': links, 'next_since': next_since})


SEARCH_MAX = 40
SEARCH_SCAN = 300  # candidates read, newest first, before keeping only what people and agents said


def snippet(text, q, width=160):
    """`text` around the first place `q` occurs, with its ends marked if cut."""
    at = text.lower().find(q.lower())
    if at < 0:
        return text[:width]
    start = max(0, at - width // 3)
    end = min(len(text), start + width)
    return ('…' if start else '') + text[start:end] + ('…' if end < len(text) else '')


@require_GET
def api_search(request):
    """What was said, in one Mood (?mood=<slug>) or all of them, that contains ?q=.

    Newest first, at most SEARCH_MAX. Only what people and agents said --
    not tool calls, their results or the harness's wrappers. A plain
    case-insensitive match on the stored text: about 0.15 s in one Mood,
    1.2 s across all of them, at 25k messages.
    """
    from django.db.models import TextField
    from django.db.models.functions import Cast

    q = (request.GET.get('q') or '').strip()
    if len(q) < 2:
        return JsonResponse({'error': 'two characters at least'}, status=400)
    q = q[:200]
    names = known_names()
    rows = (Message.objects.filter(mood__isnull=False, is_sidechain=False)
            .exclude(sender_id__in=MACHINERY_SENDERS)
            .annotate(text=Cast('content', TextField())).filter(text__icontains=q))
    mood = request.GET.get('mood')
    if mood:
        rows = rows.filter(mood__slug=mood)
    titles = dict(Mood.objects.values_list('slug', 'title'))
    hits = []
    for msg in rows.select_related('sender').order_by('-created_at')[:SEARCH_SCAN]:
        if msg.sender_id not in names:
            continue
        text = prose(msg.content)
        if not text or is_wrapper(text) or q.lower() not in text.lower():
            continue
        hits.append({'id': str(msg.id), 'mood': msg.mood_slug, 'title': titles.get(msg.mood_slug, msg.mood_slug),
                     'sender': msg.sender_id, 'created_at': msg.created_at.isoformat(), 'text': snippet(text, q)})
        if len(hits) >= SEARCH_MAX:
            break
    return JsonResponse({'q': q, 'mood': mood, 'hits': hits})


RECENT_MAX = 30
RECENT_SCAN = 800  # rows read for what was said, newest first


@require_GET
def api_recent(request):
    """What's been happening across every Mood, newest first: what people said,
    agents' finished answers, renames, settings changed, Moods opened.

    ?since=<iso> (default: a day ago), ?limit= (at most RECENT_MAX). Read
    from the record; agents' progress lines between tool calls are left
    out, or they would be all there is.
    """
    from datetime import timedelta
    from django.utils import timezone
    from .models import Setting
    from .services.mood_view import quiet_reason

    since = parse_datetime(request.GET.get('since') or '') or timezone.now() - timedelta(days=1)
    try:
        limit = min(max(int(request.GET.get('limit', RECENT_MAX)), 1), RECENT_MAX)
    except ValueError:
        limit = RECENT_MAX
    names = known_names()
    agents = set(ThinkingEntity.objects.filter(is_biological_human=False).values_list('name', flat=True))
    titles = dict(Mood.objects.values_list('slug', 'title'))
    events = []

    said = (Message.objects.filter(mood__isnull=False, is_sidechain=False, created_at__gt=since)
            .exclude(sender_id__in=MACHINERY_SENDERS).order_by('-created_at')[:RECENT_SCAN])
    for msg in said:
        if msg.sender_id not in names:
            continue
        text = prose(msg.content)
        if not text or is_wrapper(text) or quiet_reason(text) is not None:
            continue
        if msg.sender_id in agents and msg.stop_reason != 'end_turn':
            continue
        events.append({'kind': 'answered' if msg.sender_id in agents else 'said', 'at': msg.created_at.isoformat(),
                       'mood': msg.mood_slug, 'who': msg.sender_id, 'id': str(msg.id), 'text': text[:140]})
        if len(events) >= limit:
            break

    for msg in (Message.objects.filter(source_file='mood-rename', created_at__gt=since)
                .order_by('-created_at')[:limit]):
        content = msg.content if isinstance(msg.content, dict) else {}
        events.append({'kind': 'renamed', 'at': msg.created_at.isoformat(), 'mood': msg.mood_slug,
                       'who': content.get('by', ''), 'text': f"{(content.get('from') or {}).get('title', '')} → "
                                                             f"{(content.get('to') or {}).get('title', '')}"})

    for row in Setting.objects.filter(created_at__gt=since).order_by('-created_at')[:limit]:
        value = row.value if not isinstance(row.value, dict) else (row.value.get('mode') or row.value)
        events.append({'kind': 'set', 'at': row.created_at.isoformat(), 'mood': mood_slug(row.mood_id),
                       'who': row.set_by_id or '', 'text': f"{row.key} for {row.agent_id or 'every agent'}: {value}"[:140]})

    told = set()  # a redeploy is announced in several Moods; list it once
    for row in (Message.objects.filter(source_file='deploy', created_at__gt=since).order_by('-created_at')
                .values('content', 'created_at')[:limit * 10]):
        c = row['content'] if isinstance(row['content'], dict) else {}
        key = (c.get('server'), c.get('state'), row['created_at'].replace(microsecond=0).isoformat()[:18])
        if key in told:
            continue
        told.add(key)
        verb = {'started': 'redeploy started', 'finished': 'redeployed', 'failed': 'redeploy failed'}.get(c.get('state'), '')
        events.append({'kind': 'deploy', 'at': row['created_at'].isoformat(), 'mood': None, 'who': c.get('by', ''),
                       'text': f"{c.get('server')} {verb}" + (f" · {c['commit'][:8]}" if c.get('commit') else '')})

    for mood in Mood.objects.filter(created_at__gt=since).order_by('-created_at')[:limit]:
        events.append({'kind': 'opened', 'at': mood.created_at.isoformat(), 'mood': mood.slug, 'who': '',
                       'text': mood.title or mood.slug})

    events.sort(key=lambda e: e['at'], reverse=True)
    for e in events:
        e['title'] = titles.get(e['mood'], e['mood'] or 'every Mood')
    return JsonResponse({'events': events[:limit]})


NOTICES_MAX = 100


def answered_by(msg, humans):
    """Who an agent's finished turn was answering: the person whose words were
    the last a person said in that Mood before it, or None."""
    earlier = (Message.objects.filter(mood_id=msg.mood_id, is_sidechain=False, created_at__lt=msg.created_at,
                                      sender_id__in=humans)
               .order_by('-created_at').only('content', 'sender_id')[:5])
    for m in earlier:  # the latest that is someone's words, not a harness wrapper
        text = prose(m.content)
        if text and not is_wrapper(text):
            return m.sender_id
    return None


@require_GET
def api_notices(request, name):
    """What `name` would want to hear about, across every Mood, since a moment.

    Two kinds, newest first: a 'mention' of them by someone else, and an
    'answer' -- an agent's turn that ended (end_turn) in a Mood where they
    were the last person to speak before it. That second kind is how someone
    who asked something and went elsewhere learns the agent is done, without
    the agent having to @mention them back.

    ?since=<iso> (default: an hour ago). At most NOTICES_MAX.
    """
    from datetime import timedelta
    from django.utils import timezone

    name = name.lower()
    if not ThinkingEntity.objects.filter(name=name).exists():
        raise Http404
    since = parse_datetime(request.GET.get('since') or '') or timezone.now() - timedelta(hours=1)
    return JsonResponse({'name': name, 'notices': notices_for(name, since)})


def notices_for(name, since, limit=NOTICES_MAX):
    """api_notices' notices for `name` since a moment, newest first: what the
    bell tells them, in the page and by push (services/push.py)."""
    from .services.mood_view import quiet_reason
    names = known_names()
    agents = set(ThinkingEntity.objects.filter(is_biological_human=False).values_list('name', flat=True))
    humans = set(ThinkingEntity.objects.filter(is_biological_human=True).values_list('name', flat=True))
    messages = (Message.objects.filter(mood__isnull=False, is_sidechain=False, created_at__gt=since)
                .exclude(sender_id__in=MACHINERY_SENDERS).select_related('sender').order_by('-created_at'))
    found = []
    for msg in messages.iterator():
        if msg.sender_id not in names or msg.sender_id == name:
            continue
        text = prose(msg.content)
        if not text or is_wrapper(text):
            continue
        if name in addressed_in(text, names, by=msg.sender_id):
            found.append({'kind': 'mention', 'mood': msg.mood_slug, 'turn': turn_payload(msg, text, names)})
        elif (msg.sender_id in agents and msg.stop_reason == 'end_turn' and quiet_reason(text) is None
              and answered_by(msg, humans) == name):
            found.append({'kind': 'answer', 'mood': msg.mood_slug, 'turn': turn_payload(msg, text, names)})
        if len(found) >= limit:
            break
    return found


@require_GET
def api_mentions(request, name):
    """Recent turns, across all Moods, that mention one thinking entity.

    Newest first. ?since=<iso> narrows to what arrived after a moment;
    ?limit= caps the result (default 50, max 200). Mentions are derived
    from message text at read time -- there is no mention table -- so this
    scans, bounded by `since` and `limit`. Fine at today's volume.

    This is what a notification badge reads, and what a poller reads to
    learn that an agent has been addressed by name.
    """
    name = name.lower()
    if not ThinkingEntity.objects.filter(name=name).exists():
        raise Http404

    since = parse_datetime(request.GET.get('since') or '')
    try:
        limit = min(max(int(request.GET.get('limit', 50)), 1), 200)
    except ValueError:
        limit = 50

    names = known_names()
    is_agent = not ThinkingEntity.objects.get(name=name).is_biological_human
    messages = (
        Message.objects.filter(mood__isnull=False, is_sidechain=False)
        .exclude(sender_id__in=MACHINERY_SENDERS)
        .select_related('sender')
        .order_by('-created_at')
    )
    if since:
        messages = messages.filter(created_at__gt=since)

    found = []
    for msg in messages.iterator():
        if msg.sender_id not in names:
            continue
        if is_agent and from_wiki_tier(msg):
            continue  # signed in with PickiPedia: an @agent is just text, it wakes nobody
        text = prose(msg.content)
        if not text or is_wrapper(text):
            continue
        if name in addressed_in(text, names, by=msg.sender_id):
            found.append({'mood': msg.mood_slug, 'turn': turn_payload(msg, text, names)})
            if len(found) >= limit:
                break

    return JsonResponse({'name': name, 'mentions': found})


# --- installing it as an app (a web app manifest, an icon, a service worker) --
# Chrome and Edge (Ubuntu, Windows, macOS) offer "Install" for a page with a
# manifest, and Chrome on Android "Install app": it then opens in its own
# window, from the launcher, with no browser around it.

@require_GET
def app_manifest(request):
    return JsonResponse({
        'name': 'magenta',
        'short_name': 'magenta',
        'description': 'Moods: where cryptograss talks, people and agents together.',
        'id': '/moods/',
        'start_url': '/moods/',
        'scope': '/moods/',
        'display': 'standalone',
        'background_color': '#fbfaf7',
        'theme_color': '#b8106b',
        'icons': [{'src': f'/moods/icon-{size}.png', 'sizes': f'{size}x{size}', 'type': 'image/png',
                   'purpose': 'any maskable'} for size in (192, 512)],
    }, content_type='application/manifest+json')


@require_GET
def app_icon(request, size):
    from django.http import HttpResponse
    from .services import app_icon as icon
    if size not in (180, 192, 512):
        raise Http404('no icon that size')
    response = HttpResponse(icon.png(size), content_type='image/png')
    response['Cache-Control'] = 'public, max-age=86400'
    return response


SERVICE_WORKER = """// magenta: here so the page can be installed as an app. It keeps
// nothing: every request goes to the network, as if it weren't here.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', e => {
  if (e.request.mode === 'navigate') e.respondWith(fetch(e.request));
});
// A push (services/push.py): a mention or an answer, while magenta is closed.
// A magenta page in front of its person tells them itself, so then: nothing.
self.addEventListener('push', e => {
  let n = null;
  try { n = e.data ? e.data.json() : null; } catch (err) {}
  if (!n || !n.title) return;
  e.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(list => {
    if (list.some(c => c.visibilityState === 'visible' && new URL(c.url).pathname.startsWith('/moods/'))) return;
    return self.registration.showNotification(n.title, { body: n.body || '', tag: n.tag, icon: '/moods/icon-192.png',
                                                         data: n.data || {} });
  }));
});
// A notification (a mention, an answer) opens its Mood at that message: in a
// window already open on magenta if there is one, else a new one.
self.addEventListener('notificationclick', e => {
  e.notification.close();
  const { slug, id } = e.notification.data || {};
  const url = '/moods/' + encodeURIComponent(slug || '') + '/' + (id ? '#m-' + id : '');
  e.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(list => {
    const open = list.find(c => new URL(c.url).pathname.startsWith('/moods/'));
    if (open) { open.postMessage({ open: slug, id }); return open.focus(); }
    return self.clients.openWindow(url);
  }));
});
"""


@require_GET
def app_service_worker(request):
    from django.http import HttpResponse
    response = HttpResponse(SERVICE_WORKER, content_type='text/javascript')
    response['Service-Worker-Allowed'] = '/moods/'
    response['Cache-Control'] = 'no-cache'
    return response


@require_GET
def api_work(request):
    """Open pull requests and new issues across our repositories, with the people
    and Moods each involves (services/work.py)."""
    from django.core.cache import cache
    from .services import work
    items = cache.get('work:open')
    if items is None:
        try:
            items = work.open_work()
        except Exception as e:  # the forge unreachable, or rate-limited: say so, don't fail the page
            return JsonResponse({'items': [], 'error': f'could not ask the forge: {type(e).__name__}'})
        cache.set('work:open', items, 120)
    return JsonResponse({'items': items})


# --- who's around in each Mood, and the chain's height -------------------------

AROUND_BLOCKS = 100     # who spoke within this many blocks counts as around
SECONDS_PER_BLOCK = 12  # since the merge, a slot every 12 s (a missed slot makes it a little more)


@require_GET
def api_mood_todo(request, slug):
    """The Mood's to-do list, from PickiPedia (services/todo.py): {"page", "edit", "exists", "items", "error"?}.
    ?fresh=1 asks PickiPedia again now (after an edit)."""
    from .services import todo
    mood = Mood.by_slug_or_404(slug)
    if request.GET.get('fresh'):
        todo.forget(mood)
    return JsonResponse(todo.for_mood(mood))


@require_GET
def api_unfurl(request, slug, message_id):
    """The preview cards for a message's plain links (services/unfurl.py): {"cards": [...]}. Only links
    said in this Mood, so the server fetches nothing a reader names; each page kept a day."""
    from .services import unfurl
    from .views_auth import _under_limit, _uuid_or_none
    mood = Mood.by_slug_or_404(slug)
    message = Message.objects.filter(id=_uuid_or_none(message_id), mood=mood).first()
    if message is None:
        return JsonResponse({'error': 'no such message in this Mood'}, status=404)
    if not _under_limit('unfurl:' + (request.META.get('REMOTE_ADDR') or ''), 60):
        return JsonResponse({'error': 'slow down'}, status=429)
    cards = [c for c in (unfurl.card(url) for url in unfurl.links_in(prose(message.content))) if c]
    return JsonResponse({'cards': cards})


@require_GET
def api_moods_live(request):
    """Each Mood's people of the moment: who has spoken in the last 100 blocks
    (about 20 minutes), who's typing, which agent is working. Cached for
    3 s: every open page asks every few seconds."""
    from datetime import timedelta
    from django.core.cache import cache
    from django.utils import timezone

    push.nudge()  # every open page asks this: the push clock runs while the runner's away, too
    cached = cache.get('moods-live')
    if cached is not None:
        return JsonResponse(cached)
    now = timezone.now()
    names = set(ThinkingEntity.objects.values_list('name', flat=True))
    since = now - timedelta(seconds=AROUND_BLOCKS * SECONDS_PER_BLOCK)
    spoke = (Message.objects.filter(mood__isnull=False, is_sidechain=False, created_at__gte=since,
                                    sender_id__in=names)
             .values('mood__slug', 'sender_id').annotate(last=Max('created_at')).order_by('-last'))
    live = {}
    for row in spoke:
        live.setdefault(row['mood__slug'], {'speakers': [], 'typing': [], 'busy': None})['speakers'].append(row['sender_id'])
    for mood in Mood.objects.all():
        entry = live.setdefault(mood.slug, {'speakers': [], 'typing': [], 'busy': None})
        entry['typing'] = typing_in(mood.slug)
        if mood.slug in {r['mood__slug'] for r in spoke}:  # only a Mood with recent words can have work under way
            act = activity(mood)
            if act and act.get('doing') != 'held':
                entry['busy'] = act['agent']
    body = {'moods': live, 'blocks': AROUND_BLOCKS}
    cache.set('moods-live', body, 3)
    return JsonResponse(body)


@require_GET
def api_block(request):
    """Ethereum mainnet's newest block -- height and time -- from a public
    explorer, cached a minute; the page counts on from it, a block every
    12 seconds."""
    from django.core.cache import cache
    block = cache.get('eth-head')
    if block is None:
        try:
            import requests
            head = requests.get('https://eth.blockscout.com/api/v2/main-page/blocks', timeout=5).json()[0]
            block = {'height': int(head['height']), 'at': head['timestamp'], 'seconds': SECONDS_PER_BLOCK}
        except Exception:
            block = {'height': None}
        cache.set('eth-head', block, 60 if block['height'] else 15)
    return JsonResponse(block)
