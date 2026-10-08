"""Sealing to the recovery key: kept, but unreadable here.

What an edit or a deletion takes out of a Mood (services/retract.py) is
sealed before it goes: encrypted to a recovery key whose public half the
server holds (settings.MOOD_RECOVERY_PUBLIC_KEY) and whose private half it
never does -- someone keeps it offline. The server can seal; it cannot
unseal. So a pasted secret is gone from everything the server can read, and
a captured account's (or key's) deletions can still be put back, by whoever
holds the private half (manage.py unseal).

A sealed box: a fresh X25519 key for each sealing, its agreement with the
recovery key run through HKDF-SHA256 into a ChaCha20-Poly1305 key. Text,
"mgs1." and then base64url of (the fresh public key, the nonce, the
ciphertext). Keys are "mgrpub1."/"mgrkey1." and base64url of their 32 bytes
(scripts/recovery_key.py makes a pair).
"""

import base64
import os

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

SEALED, PUBLIC, PRIVATE = 'mgs1.', 'mgrpub1.', 'mgrkey1.'
_INFO = b'magenta sealed words v1'


class SealingError(Exception):
    pass


def _b64(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip('=')


def _unb64(text):
    return base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))


def _raw(key):
    return key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def new_pair():
    """(private, public) recovery keys, as text."""
    private = X25519PrivateKey.generate()
    secret = private.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                   serialization.NoEncryption())
    return PRIVATE + _b64(secret), PUBLIC + _b64(_raw(private.public_key()))


def public_key(text):
    if not (text or '').startswith(PUBLIC):
        raise SealingError('not a recovery public key (mgrpub1.)')
    try:
        return X25519PublicKey.from_public_bytes(_unb64(text[len(PUBLIC):]))
    except Exception as e:
        raise SealingError(f'not a recovery public key: {e}')


def _key(shared, ephemeral, recipient):
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=ephemeral + recipient, info=_INFO).derive(shared)


def seal(data, recipient_text):
    """`data` (bytes) sealed to the recovery key: text only the private half opens."""
    recipient = public_key(recipient_text)
    ephemeral = X25519PrivateKey.generate()
    eph_raw, rec_raw = _raw(ephemeral.public_key()), _raw(recipient)
    nonce = os.urandom(12)
    box = ChaCha20Poly1305(_key(ephemeral.exchange(recipient), eph_raw, rec_raw)).encrypt(nonce, data, None)
    return SEALED + _b64(eph_raw + nonce + box)


def unseal(sealed, private_text):
    """The bytes a sealing holds, with the recovery key's private half."""
    if not (private_text or '').startswith(PRIVATE) or not (sealed or '').startswith(SEALED):
        raise SealingError('a recovery private key (mgrkey1.) and something sealed (mgs1.), please')
    private = X25519PrivateKey.from_private_bytes(_unb64(private_text[len(PRIVATE):]))
    raw = _unb64(sealed[len(SEALED):])
    eph_raw, nonce, box = raw[:32], raw[32:44], raw[44:]
    shared = private.exchange(X25519PublicKey.from_public_bytes(eph_raw))
    try:
        return ChaCha20Poly1305(_key(shared, eph_raw, _raw(private.public_key()))).decrypt(nonce, box, None)
    except Exception:
        raise SealingError("that key doesn't open this")
