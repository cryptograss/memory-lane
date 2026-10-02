#!/usr/bin/env python3
"""Moderate Motions with your SSH key: kick someone out, or stop everything.

    python3 motion_admin.py kick skyler          # sign out every device of theirs
    python3 motion_admin.py kick skyler --ban    # ...and bar their key from signing in
    python3 motion_admin.py unban skyler
    python3 motion_admin.py az5                  # the scram: everyone out, Motions locked, runners still
    python3 motion_admin.py lift                 # the scram off

Your key must be an admin's (memory-lane's MOTION_ADMINS). The signature
covers the action and its name, so it can't be replayed as anything else
(conversations/views_admin.py). Standard library only, like motion_login.py.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE = 'https://memory-lane.maybelle.cryptograss.live'
NAMESPACE = 'magenta-motions'  # fixed here: a server must not choose what your key signs for


def signed_message(challenge, base, purpose):
    """motion_auth.signed_message: the challenge, the server, and what this signature is for."""
    parts = urllib.parse.urlsplit(base)
    return f'{NAMESPACE} {purpose}\n{parts.scheme}://{parts.netloc}'.lower() + f'\n{challenge}'


def call(url, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json',
                                                              'User-Agent': 'motion-admin'})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as e:
        try:
            detail = json.load(e).get('error', '')
        except ValueError:
            detail = ''
        sys.exit(f'memory-lane said {e.code}: {detail}')


def default_key():
    for name in ('id_ed25519', 'id_ecdsa', 'id_rsa'):
        path = os.path.expanduser(f'~/.ssh/{name}')
        if os.path.exists(path):
            return path
    return None


def sign(text, key):
    with tempfile.TemporaryDirectory() as tmp:
        message = os.path.join(tmp, 'challenge')
        with open(message, 'w') as f:
            f.write(text)
        subprocess.run(['ssh-keygen', '-Y', 'sign', '-f', key, '-n', NAMESPACE, message], check=True,
                       stdout=subprocess.DEVNULL)
        with open(message + '.sig') as f:
            return f.read()


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('action', choices=['kick', 'unban', 'az5', 'lift'])
    parser.add_argument('name', nargs='?', help='Whose devices (kick, unban)')
    parser.add_argument('--ban', action='store_true', help='With kick: bar their key from signing in again')
    parser.add_argument('--key', default=default_key(), help='Your SSH private key (default: ~/.ssh/id_ed25519, …)')
    parser.add_argument('--base', default=os.environ.get('MEMORY_LANE_URL', DEFAULT_BASE))
    args = parser.parse_args()
    if args.action in ('kick', 'unban') and not args.name:
        parser.error(f'{args.action} needs a name')
    if args.action in ('az5', 'lift') and args.name:
        parser.error(f'{args.action} takes no name')
    if not args.key:
        sys.exit('No SSH key found; pass --key.')
    if not shutil.which('ssh-keygen'):
        sys.exit('ssh-keygen not found; install OpenSSH.')

    action = 'ban' if args.action == 'kick' and args.ban else args.action
    target = (args.name or '').lower()
    base = args.base.rstrip('/')
    challenge = call(f'{base}/api/auth/challenge/')['challenge']
    purpose = f'admin {action} {target}'.strip()
    try:
        signature = sign(signed_message(challenge, base, purpose), args.key)
    except subprocess.CalledProcessError:
        sys.exit('ssh-keygen could not sign with that key.')
    result = call(f'{base}/api/auth/admin/', {'challenge': challenge, 'signature': signature,
                                              'action': action, 'target': target or None})

    if action in ('kick', 'ban'):
        print(f"{target}: {result.get('devices_signed_out', 0)} device(s) signed out, "
              f"{result.get('links_spent', 0)} login link(s) spent" + ('; barred from signing in' if action == 'ban' else '') + '.')
    elif action == 'unban':
        print(f'{target} may sign in again.')
    elif action == 'az5':
        print(f"AZ5: {result.get('devices_signed_out', 0)} device(s) signed out, {result.get('links_spent', 0)} "
              f"login link(s) spent. Motions are locked and every runner is still until `lift`.")
    elif action == 'lift':
        print('Lifted: Motions are open again; people sign in as usual.')


if __name__ == '__main__':
    main()
