#!/usr/bin/env python3
"""Make a recovery key pair for magenta's sealed copies (conversations/services/sealing.py).

    python3 scripts/recovery_key.py

Prints the private half (keep it offline: a password manager, paper -- not
the vault, not any server) and the public half (the vault, as
memory_lane_recovery_public_key, becomes MOOD_RECOVERY_PUBLIC_KEY). Needs only
the `cryptography` package.
"""

import importlib.util
import pathlib

spec = importlib.util.spec_from_file_location(
    'sealing', pathlib.Path(__file__).resolve().parent.parent / 'conversations' / 'services' / 'sealing.py')
sealing = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sealing)

private, public = sealing.new_pair()
print('Private half -- keep offline, never on a server:')
print(f'  {private}')
print('Public half -- the vault, as memory_lane_recovery_public_key:')
print(f'  {public}')
