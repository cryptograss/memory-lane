"""Read-only public view of Motions.

Served by memory-lane because memory-lane is where the record lives. This
is a projection: nothing here writes, and nothing a reader does can change
the conversation.
"""

from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_GET

from .models import Message, Motion
from .services.motion_view import motion_payload, turn_payload, turns


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

    payload = [turn_payload(msg, text) for msg, text in turns(motion, after=after)]
    return JsonResponse({
        'motion': motion_payload(motion),
        'turns': payload,
    })
