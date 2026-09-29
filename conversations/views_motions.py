"""Read-only public view of Motions.

Served by memory-lane because memory-lane is where the record lives. This
is a projection: nothing here writes, and nothing a reader does can change
the conversation.
"""

from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_GET

from .models import Message, Motion, ThinkingEntity
from .services.motion_view import (
    MACHINERY_SENDERS, is_wrapper, known_names, mentions_in, motion_payload,
    prose, turn_payload, turns,
)


@require_GET
def motions_page(request, slug=None):
    if slug is not None and not Motion.objects.filter(slug=slug).exists():
        raise Http404
    return render(request, 'conversations/motions.html', {'initial_slug': slug or ''})


@require_GET
def api_motions(request):
    """Every Motion, most recently active first."""
    payloads = [motion_payload(m) for m in Motion.objects.all()]
    payloads.sort(key=lambda p: p['last_at'] or '', reverse=True)
    return JsonResponse({'motions': payloads})


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
    })


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
        Message.objects.filter(motion__isnull=False)
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
