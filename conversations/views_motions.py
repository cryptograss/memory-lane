"""Read-only public view of Motions.

Served by memory-lane because memory-lane is where the record lives. This
is a projection: nothing here writes, and nothing a reader does can change
the conversation.
"""

from django.conf import settings
from django.db.models import Max
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET

from .models import Message, Motion, ThinkingEntity
from .services import motion_auth
from .services.motion_view import (
    MACHINERY_SENDERS, activity, is_wrapper, known_names, mentions_in, motion_payload,
    prose, turn_payload, turns, wiki_title, wikilinks_in,
)


@ensure_csrf_cookie
@require_GET
def motions_page(request, slug=None):
    if slug is not None and not Motion.objects.filter(slug=slug).exists():
        raise Http404
    device = motion_auth.device_for(request)
    return render(request, 'conversations/motions.html', {
        'initial_slug': slug or '',
        # A preview can pretend to be someone, to show the composer; its
        # database is read-only, so nothing it sends is kept.
        'viewer': device.entity_id if device else getattr(settings, 'PREVIEW_VIEWER', ''),
        'preview_label': getattr(settings, 'PREVIEW_LABEL', ''),
    })


@require_GET
def api_motions(request):
    """Every Motion, most recently active first."""
    payloads = [motion_payload(m) for m in Motion.objects.all()]
    payloads.sort(key=lambda p: p['last_at'] or '', reverse=True)
    people = ThinkingEntity.objects.order_by('name')
    return JsonResponse({'motions': payloads, 'people': [
        {'name': p.name, 'is_human': p.is_biological_human} for p in people]})


@require_GET
def api_motion_turns(request, slug):
    """The readable conversation in one Motion.

    Pass ?after=<message id> to get only what arrived since -- the polling
    contract. An unknown `after` is treated as "everything", so a client
    that has fallen out of sync recovers rather than stalls.
    """
    motion = get_object_or_404(Motion, slug=slug)

    after = None
    after_id = request.GET.get('after')
    if after_id:
        after = Message.objects.filter(id=after_id).first()

    names = known_names()
    payload = [turn_payload(msg, text, names) for msg, text in turns(motion, after=after)]
    return JsonResponse({
        'motion': motion_payload(motion),
        'turns': payload,
        'activity': activity(motion),
    })


@require_GET
def api_motion_sessions(request, slug):
    """Runtime sessions in a Motion, most recently active first.

    ?sender=<name> keeps only sessions in which that entity wrote. This is
    how the poller finds a session to resume when it wakes an agent: the
    Motion is durable, and whichever process last spoke for the agent is
    the one to continue. A session id is a local handle, not a credential.
    """
    motion = get_object_or_404(Motion, slug=slug)
    messages = Message.objects.filter(motion=motion, session_id__isnull=False)
    sender = request.GET.get('sender')
    if sender:
        messages = messages.filter(sender_id=sender.lower())
    sessions = (messages.values('session_id')
                .annotate(last_at=Max('created_at'))
                .order_by('-last_at'))
    return JsonResponse({'motion': motion.slug, 'sessions': [
        {'session_id': str(s['session_id']), 'last_at': s['last_at'].isoformat()} for s in sessions
    ]})


@require_GET
def api_wikilinks(request):
    """Turns in Motions that link to PickiPedia pages with [[...]], oldest first.

    This is what PickiPedia reads to reach Motions without storing them.
    ?page=<title> keeps one page's backlinks. ?since=<iso> starts after a
    moment; pass back `next_since` to page forward. A message's links are
    never split across pages. The cursor is ingest time (created_at), so a
    consumer should overlap its cursor by a few minutes and dedupe on
    (turn, page), and resync fully now and then: motion_assign attaches
    old messages to a Motion without changing their created_at.
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
        Message.objects.filter(motion__isnull=False, is_sidechain=False)
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
            links.append({'page': title, 'motion': msg.motion_id, 'turn': str(msg.id),
                          'sender': msg.sender_id, 'created_at': msg.created_at.isoformat(),
                          'eth_blockheight': msg.eth_blockheight})
    else:
        next_since = None  # read to the end

    return JsonResponse({'links': links, 'next_since': next_since})


@require_GET
def api_mentions(request, name):
    """Recent turns, across all Motions, that mention one thinking entity.

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
    messages = (
        Message.objects.filter(motion__isnull=False, is_sidechain=False)
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
        text = prose(msg.content)
        if not text or is_wrapper(text):
            continue
        if name in mentions_in(text, names):
            found.append({'motion': msg.motion_id, 'turn': turn_payload(msg, text, names)})
            if len(found) >= limit:
                break

    return JsonResponse({'name': name, 'mentions': found})
