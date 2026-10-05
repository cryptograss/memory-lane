"""Agent turns launched for a Mood stream straight into it.

The runner (poller/mood_poller.py) launches an agent's turn headless --
`claude -p --output-format stream-json` -- and posts each event as it comes
out of the process. Three things follow from the runner having launched the
turn itself:

  - The turn's session is claimed for this Mood outright. Nothing is
    inferred from transcript files, which is how a woken reply once landed
    in no Mood at all.
  - What the agent says arrives as it says it, not when a watcher next
    reads a file.
  - The turn's end is known exactly: the `result` event. Stream events carry
    no stop_reason, so the result stamps the turn's last message, and the
    Mood stops showing the agent at work the moment it is done.

The same turn's transcript still reaches the record through the watcher. Its
lines carry the same uuids, so they find these rows rather than adding new
ones, and fill in what only a transcript has (effort, cwd, parents): the
transcript is metadata now, the stream is the conversation.

A runner proves itself with a key from the vault, bound to the agent it
speaks for (settings.MOOD_RUNNER_KEYS).
"""

import hmac
import json
import uuid

from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from .models import ConversationParticipant, Message, Mood, MoodSession

SOURCE = 'mood-runner'  # rows are filed as 'ingest-mood-runner'
MAX_EVENTS = 500
TURN_ENDS = {'end_turn', 'refusal', 'stop_sequence', 'max_tokens'}


def runner_agent(request):
    """The agent a valid runner key speaks for, or None."""
    header = request.headers.get('Authorization', '').encode()
    for agent, key in getattr(settings, 'MOOD_RUNNER_KEYS', {}).items():
        if key and hmac.compare_digest(header, f'Bearer {key}'.encode()):
            return agent
    return None


def stream_line(event, now_iso):
    """The transcript line a stream-json event corresponds to, or None.

    Claude Code's stream events hold the same message objects as its
    transcript lines, under slightly different names. Only messages become
    lines; the init, rate-limit and result events are about the run.
    """
    if not isinstance(event, dict) or event.get('type') not in ('assistant', 'user'):
        return None
    if not isinstance(event.get('message'), dict) or not event.get('uuid'):
        return None
    line = {
        'type': event['type'],
        'uuid': event['uuid'],
        'parentUuid': None,
        'sessionId': event.get('session_id'),
        'timestamp': event.get('timestamp') or now_iso,
        'userType': 'external',
        'entrypoint': 'sdk-cli',
        # A helper's events name the tool call that started it.
        'isSidechain': bool(event.get('parent_tool_use_id')),
        'message': event['message'],
    }
    if 'tool_use_result' in event:
        line['toolUseResult'] = event['tool_use_result']
    return json.dumps(line)


def finish_turn(mood, session_id, agent, result):
    """The run is over: stamp the turn's last message, and keep what it cost."""
    stop = result.get('stop_reason') if result.get('stop_reason') in TURN_ENDS else 'end_turn'
    last = (Message.objects.filter(session_id=session_id, sender_id=agent, is_sidechain=False)
            .order_by('-created_at').first())
    if last is not None and last.stop_reason is None:
        Message.objects.filter(pk=last.pk).update(stop_reason=stop)
    system, _ = ConversationParticipant.objects.get_or_create(name='system', defaults={'participant_type': 'system'})
    summary = {
        'type': 'turn-result',
        'agent': agent,
        'subtype': result.get('subtype'),
        'is_error': bool(result.get('is_error')),
        'cost_usd': result.get('total_cost_usd'),
        'duration_ms': result.get('duration_ms'),
        'num_turns': result.get('num_turns'),
        'stop_reason': result.get('stop_reason'),
    }
    row_id = result.get('uuid') or uuid.uuid5(uuid.NAMESPACE_URL, f'turn-result:{session_id}')
    Message.objects.get_or_create(id=row_id, defaults={
        'sender': system, 'content': summary, 'mood': mood, 'session_id': session_id,
        'source_file': f'ingest-{SOURCE}',
    })


@csrf_exempt  # authenticated by the runner's key, not a browser session
@require_POST
def api_stream(request, slug):
    """Events from a turn a runner launched for this Mood.

    Body: {"harness": "claude-code", "session_id": "<the turn's session>",
           "events": [<stream-json event>, ...]}
    """
    from .views import import_lines

    if not getattr(settings, 'MOOD_RUNNER_KEYS', {}):
        return JsonResponse({'error': 'no runners are configured'}, status=503)
    agent = runner_agent(request)
    if agent is None:
        return JsonResponse({'error': 'unauthorized'}, status=401)
    # The Claude Code importer speaks for magent; another agent needs its own.
    if agent != 'magent':
        return JsonResponse({'error': f'no importer speaks for {agent} yet'}, status=400)
    mood = Mood.by_slug_or_404(slug)

    try:
        body = json.loads(request.body)
        session_id = str(uuid.UUID(str(body['session_id'])))
        events = body['events']
        assert isinstance(events, list)
    except (ValueError, KeyError, TypeError, AssertionError):
        return JsonResponse({'error': 'expected {"session_id": <uuid>, "events": [...]}'}, status=400)
    if body.get('harness', 'claude-code') != 'claude-code':
        return JsonResponse({'error': 'only claude-code streams are understood so far'}, status=400)
    if len(events) > MAX_EVENTS:
        return JsonResponse({'error': f'at most {MAX_EVENTS} events per post'}, status=413)

    # Launched for this Mood: it belongs here, whatever else might be guessed.
    if MoodSession.mood_for(session_id) != mood:
        mood.claim(session_id)

    from django.utils import timezone
    now_iso = timezone.now().isoformat()
    lines = [line for line in (stream_line(e, now_iso) for e in events) if line]
    imported, skipped, errors = import_lines(lines, source=SOURCE, username=agent) if lines else (0, 0, [])

    finished = False
    for event in events:
        if isinstance(event, dict) and event.get('type') == 'result':
            finish_turn(mood, session_id, agent, event)
            finished = True

    return JsonResponse({'imported': imported, 'skipped': skipped, 'errors': errors[:10], 'finished': finished})


