"""Writing into Motions: device enrollment by SSH key, and the say endpoint.

See services/motion_auth.py for the design. Everything here that changes
anything is a POST: enrollment is authenticated by an SSH signature, the
rest by a device cookie plus Django's CSRF check.
"""

import json
import time
import uuid
from datetime import timedelta

from django.conf import settings
from django.http import HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .models import Message, Motion, ThinkingEntity
from .services import motion_auth
from .services.redaction import redact

MAX_CHARS = 20_000
PER_MINUTE = 20
WEB_SOURCE = 'motion-web'


@require_GET
def api_challenge(request):
    return JsonResponse({'challenge': motion_auth.new_challenge(), 'namespace': motion_auth.NAMESPACE})


@csrf_exempt  # authenticated by the SSH signature, not a browser session
@require_POST
def api_enroll(request):
    """Trade a signed challenge for a one-time login link."""
    try:
        body = json.loads(request.body)
        name, challenge, signature = body['name'].lower(), body['challenge'], body['signature']
    except (ValueError, KeyError, AttributeError):
        return JsonResponse({'error': 'expected name, challenge, signature'}, status=400)

    if not motion_auth.challenge_is_fresh(challenge):
        return JsonResponse({'error': 'challenge expired; fetch a new one'}, status=400)
    entity = ThinkingEntity.objects.filter(name=name).first()
    if entity is None or not motion_auth.signature_is_valid(name, challenge, signature):
        return JsonResponse({'error': 'signature not accepted'}, status=403)

    code = motion_auth.issue_login_code(entity)
    url = request.build_absolute_uri(f'/motions/login/{code}/')
    return JsonResponse({'url': url, 'name': name, 'expires_in': int(motion_auth.CODE_LIFETIME.total_seconds())})


@ensure_csrf_cookie
@require_http_methods(['GET', 'POST'])
def login_page(request, code):
    """GET asks; POST enrolls this browser. A link preview only ever GETs."""
    login = motion_auth.code_is_live(code)
    if request.method == 'GET':
        return render(request, 'conversations/motion_login.html',
                      {'name': login.entity_id if login else None}, status=200 if login else 410)

    device, token = motion_auth.redeem_login_code(code, label=request.POST.get('label', ''))
    if device is None:
        return render(request, 'conversations/motion_login.html', {'name': None}, status=410)
    response = HttpResponseRedirect('/motions/')
    response.set_cookie(motion_auth.COOKIE, token, max_age=motion_auth.COOKIE_AGE,
                        httponly=True, secure=not settings.DEBUG, samesite='Lax')
    return response


@require_GET
def api_me(request):
    device = motion_auth.device_for(request)
    return JsonResponse({'name': device.entity_id if device else None})


@require_POST
def api_logout(request):
    device = motion_auth.device_for(request)
    if device:
        type(device).objects.filter(pk=device.pk).update(revoked_at=timezone.now())
    response = JsonResponse({'ok': True})
    response.delete_cookie(motion_auth.COOKIE)
    return response


@require_POST
def api_say(request, slug):
    """Post a turn into a Motion as the device's person."""
    device = motion_auth.device_for(request)
    if device is None:
        return JsonResponse({'error': 'sign in to write'}, status=401)
    motion = get_object_or_404(Motion, slug=slug)

    try:
        text = json.loads(request.body).get('text', '')
    except (ValueError, AttributeError):
        return JsonResponse({'error': 'expected {"text": ...}'}, status=400)
    text = (text or '').strip()
    if not text:
        return JsonResponse({'error': 'nothing to say'}, status=400)
    if len(text) > MAX_CHARS:
        return JsonResponse({'error': f'longer than {MAX_CHARS} characters'}, status=400)

    recent = Message.objects.filter(sender=device.entity, source_file=WEB_SOURCE,
                                    created_at__gt=timezone.now() - timedelta(minutes=1)).count()
    if recent >= PER_MINUTE:
        return JsonResponse({'error': 'slow down'}, status=429)

    text, _ = redact(text)
    message = Message.objects.create(
        id=uuid.uuid4(), sender=device.entity, content=text, motion=motion,
        timestamp=int(time.time() * 1000), source_file=WEB_SOURCE,
    )
    return JsonResponse({'id': str(message.id)}, status=201)
