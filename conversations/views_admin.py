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

ACTIONS = ('kick', 'kick-device', 'ban', 'unban', 'az5', 'lift')
TARGETED = ('kick', 'kick-device', 'ban', 'unban')
# A device by the start of its id, as the devices dialog shows it (8 hex digits).
DEVICE_PREFIX = re.compile(r'^[0-9a-f]{4,32}$')


def admin_purpose(action, target='', device=''):
    """The words an admin's signature covers, besides the server and challenge.
    For kick-device, the device too: a signature for one can't sign out another."""
    return f'admin {action} {target} {device}'.strip()


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
        device = str(body.get('device') or '').lower().replace('-', '')
    except (ValueError, KeyError, TypeError):
        return JsonResponse({'error': 'expected challenge, signature, action (and target)'}, status=400)
    if action not in ACTIONS:
        return JsonResponse({'error': f"action: one of {', '.join(ACTIONS)}"}, status=400)
    if (action in TARGETED) != bool(target) or (target and not re.fullmatch(r'[a-z0-9._-]{1,60}', target)):
        return JsonResponse({'error': f'{action} takes {"a name" if action in TARGETED else "no name"}'}, status=400)
    if (action == 'kick-device') != bool(device) or (device and not DEVICE_PREFIX.match(device)):
        return JsonResponse({'error': 'kick-device takes a device: the start of its id, as the devices dialog shows it'
                             if action == 'kick-device' else f'{action} takes no device'}, status=400)
    if not mood_auth.challenge_is_fresh(challenge):
        return JsonResponse({'error': 'challenge expired; fetch a new one'}, status=400)

    message = mood_auth.signed_message(challenge, mood_auth.origin_of(request), admin_purpose(action, target, device))
    admin = mood_auth.signer_of(message, signature)
    if admin not in admins:
        return JsonResponse({'error': 'not an admin signature'}, status=403)
    admin_entity = ThinkingEntity.objects.filter(name=admin).first()

    result = {'action': action, 'target': target or None, 'by': admin}
    if action == 'kick-device':
        person = ThinkingEntity.objects.filter(name=target).first()
        if person is None:
            return JsonResponse({'error': f'no one named {target}'}, status=404)
        # Only that person's own devices: a mistyped id can't reach anyone else's.
        live = Device.objects.filter(entity=person, revoked_at__isnull=True)
        found = [d for d in live if d.id.hex.startswith(device)]
        if not found:
            return JsonResponse({'error': f'{target} has no signed-in device starting {device}'}, status=404)
        if len(found) > 1:
            return JsonResponse({'error': f'{len(found)} of {target}\'s devices start {device}: give more of its id'},
                                status=400)
        result['devices_signed_out'], _ = sign_out(Device.objects.filter(pk=found[0].pk), LoginCode.objects.none())
        result['device'] = found[0].label or '(unnamed)'
    elif action in ('kick', 'ban'):
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
    # Said in #general, so the room knows an admin acted (services/access.py).
    from .services import access
    if action == 'kick-device':
        access.announce('kicked', target, by=admin, device=result.get('device'))
    elif action in ('kick', 'ban'):
        access.announce('banned' if action == 'ban' else 'kicked', target, by=admin, devices=result['devices_signed_out'])
    elif action == 'unban':
        access.announce('unbanned', target, by=admin)
    elif action == 'az5':
        access.announce('az5', admin, devices=result['devices_signed_out'])
    elif action == 'lift':
        access.announce('lifted', admin)
    result['scram'] = knobs.scram()
    return JsonResponse(result)


def locked_response():
    """The answer to any write while the scram is set, or None if it isn't."""
    scram = knobs.scram()
    if scram is None:
        return None
    return JsonResponse({'error': f"Moods are locked (scram by {scram['by']} at {scram['at'][:16]}Z)",
                         'scram': scram}, status=423)