@csrf_exempt  # authenticated by the runner's key
@require_POST
def api_quiet(request, slug):
    """Record that the runner's screen let a moment pass for its agent.

    Body: {"reason": "a few words", "by": "screen"}. Shown as a dot, like the
    agent's own silences, but marked as the screen's: the record never
    passes a reflex off as the agent's considered judgment. No session: a
    screen is not a turn anyone could resume.
    """
    import re
    import time
    from .services.redaction import redact

    if not getattr(settings, 'MOOD_RUNNER_KEYS', {}):
        return JsonResponse({'error': 'no runners are configured'}, status=503)
    agent = runner_agent(request)
    if agent is None:
        return JsonResponse({'error': 'unauthorized'}, status=401)
    mood = Mood.by_slug_or_404(slug)
    try:
        body = json.loads(request.body)
        reason = str(body.get('reason', ''))
        by = str(body.get('by', 'screen'))
    except (ValueError, AttributeError):
        return JsonResponse({'error': 'expected {"reason": ..., "by": "screen"}'}, status=400)
    if not re.fullmatch(r'\w{1,20}', by):
        return JsonResponse({'error': 'by: a single word'}, status=400)
    reason = redact(re.sub(r'[<>]', '', reason).strip()[:300])[0]
    from .models import ThinkingEntity
    sender = ThinkingEntity.objects.filter(name=agent).first()
    if sender is None:
        return JsonResponse({'error': f'no entity {agent}'}, status=400)
    message = Message.objects.create(
        id=uuid.uuid4(), sender=sender, mood=mood, content=f'<silent by="{by}">{reason}</silent>',
        timestamp=int(time.time() * 1000), source_file=f'ingest-{SOURCE}', stop_reason='end_turn')
    return JsonResponse({'id': str(message.id)}, status=201)


@csrf_exempt  # authenticated by the runner's key
@require_POST
def api_held(request, slug):
    """The runner is holding its agent's next turn here, and why.

    Body: {"reason": "a few words", "until": "<iso>" or null}. An empty
    reason lifts the hold. Not in the record: it's how things stand, like
    typing, and lapses unless renewed (services/mood_view.py, HELD_FOR).
    """
    import re
    from datetime import datetime
    from .services.mood_view import set_held

    if not getattr(settings, 'MOOD_RUNNER_KEYS', {}):
        return JsonResponse({'error': 'no runners are configured'}, status=503)
    agent = runner_agent(request)
    if agent is None:
        return JsonResponse({'error': 'unauthorized'}, status=401)
    Mood.by_slug_or_404(slug)
    try:
        body = json.loads(request.body)
        reason = re.sub(r'<[^>]*>|[<>]', '', str(body.get('reason') or '')).strip()[:200]
        until = body.get('until') or None
        if until is not None:
            until = datetime.fromisoformat(str(until).replace('Z', '+00:00')).isoformat()
    except (ValueError, AttributeError, TypeError):
        return JsonResponse({'error': 'expected {"reason": ..., "until": <iso> or null}'}, status=400)
    holds = set_held(slug, agent, reason, until)
    return JsonResponse({'held': holds.get(agent)})


# --- servers: whether each answers, and their redeploys -------------------------

@csrf_exempt  # authenticated by the deploy key, not a browser session
@require_POST
def api_deploys(request):
    """A deploy script says a server's redeploy started, finished or failed.

    Body: {"server": "hunter", "state": "started"|"finished"|"failed",
           "commit": "<sha>", "by": "<who ran it>", "note": "..."}.
    Announced in the Moods it concerns (services/servers.py).
    """
    from .services import servers

    key = getattr(settings, 'MOOD_DEPLOY_KEY', '')
    if not key:
        return JsonResponse({'error': 'no deploy key is configured'}, status=503)
    if not hmac.compare_digest(request.headers.get('Authorization', '').encode(), f'Bearer {key}'.encode()):
        return JsonResponse({'error': 'unauthorized'}, status=401)
    try:
        body = json.loads(request.body)
        name, state = str(body['server']), str(body['state'])
    except (ValueError, KeyError, TypeError):
        return JsonResponse({'error': 'expected {"server": ..., "state": ...}'}, status=400)
    if name not in servers.NAMES or state not in servers.STATES:
        return JsonResponse({'error': f'server: one of {servers.NAMES}; state: one of {servers.STATES}'}, status=400)
    told = servers.record_deploy(name, state, commit=str(body.get('commit') or ''), by=str(body.get('by') or ''),
                                 note=str(body.get('note') or ''))
    return JsonResponse({'server': name, 'state': state, 'moods': told}, status=201)


@require_GET
def api_servers(request):
    """Every server: whether it answers (checked at most once a minute), and its newest redeploy."""
    from .services import servers
    return JsonResponse({'servers': servers.status()})
