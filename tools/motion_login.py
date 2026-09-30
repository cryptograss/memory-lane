#!/usr/bin/env python3
"""Get a login link for writing into Motions, vouched for by your SSH key.

Signs a fresh challenge from memory-lane with your SSH key (ssh-keygen -Y
sign, the same mechanism git uses for signed commits) and prints a one-time
link, plus a QR code if `qrencode` is installed. Open the link on the device
you want to write from -- a phone works -- and confirm.

Your key must be the one hunter's inventory lists for you.

    python3 motion_login.py --name justin
    python3 motion_login.py --name skyler --key ~/.ssh/id_rsa

Standard library only, so it runs anywhere Python and OpenSSH do.
"""

import argparse
import getpass
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

DEFAULT_BASE = 'https://memory-lane.maybelle.cryptograss.live'


def call(url, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json',
                                                              'User-Agent': 'motion-login'})
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


def sign(challenge, key, namespace):
    with tempfile.TemporaryDirectory() as tmp:
        message = os.path.join(tmp, 'challenge')
        with open(message, 'w') as f:
            f.write(challenge)
        # ssh-keygen may ask for the key's passphrase on the terminal.
        subprocess.run(['ssh-keygen', '-Y', 'sign', '-f', key, '-n', namespace, message], check=True,
                       stdout=subprocess.DEVNULL)
        with open(message + '.sig') as f:
            return f.read()


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--name', default=os.environ.get('MOTION_NAME') or getpass.getuser(),
                        help='Your name in the record (default: $MOTION_NAME or your login name)')
    parser.add_argument('--key', default=default_key(), help='SSH private key (default: ~/.ssh/id_ed25519, …)')
    parser.add_argument('--base', default=os.environ.get('MEMORY_LANE_URL', DEFAULT_BASE))
    args = parser.parse_args()
    if not args.key:
        sys.exit('No SSH key found; pass --key.')
    if not shutil.which('ssh-keygen'):
        sys.exit('ssh-keygen not found; install OpenSSH.')

    base = args.base.rstrip('/')
    offer = call(f'{base}/api/auth/challenge/')
    try:
        signature = sign(offer['challenge'], args.key, offer['namespace'])
    except subprocess.CalledProcessError:
        sys.exit('ssh-keygen could not sign with that key.')
    result = call(f'{base}/api/auth/enroll/', {'name': args.name, 'challenge': offer['challenge'],
                                                'signature': signature})

    url = result['url']
    minutes = result.get('expires_in', 900) // 60
    print(f"\nWrite as {result['name']}: open this on the device you want, within {minutes} minutes.\n")
    if shutil.which('qrencode'):
        subprocess.run(['qrencode', '-t', 'ansiutf8', url])
    print(url + '\n')


if __name__ == '__main__':
    main()
