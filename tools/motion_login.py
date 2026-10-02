#!/usr/bin/env python3
"""Get a login link for writing into Motions, vouched for by your SSH key.

Signs a fresh challenge from memory-lane with your SSH key (ssh-keygen -Y
sign, the same mechanism git uses for signed commits) and prints a one-time
link, plus a QR code if `qrencode` is installed. Open the link on the device
you want to write from -- a phone works -- and confirm.

Your key must be the one hunter's inventory lists for you; it also tells
memory-lane who you are, so there is no name to type.

    python3 motion_login.py
    python3 motion_login.py --key ~/.ssh/id_rsa

Standard library only, so it runs anywhere Python and OpenSSH do.
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
# Fixed here, never taken from the server: a server must not choose what
# your key signs for (`git` would make it a commit signature).
NAMESPACE = 'magenta-motions'


def signed_message(challenge, base, purpose='login'):
    """The challenge bound to the server it came from (motion_auth.signed_message)."""
    parts = urllib.parse.urlsplit(base)
    return f'{NAMESPACE} {purpose}\n{parts.scheme}://{parts.netloc}'.lower() + f'\n{challenge}'


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


def sign(text, key):
    with tempfile.TemporaryDirectory() as tmp:
        message = os.path.join(tmp, 'challenge')
        with open(message, 'w') as f:
            f.write(text)
        # ssh-keygen may ask for the key's passphrase on the terminal.
        subprocess.run(['ssh-keygen', '-Y', 'sign', '-f', key, '-n', NAMESPACE, message], check=True,
                       stdout=subprocess.DEVNULL)
        with open(message + '.sig') as f:
            return f.read()


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--name', default=os.environ.get('MOTION_NAME'),
                        help='Only needed if one key is listed for several people; the key names you')
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
        signature = sign(signed_message(offer['challenge'], base), args.key)
    except subprocess.CalledProcessError:
        sys.exit('ssh-keygen could not sign with that key.')
    payload = {'challenge': offer['challenge'], 'signature': signature}
    if args.name:
        payload['name'] = args.name
    result = call(f'{base}/api/auth/enroll/', payload)

    url = result['url']
    minutes = result.get('expires_in', 900) // 60
    print(f"\nWrite as {result['name']}: open this on the device you want, within {minutes} minutes.\n")
    if shutil.which('qrencode'):
        subprocess.run(['qrencode', '-t', 'ansiutf8', url])
    print(url + '\n')


if __name__ == '__main__':
    main()
