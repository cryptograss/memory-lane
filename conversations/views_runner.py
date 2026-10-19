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
import re
import uuid

from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from .models import ConversationParticipant, Message, Mood, MoodSession, ThinkingEntity

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
    A background task's end becomes the notice a transcript would have.
    """
    if isinstance(event, dict) and event.get('type') == 'system' and event.get('subtype') == 'task_notification':
        return task_notice_line(event, now_iso)
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


def task_notice_line(event, now_iso):
    """The queue-operation line Claude Code writes when a background task
    ends, for a stream's task_notification event; None if it names no task.

    A turn run with -p stops the tasks it left running when it ends, and
    says so only on its stream, after the result: without this, a helper or
    command it started looked to be running for hours. Keyed by the event's
    uuid, so posting it again finds the same row.
    """
    import re
    task, status = str(event.get('task_id') or ''), str(event.get('status') or '')
    if not re.fullmatch(r'\w+', task) or not re.fullmatch(r'\w+', status) or not event.get('uuid'):
        return None
    content = (f"<task-notification>\n<task-id>{task}</task-id>\n<tool-use-id>{event.get('tool_use_id') or ''}</tool-use-id>\n"
               f"<status>{status}</status>\n<summary>{event.get('summary') or ''}</summary>\n</task-notification>")
    return json.dumps({'type': 'queue-operation', 'operation': 'enqueue', 'uuid': event['uuid'],
                       'timestamp': event.get('timestamp') or now_iso, 'sessionId': event.get('session_id'),
                       'content': content})


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
        # What this turn cost. A resumed or forked session's total_cost_usd is
        # its whole history's plus this run's, so the runner works the run out
        # (run_cost_usd) and the total is kept beside it.
        'cost_usd': result['run_cost_usd'] if result.get('run_cost_usd') is not None else result.get('total_cost_usd'),
        'session_cost_usd': result.get('total_cost_usd'),
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
    mood = Mood.by_slug_or_404(slug)

    try:
        body = json.loads(request.body)
        session_id = str(uuid.UUID(str(body['session_id'])))
        events = body['events']
        assert isinstance(events, list)
    except (ValueError, KeyError, TypeError, AssertionError):
        return JsonResponse({'error': 'expected {"session_id": <uuid>, "events": [...]}'}, status=400)
    if len(events) > MAX_EVENTS:
        return JsonResponse({'error': f'at most {MAX_EVENTS} events per post'}, status=413)
    events_v1 = body.get('format') == 'events/1'
    if not events_v1 and body.get('harness', 'claude-code') != 'claude-code':
        return JsonResponse({'error': 'a harness other than claude-code posts format events/1'}, status=400)
    # Claude Code's own stream is filed by the Claude Code importer, as from
    # the agent whose key posted it (re, on Anthropic's models: the poller's
    # Harnesses); any agent can post events/1. Either way, one the record
    # knows: a key alone makes nobody.
    if not ThinkingEntity.objects.filter(name=agent).exists():
        return JsonResponse({'error': f'no entity {agent}'}, status=400)

    # Launched for this Mood: it belongs here, whatever else might be guessed.
    if MoodSession.mood_for(session_id) != mood:
        mood.claim(session_id)
    # Whether the runner woke it with full tools: an agent it mentions may be
    # woken with full tools too (api_mentions, mood_poller.trusted).
    if isinstance(body.get('full_tools'), bool):
        MoodSession.objects.filter(session_id=session_id).exclude(full_tools=body['full_tools']).update(
            full_tools=body['full_tools'])

    if events_v1:
        return stream_events(mood, session_id, agent, body)

    from django.utils import timezone
    now_iso = timezone.now().isoformat()
    lines = [line for line in (stream_line(e, now_iso) for e in events) if line]
    imported, skipped, errors = (import_lines(lines, source=SOURCE, username=agent, assistant=agent) if lines
                                 else (0, 0, []))
    # The effort the runner launched the turn at. Stream events don't carry
    # it, and the transcript that does may never reach a watcher (the turn
    # ran in another container), so the runner says it with each post.
    effort = str(body.get('effort') or '')
    if re.fullmatch(r'[a-z]{2,12}', effort):
        Message.objects.filter(session_id=session_id, sender_id=agent, model_backend__isnull=False,
                               effort__isnull=True).update(effort=effort)

    finished = False
    for event in events:
        if isinstance(event, dict) and event.get('type') == 'result':
            finish_turn(mood, session_id, agent, event)
            finished = True

    return JsonResponse({'imported': imported, 'skipped': skipped, 'errors': errors[:10], 'finished': finished})


def stream_events(mood, session_id, agent, body):
    """A post in memory-lane's own format (docs/RECORD_EVENTS.md)."""
    from importers_and_parsers.events_v1 import Turn
    from .views import scrub_texts

    try:
        forked_from = str(uuid.UUID(str(body['forked_from']))) if body.get('forked_from') else None
    except ValueError:
        return JsonResponse({'error': 'forked_from: a session uuid'}, status=400)
    # The scrubber, when one is configured, reads every event as text; if it
    # can't be reached, tool results keep their link and lose their output.
    texts, scrubbed = scrub_texts([json.dumps(e) for e in body['events']])
    events = []
    for text in texts:
        try:
            events.append(json.loads(text))
        except (TypeError, ValueError):
            events.append(None)  # reported by the importer, as any malformed event
    turn = Turn(agent=agent, mood=mood, session_id=session_id, harness=str(body.get('harness') or '')[:50] or None,
                forked_from=forked_from, source=SOURCE, keep_tool_output=scrubbed)
    imported, skipped, ignored, errors = turn.take(events)
    if turn.finished:
        finish_turn(mood, session_id, agent, turn.finished)
    return JsonResponse({'imported': imported, 'skipped': skipped, 'ignored': ignored, 'errors': errors[:10],
                         'finished': turn.finished is not None})


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


