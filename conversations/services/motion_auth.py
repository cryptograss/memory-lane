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
# A device unused this long has timed out: it writes nothing until it's
# renewed (`magenta.sh renew <its name>`, signed with the person's key) or
# replaced. Used, it never times out; lost, it does on its own.
DEVICE_IDLE_LIMIT = timedelta(days=30)


def digest(secret):
    return hashlib.sha256(secret.encode()).hexdigest()


def new_challenge():
    """A signed, expiring string for the client to sign with its SSH key."""
    return signing.dumps({'nonce': secrets.token_hex(16)}, salt=NAMESPACE)


def signed_message(challenge, origin, purpose='login'):
    """What a client signs: the challenge, bound to the origin it was talking to.

    Signing the bare challenge let any server a client was pointed at (a
    tampered preview, say) fetch a challenge from production, have the
    client sign it, and relay the signature: a device in someone else's
    name. Bound to the origin, a relayed signature names the wrong server.
    tools/motion_login.py builds the same string. An admin's signature
    names its action instead of 'login' (views_admin.py), so it can't be
    replayed as any other.
    """
    return f'{NAMESPACE} {purpose}\n{origin.lower().rstrip("/")}\n{challenge}'


def origin_of(request):
    return f'{request.scheme}://{request.get_host()}'


def challenge_is_fresh(challenge):
    try:
        signing.loads(challenge, salt=NAMESPACE, max_age=CHALLENGE_MAX_AGE)
        return True
    except signing.BadSignature:
        return False


def allowed_signers_path():
    return getattr(settings, 'MOTION_ALLOWED_SIGNERS', '') or os.environ.get('MOTION_ALLOWED_SIGNERS', '')


def signature_is_valid(name, message, signature):
    """True if `signature` over `message` was made by `name`'s listed key."""
    path = allowed_signers_path()
    if not path or not os.path.exists(path):
        return False
    with tempfile.NamedTemporaryFile('w', suffix='.sig', delete=False) as f:
        f.write(signature)
        sig_path = f.name
    try:
        result = subprocess.run(
            ['ssh-keygen', '-Y', 'verify', '-f', path, '-I', name, '-n', NAMESPACE, '-s', sig_path],
            input=message, capture_output=True, text=True, timeout=10)
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        os.unlink(sig_path)


def signer_of(message, signature):
    """The name whose listed key made `signature` over `message`, or None.

    The key says who you are: allowed_signers maps each key to a person, as
    hunter's inventory does, so nobody has to type their name.
    """
    path = allowed_signers_path()
    if not path or not os.path.exists(path):
        return None
    with tempfile.NamedTemporaryFile('w', suffix='.sig', delete=False) as f:
        f.write(signature)
        sig_path = f.name
    try:
        found = subprocess.run(
            ['ssh-keygen', '-Y', 'find-principals', '-f', path, '-n', NAMESPACE, '-s', sig_path],
            capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        os.unlink(sig_path)
    names = found.stdout.split() if found.returncode == 0 else []
    # find-principals only matches the key; verify proves it signed this message.
    if len(names) == 1 and signature_is_valid(names[0], message, signature):
        return names[0]
    return None


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


def code_state(code):
    """What became of a login link: ('live', LoginCode), ('used', {'at', 'name',
    'label'}), ('expired', None), or ('unknown', None) -- never issued, or cut
    short when it was copied. The page says which, so someone whose second
    look at a link finds it spent can tell they're already in."""
    from conversations.models import Device, LoginCode
    login = LoginCode.objects.filter(code_hash=digest(code)).select_related('entity').first()
    if login is None:
        return 'unknown', None
    if login.used_at is None:
        return ('live', login) if login.expires_at > timezone.now() else ('expired', None)
    # The device it made was created as it was spent (redeem_login_code).
    device = (Device.objects.filter(entity=login.entity, created_at__gte=login.used_at - timedelta(seconds=5),
                                    created_at__lte=login.used_at + timedelta(seconds=5))
              .order_by('created_at').first())
    return 'used', {'at': login.used_at, 'name': login.entity_id, 'label': (device.label if device else '').strip()}


def device_state(device, now=None):
    """'live', 'timed out' or 'revoked'."""
    now = now or timezone.now()
    if device.revoked_at:
        return 'revoked'
    return 'timed out' if now - (device.last_used_at or device.created_at) > DEVICE_IDLE_LIMIT else 'live'


def device_for(request):
    """The live Device behind this request's cookie, or None (none, revoked, or timed out)."""
    from conversations.models import Device
    token = request.COOKIES.get(COOKIE)
    if not token:
        return None
    device = (Device.objects.select_related('entity')
              .filter(token_hash=digest(token), revoked_at__isnull=True).first())
    if device is None or device_state(device) != 'live':
        return None
    if device.last_used_at is None or timezone.now() - device.last_used_at > timedelta(hours=1):
        Device.objects.filter(pk=device.pk).update(last_used_at=timezone.now())
    return device


def renew_device(entity, label):
    """Bring `entity`'s device of that name back (a timed-out one included); it or None."""
    from conversations.models import Device
    device = (Device.objects.filter(entity=entity, label=label, revoked_at__isnull=True)
              .order_by('-created_at').first())
    if device is None:
        return None
    Device.objects.filter(pk=device.pk).update(last_used_at=timezone.now())
    device.refresh_from_db()
    return device


def attest_message(challenge, origin, text):
    """What `magenta.sh attest` signs: the challenge, its origin, and the statement."""
    return signed_message(challenge, origin, purpose='attest') + '\n' + text


def verify_attestation(name, proof, text):
    """Check a stored attestation again, now: {'signature_valid', 'key_is_current',
    'statement_matches', 'origin'}.

    The signature is checked against the key kept with it (so a key rotated
    since doesn't make an old statement look forged), and separately whether
    that key is still `name`'s; and that what was signed ends with the words
    shown, on the server it names."""
    key = (proof.get('key') or '').strip()
    signed, signature = proof.get('signed') or '', proof.get('signature') or ''
    valid = False
    if key and signed and signature:
        with tempfile.TemporaryDirectory() as d:
            signers, sig = os.path.join(d, 'allowed_signers'), os.path.join(d, 'sig')
            with open(signers, 'w') as f:
                f.write(f'{name} namespaces="{NAMESPACE}" {key}\n')
            with open(sig, 'w') as f:
                f.write(signature)
            try:
                result = subprocess.run(['ssh-keygen', '-Y', 'verify', '-f', signers, '-I', name, '-n', NAMESPACE,
                                         '-s', sig], input=signed, capture_output=True, text=True, timeout=10)
                valid = result.returncode == 0
            except (OSError, subprocess.SubprocessError):
                valid = False
    lines = signed.split('\n')
    return {'signature_valid': valid,
            'key_is_current': bool(key) and public_key_of(name) == ' '.join(key.split()[:2]),
            'statement_matches': len(lines) >= 4 and '\n'.join(lines[3:]) == text,
            'origin': lines[1] if len(lines) > 1 else ''}


def public_key_of(name):
    """The key line allowed_signers holds for `name` (to show beside what it signed)."""
    path = allowed_signers_path()
    if not path or not os.path.exists(path):
        return ''
    with open(path) as f:
        for line in f:
            parts = line.split()
            if parts and parts[0] == name:
                keys = [p for p in parts if p.startswith(('ssh-', 'ecdsa-', 'sk-'))]
                if keys:
                    i = parts.index(keys[0])
                    return ' '.join(parts[i:i + 2])
    return ''
