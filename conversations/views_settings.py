"""The settings page and its API: how each agent carries itself in each Motion.

Reading is public, like the rest of the record. Changing a setting takes a
signed-in person (a device) and the CSRF check; every change is a new row,
so the page also shows who changed what, when, and why.
"""

import json

from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_http_methods

from .models import Motion, Setting, ThinkingEntity
from .services import motion_auth
from .services import settings as knobs


def settings_state():
    agents = list(ThinkingEntity.objects.filter(is_biological_human=False).order_by('name')
                  .values_list('name', flat=True))
    motions = [{'slug': m.slug, 'title': m.title or m.slug} for m in Motion.objects.order_by('slug')]
    rows = list(Setting.objects.exclude(key__in=knobs.MODERATION_KEYS))
    current = knobs.latest(rows)
    return {
        'knobs': {k: {'default': d, 'help': h} for k, (d, h) in knobs.KNOBS.items()},
        'global_knobs': {k: {'default': d, 'help': h} for k, (d, h) in knobs.GLOBAL_KNOBS.items()},
        'efforts': knobs.EFFORTS,
        'agents': agents,
        'motions': motions,
        # Every explicitly set slot, as it stands; anything absent is inherited.
        'set': [knobs.describe(row) for row in current.values()],
        # What each agent ends up with in each Motion, after inheritance.
        'resolved': {m['slug']: {a: knobs.resolve(m['slug'], a, rows=rows) for a in agents} for m in motions},
        'global': {k: knobs.global_value(k) for k in knobs.GLOBAL_KNOBS},
        'history': [knobs.describe(row) for row in Setting.objects.order_by('-created_at')[:100]],
        'scram': knobs.scram(),
    }


@ensure_csrf_cookie
@require_GET
def settings_page(request):
    device = motion_auth.device_for(request)
    return render(request, 'conversations/motion_settings.html', {'viewer': device.entity_id if device else ''})


@require_http_methods(['GET', 'POST'])
def api_settings(request):
    if request.method == 'GET':
        return JsonResponse(settings_state())

    device = motion_auth.device_for(request)
    if device is None:
        return JsonResponse({'error': 'sign in to change settings'}, status=401)
    if not device.entity.is_biological_human:
        return JsonResponse({'error': 'settings are for the people in a Motion to change'}, status=403)
    try:
        body = json.loads(request.body)
        key = str(body['key'])
    except (ValueError, KeyError, TypeError):
        return JsonResponse({'error': 'expected {"key": ..., "value": ..., "motion": slug|null, "agent": name|null}'},
                            status=400)
    motion = agent = None
    if body.get('motion'):
        motion = Motion.objects.filter(slug=body['motion']).first()
        if motion is None:
            return JsonResponse({'error': f"no Motion {body['motion']}"}, status=400)
    if body.get('agent'):
        agent = ThinkingEntity.objects.filter(name=body['agent'], is_biological_human=False).first()
        if agent is None:
            return JsonResponse({'error': f"no agent {body['agent']}"}, status=400)
    try:
        row = knobs.change(key, body.get('value'), motion=motion, agent=agent, by=device.entity,
                           note=body.get('note', ''))
    except knobs.Invalid as e:
        return JsonResponse({'error': str(e)}, status=400)
    return JsonResponse(knobs.describe(row), status=201)