_SSH_NAME = re.compile(r'^[a-z0-9][a-z0-9_-]{0,39}$')
_SSH_KEY = re.compile(r'^SHA256:[A-Za-z0-9+/]{43}$')


@csrf_exempt  # authenticated by the access key, not a browser session
@require_POST
def api_ssh(request):
    """A server's SSH router says someone came in.

    Body: {"server": "hunter", "who": "<whose key it was, or 'someone'>",
    "into": "<whose container, if not their own>", "how": "a shell" |
    "tmux magenta" | "a command", "as": "<the account, e.g. root>", "key":
    "<SHA256:... of a key nobody is known by>"}. Said in #general
    (services/access.py), every time.
    """
    from .services import access, servers

    key = getattr(settings, 'MOOD_ACCESS_KEY', '')
    if not key:
        return JsonResponse({'error': 'no access key is configured'}, status=503)
    if not hmac.compare_digest(request.headers.get('Authorization', '').encode(), f'Bearer {key}'.encode()):
        return JsonResponse({'error': 'unauthorized'}, status=401)
    try:
        body = json.loads(request.body)
        server, who = str(body['server']), str(body['who'])
        into, how = str(body.get('into') or ''), str(body.get('how') or '')[:60]
        account, key = str(body.get('as') or ''), str(body.get('key') or '')
    except (ValueError, KeyError, TypeError):
        return JsonResponse({'error': 'expected {"server": ..., "who": ...}'}, status=400)
    if (server not in servers.NAMES or not _SSH_NAME.match(who) or (into and not _SSH_NAME.match(into))
            or (account and not _SSH_NAME.match(account)) or (key and not _SSH_KEY.match(key))):
        return JsonResponse({'error': f'server: one of {servers.NAMES}; who, into, as: account names; '
                                      f'key: SHA256:<fingerprint>'}, status=400)
    row = access.came_in_by_ssh(who, server, into=into, how=how, account=account if account != who else '', key=key)
    return JsonResponse({'announced': row is not None}, status=201 if row is not None else 200)


