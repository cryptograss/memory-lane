"""Who may write into a Motion: SSH keys enroll devices.

Reading Motions is public. Writing is tied to a person, and the people
who talk with the agents already have an identity the team maintains:
the SSH public key hunter's inventory lists for each of them. So:

1. On a machine holding that key, `tools/motion_login.py` fetches a
   challenge, signs it with `ssh-keygen -Y sign` (the mechanism git uses
   for signed commits) under the namespace below, and posts it back.
2. memory-lane checks the signature against MOTION_ALLOWED_SIGNERS, an
   OpenSSH allowed_signers file generated from that inventory, and
   answers with a one-time login link (printed, and as a QR code).
3. Opening the link on any browser -- a phone included -- and confirming
   enrolls that browser as a Device: a long-lived, revocable token in an
   HttpOnly cookie. Confirming is a button, not the GET itself, so a link
   preview can't spend the code.

No passwords, no third party, nothing new to secure beyond the keys we
already have; and writing works from a phone, which an SSH tunnel never
would.
"""

import hashlib
import os
import secrets
import subprocess
import tempfile
from datetime import timedelta

from django.conf import settings
from django.core import signing
from django.utils import timezone

NAMESPACE = 'magenta-motions'
CHALLENGE_MAX_AGE = 300
CODE_LIFETIME = timedelta(minutes=15)
COOKIE = 'motion_device'
COOKIE_AGE = 365 * 24 * 3600


def digest(secret):
    return hashlib.sha256(secret.encode()).hexdigest()


def new_challenge():
    """A signed, expiring string for the client to sign with its SSH key."""
    return signing.dumps({'nonce': secrets.token_hex(16)}, salt=NAMESPACE)


def challenge_is_fresh(challenge):
    try:
        signing.loads(challenge, salt=NAMESPACE, max_age=CHALLENGE_MAX_AGE)
        return True
    except signing.BadSignature:
        return False


def allowed_signers_path():
    return getattr(settings, 'MOTION_ALLOWED_SIGNERS', '') or os.environ.get('MOTION_ALLOWED_SIGNERS', '')


def signature_is_valid(name, challenge, signature):
    """True if `signature` over `challenge` was made by `name`'s listed key."""
    path = allowed_signers_path()
    if not path or not os.path.exists(path):
        return False
    with tempfile.NamedTemporaryFile('w', suffix='.sig', delete=False) as f:
        f.write(signature)
        sig_path = f.name
    try:
        result = subprocess.run(
            ['ssh-keygen', '-Y', 'verify', '-f', path, '-I', name, '-n', NAMESPACE, '-s', sig_path],
            input=challenge, capture_output=True, text=True, timeout=10)
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        os.unlink(sig_path)


def issue_login_code(entity):
    from conversations.models import LoginCode
    code = secrets.token_urlsafe(32)
    LoginCode.objects.create(code_hash=digest(code), entity=entity,
                             expires_at=timezone.now() + CODE_LIFETIME)
    return code


def redeem_login_code(code, label=''):
    """(Device, token) for a live code, spending it; or (None, None)."""
    from conversations.models import Device, LoginCode
    now = timezone.now()
    spent = LoginCode.objects.filter(code_hash=digest(code), used_at__isnull=True,
                                     expires_at__gt=now).update(used_at=now)
    if not spent:
        return None, None
    login = LoginCode.objects.select_related('entity').get(code_hash=digest(code))
    token = secrets.token_urlsafe(32)
    device = Device.objects.create(entity=login.entity, label=label[:100], token_hash=digest(token))
    return device, token


def code_is_live(code):
    from conversations.models import LoginCode
    return LoginCode.objects.filter(code_hash=digest(code), used_at__isnull=True,
                                    expires_at__gt=timezone.now()).select_related('entity').first()


def device_for(request):
    """The live Device behind this request's cookie, or None."""
    from conversations.models import Device
    token = request.COOKIES.get(COOKIE)
    if not token:
        return None
    device = (Device.objects.select_related('entity')
              .filter(token_hash=digest(token), revoked_at__isnull=True).first())
    if device and (device.last_used_at is None or timezone.now() - device.last_used_at > timedelta(hours=1)):
        Device.objects.filter(pk=device.pk).update(last_used_at=timezone.now())
    return device
