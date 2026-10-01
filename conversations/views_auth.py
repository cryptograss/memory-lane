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
ENROLL_MAX_BYTES = 64 * 1024
# For everyone together: behind the proxy every client has the same address.
ENROLL_PER_MINUTE = 30


def _under_limit(bucket, per_minute):
    """Count one more attempt in this minute; False once there are too many."""
    from django.core.cache import cache
    key = f'limit:{bucket}:{int(time.time() // 60)}'
    cache.add(key, 0, 120)
    return cache.incr(key) <= per_minute


@require_GET
def api_challenge(request):
    return JsonResponse({'challenge': motion_auth.new_challenge(), 'namespace': motion_auth.NAMESPACE})


@csrf_exempt  # authenticated by the SSH signature, not a browser session
@require_POST
def api_enroll(request):
    """Trade a signed challenge for a one-time login link."""
    # Each attempt runs ssh-keygen twice; keep strangers from making that a load.
    if len(request.body) > ENROLL_MAX_BYTES:
        return JsonResponse({'error': 'too large'}, status=413)
    if not _under_limit('enroll', ENROLL_PER_MINUTE):
        return JsonResponse({'error': 'too many sign-in attempts; wait a minute'}, status=429)
    try:
        body = json.loads(request.body)
        challenge, signature = body['challenge'], body['signature']
        name = (body.get('name') or '').lower()
    except (ValueError, KeyError, AttributeError):
        return JsonResponse({'error': 'expected challenge and signature'}, status=400)

    if not motion_auth.challenge_is_fresh(challenge):
        return JsonResponse({'error': 'challenge expired; fetch a new one'}, status=400)
    message = motion_auth.signed_message(challenge, motion_auth.origin_of(request))
    if name:
        name = name if motion_auth.signature_is_valid(name, message, signature) else ''
    else:
        name = motion_auth.signer_of(message, signature) or ''  # the key says who you are
    entity = ThinkingEntity.objects.filter(name=name).first() if name else None
    if entity is None:
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

    label = getattr(settings, 'DEVICE_LABEL_PREFIX', '') + request.POST.get('label', '')
    device, token = motion_auth.redeem_login_code(code, label=label)
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
    if not isinstance(text, str):
        return JsonResponse({'error': 'expected {"text": ...}'}, status=400)
    text = text.replace('\x00', '').strip()  # Postgres text can't hold NUL
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