@require_GET
def api_servers(request):
    """Every server: whether it answers (checked at most once a minute), and its newest redeploy.

    'page' is the Moods page's code as this server has it now: a page opened
    before a redeploy sees it differ and offers a reload.
    """
    from .services import servers
    from .views_moods import page_version
    return JsonResponse({'servers': servers.status(), 'page': page_version()})


VENDOR_NAME = re.compile(r'[A-Za-z0-9._\-]{1,60}')
MAX_VENDORS, MAX_MODELS = 200, 20_000


def clean_vendors(vendors):
    """A runner's report of what its harness can run, checked and trimmed to
    [{'name', 'usable', 'models': [[id, context], ...]}]; ValueError if it isn't one."""
    if not isinstance(vendors, list) or len(vendors) > MAX_VENDORS:
        raise ValueError(f'vendors: a list of at most {MAX_VENDORS}')
    cleaned, total = [], 0
    for vendor in vendors:
        if not isinstance(vendor, dict) or not VENDOR_NAME.fullmatch(str(vendor.get('name', ''))):
            raise ValueError('each vendor: {"name": letters, digits, dots and dashes, ...}')
        models = []
        for model in vendor.get('models') or []:
            model_id, context = (model + [None])[:2] if isinstance(model, list) else (model, None)
            if not isinstance(model_id, str) or not 0 < len(model_id) <= 160 or any(c.isspace() for c in model_id):
                raise ValueError(f"{vendor['name']}: a model id is a string without spaces")
            models.append([model_id, context if isinstance(context, int) and context > 0 else None])
        total += len(models)
        if total > MAX_MODELS:
            raise ValueError(f'at most {MAX_MODELS} models in all')
        cleaned.append({'name': vendor['name'], 'usable': vendor.get('usable') is True, 'models': models})
    return cleaned


@csrf_exempt  # a POST is authenticated by the runner's key; reading is open, like the Moods
def api_agent_models(request, name):
    """What `name`'s harness can run, for the model menu: GET it, or (its
    runner, with its key) POST it.

    Body: {"harness": "pi 1.1.0", "vendors": [{"name": "anthropic",
    "usable": true, "models": [["claude-opus-5-5", 1000000], ...]}, ...]}.
    `usable`: the keys on the agent's machine reach that vendor. Which keys
    those are is never sent, only whether there is one.
    """
    from .models import HarnessModels, ThinkingEntity
    if request.method == 'GET':
        found = HarnessModels.objects.filter(agent_id=name).first()
        if found is None:
            return JsonResponse({'error': f'{name} has reported no models'}, status=404)
        return JsonResponse({'agent': name, 'harness': found.harness, 'vendors': found.vendors,
                             'reported_at': found.reported_at.isoformat()})
    if request.method != 'POST':
        return JsonResponse({'error': 'GET or POST'}, status=405)
    if not getattr(settings, 'MOOD_RUNNER_KEYS', {}):
        return JsonResponse({'error': 'no runners are configured'}, status=503)
    if runner_agent(request) != name:
        return JsonResponse({'error': 'unauthorized'}, status=401)
    agent = ThinkingEntity.objects.filter(name=name).first()
    if agent is None:
        return JsonResponse({'error': f'no entity {name}'}, status=400)
    try:
        body = json.loads(request.body)
        harness = str(body.get('harness') or '')[:60]
        vendors = clean_vendors(body.get('vendors'))
    except (ValueError, AttributeError, TypeError) as e:
        return JsonResponse({'error': str(e) or 'expected {"harness": ..., "vendors": [...]}'}, status=400)
    HarnessModels.objects.update_or_create(agent=agent, defaults={'harness': harness, 'vendors': vendors})
    return JsonResponse({'agent': name, 'vendors': len(vendors), 'models': sum(len(v['models']) for v in vendors)})
