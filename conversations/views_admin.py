"""Moderation, by an admin's SSH key: kick someone out, or stop everything.

For when a sign-in has gone wrong -- a stolen phone, a leaked login link, a
bug of ours:

  kick <name>    every device of theirs signed out, every login link of
                 theirs spent. They can sign in again with their key.
  ban <name>     a kick, and their key can't sign in again until unbanned
                 (or until their key leaves hunter's inventory).
  unban <name>
  az5            the scram: every device signed out, every login link spent,
                 and the Moods locked -- nobody writes, nobody signs in,
                 and every runner wakes nothing -- until an admin lifts it.
  lift           the scram off; people sign in again as usual.

The request is signed like a sign-in (`ssh-keygen -Y sign`), but the
message names the action and its target, so a signature for one can't be
replayed as another. Only names in settings.MOOD_ADMINS may act. Every
action is recorded as a settings row (key 'scram' or 'banned'), so the
settings page shows who did what, when. tools/mood_admin.py is the
client; `magenta.sh kick` and `magenta.sh AZ5` run it.
"""

import json
import re

from django.conf import settings
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import Device, LoginCode, Setting, ThinkingEntity
from .services import mood_auth
from .services import settings as knobs

ACTIONS = ('kick', 'ban', 'unban', 'az5', 'lift')
TARGETED = ('kick', 'ban', 'unban')


def admin_purpose(action, target=''):
    """The words an admin's signature covers, besides the server and challenge."""
    return f'admin {action} {target}'.strip()


def sign_out(devices, codes):
    now = timezone.now()
    return devices.filter(revoked_at__isnull=True).update(revoked_at=now), \
        codes.filter(used_at__isnull=True, expires_at__gt=now).update(used_at=now)


@csrf_exempt  # authenticated by the admin's SSH signature
@require_POST
def api_admin(request):
    from .views_auth import _under_limit

    admins = getattr(settings, 'MOOD_ADMINS', ())
    if not admins:
        return JsonResponse({'error': 'no admins are configured'}, status=503)
    if not _under_limit('admin', 20):
        return JsonResponse({'error': 'too many attempts; wait a minute'}, status=429)
    try:
        body = json.loads(request.body)
        challenge, signature, action = body['challenge'], body['signature'], body['action']
        target = str(body.get('target') or '').lower()
    except (ValueError, KeyError, TypeError):
        return JsonResponse({'error': 'expected challenge, signature, action (and target)'}, status=400)
    if action not in ACTIONS:
        return JsonResponse({'error': f"action: one of {', '.join(ACTIONS)}"}, status=400)
    if (action in TARGETED) != bool(target) or (target and not re.fullmatch(r'[a-z0-9._-]{1,60}', target)):
        return JsonResponse({'error': f'{action} takes {"a name" if action in TARGETED else "no name"}'}, status=400)
    if not mood_auth.challenge_is_fresh(challenge):
        return JsonResponse({'error': 'challenge expired; fetch a new one'}, status=400)

    message = mood_auth.signed_message(challenge, mood_auth.origin_of(request), admin_purpose(action, target))
    admin = mood_auth.signer_of(message, signature)
    if admin not in admins:
        return JsonResponse({'error': 'not an admin signature'}, status=403)
    admin_entity = ThinkingEntity.objects.filter(name=admin).first()

    result = {'action': action, 'target': target or None, 'by': admin}
    if action in ('kick', 'ban'):
        person = ThinkingEntity.objects.filter(name=target).first()
        if person is None:
            return JsonResponse({'error': f'no one named {target}'}, status=404)
        result['devices_signed_out'], result['links_spent'] = sign_out(
            Device.objects.filter(entity=person), LoginCode.objects.filter(entity=person))
        if action == 'ban':
            Setting.objects.create(key='banned', agent=person, value=True, set_by=admin_entity,
                                   note=f'banned by {admin}')
    elif action == 'unban':
        person = ThinkingEntity.objects.filter(name=target).first()
        if person is None:
            return JsonResponse({'error': f'no one named {target}'}, status=404)
        Setting.objects.create(key='banned', agent=person, value=False, set_by=admin_entity,
                               note=f'unbanned by {admin}')
    elif action == 'az5':
        result['devices_signed_out'], result['links_spent'] = sign_out(Device.objects.all(), LoginCode.objects.all())
        Setting.objects.create(key='scram', value=True, set_by=admin_entity, note=f'AZ5 by {admin}')
    elif action == 'lift':
        Setting.objects.create(key='scram', value=False, set_by=admin_entity, note=f'lifted by {admin}')
    result['scram'] = knobs.scram()
    return JsonResponse(result)


def locked_response():
    """The answer to any write while the scram is set, or None if it isn't."""
    scram = knobs.scram()
    if scram is None:
        return None
    return JsonResponse({'error': f"Moods are locked (scram by {scram['by']} at {scram['at'][:16]}Z)",
                         'scram': scram}, status=423)
