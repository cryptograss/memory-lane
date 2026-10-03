#!/usr/bin/env python3
"""Get a login link for writing into Motions, vouched for by your SSH key.

Signs a fresh challenge from memory-lane with your SSH key (ssh-keygen -Y
sign, the same mechanism git uses for signed commits) and prints a one-time
link, drawn as a QR code too (by `qrencode` if installed, else by the code
below). Open the link on the device you want to write from -- a phone
works, by its camera -- and confirm.

Your key must be the one hunter's inventory lists for you; it also tells
memory-lane who you are, so there is no name to type.

    python3 motion_login.py                      # a link for a new device
    python3 motion_login.py --key ~/.ssh/id_rsa
    python3 motion_login.py renew phone          # bring a timed-out device back, by its name
    python3 motion_login.py attest "I'll bring the PA Saturday."   # signed, into #general

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


# --- a QR code in the terminal, for opening the link on a phone ---------------
# qrencode draws one when it's installed; otherwise this does, so nobody has
# to install anything. Byte mode, error correction level M, the smallest
# version that fits, the mask with the lowest penalty -- after Project
# Nayuki's QR Code generator (MIT), cut down to what one URL needs.

_ECC_PER_BLOCK_M = (None, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26, 30, 22, 22, 24, 24, 28, 28, 26, 26, 26, 26, 28, 28,
                    28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28)
_BLOCKS_M = (None, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5, 5, 8, 9, 9, 10, 10, 11, 13, 14, 16, 17, 17, 18, 20, 21, 23, 25, 26,
             28, 29, 31, 33, 35, 37, 38, 40, 43, 45, 47, 49)
_FORMAT_M = 0  # level M's two format bits


def _raw_modules(ver):
    result = (16 * ver + 128) * ver + 64
    if ver >= 2:
        n = ver // 7 + 2
        result -= (25 * n - 10) * n - 55
        if ver >= 7:
            result -= 36
    return result


def _data_codewords(ver):
    return _raw_modules(ver) // 8 - _ECC_PER_BLOCK_M[ver] * _BLOCKS_M[ver]


def _gf_mul(x, y):
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree):
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 0x02)
    return result


def _rs_remainder(data, divisor):
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _gf_mul(coef, factor)
    return result


def _codewords(data, ver):
    """Data plus error correction, split into blocks and interleaved."""
    blocks_n, ecc_len = _BLOCKS_M[ver], _ECC_PER_BLOCK_M[ver]
    raw = _raw_modules(ver) // 8
    short_n = blocks_n - raw % blocks_n
    short_len = raw // blocks_n
    divisor = _rs_divisor(ecc_len)
    blocks, k = [], 0
    for i in range(blocks_n):
        dat = data[k:k + short_len - ecc_len + (0 if i < short_n else 1)]
        k += len(dat)
        ecc = _rs_remainder(dat, divisor)
        if i < short_n:
            dat = dat + [0]  # a placeholder, skipped below
        blocks.append(dat + ecc)
    result = []
    for i in range(len(blocks[0])):
        for j, block in enumerate(blocks):
            if i != short_len - ecc_len or j >= short_n:
                result.append(block[i])
    return result


def qr_matrix(text):
    """The modules of a QR code for `text`: rows of booleans, True dark."""
    payload = text.encode('utf-8')
    for ver in range(1, 41):
        count_bits = 8 if ver < 10 else 16
        if 4 + count_bits + 8 * len(payload) <= _data_codewords(ver) * 8:
            break
    else:
        raise ValueError('too long for a QR code')
    capacity = _data_codewords(ver) * 8
    bits = [0, 1, 0, 0] + [(len(payload) >> i) & 1 for i in reversed(range(count_bits))]
    for b in payload:
        bits += [(b >> i) & 1 for i in reversed(range(8))]
    bits += [0] * min(4, capacity - len(bits))
    bits += [0] * (-len(bits) % 8)
    pad = 0xEC
    while len(bits) < capacity:
        bits += [(pad >> i) & 1 for i in reversed(range(8))]
        pad ^= 0xEC ^ 0x11
    data = [int(''.join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]

    size = ver * 4 + 17
    modules = [[False] * size for _ in range(size)]
    function = [[False] * size for _ in range(size)]

    def put(x, y, dark):
        modules[y][x] = dark
        function[y][x] = True

    for i in range(size):  # timing
        put(6, i, i % 2 == 0)
        put(i, 6, i % 2 == 0)
    for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):  # finders and their separators
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                x, y = cx + dx, cy + dy
                if 0 <= x < size and 0 <= y < size:
                    put(x, y, max(abs(dx), abs(dy)) not in (2, 4))
    if ver > 1:  # alignment patterns
        n = ver // 7 + 2
        step = 26 if ver == 32 else (ver * 4 + n * 2 + 1) // (n * 2 - 2) * 2
        positions = [6] + [size - 7 - i * step for i in range(n - 1)][::-1]
        for i, ax in enumerate(positions):
            for j, ay in enumerate(positions):
                if (i, j) in ((0, 0), (0, n - 1), (n - 1, 0)):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        put(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)

    def draw_format(mask):
        value = _FORMAT_M << 3 | mask
        rem = value
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        fbits = (value << 10 | rem) ^ 0x5412
        bit = lambda i: (fbits >> i) & 1 != 0
        for i in range(6):
            put(8, i, bit(i))
        put(8, 7, bit(6))
        put(8, 8, bit(7))
        put(7, 8, bit(8))
        for i in range(9, 15):
            put(14 - i, 8, bit(i))
        for i in range(8):
            put(size - 1 - i, 8, bit(i))
        for i in range(8, 15):
            put(8, size - 15 + i, bit(i))
        put(8, size - 8, True)  # the dark module

    draw_format(0)  # reserves the format areas
    if ver >= 7:
        rem = ver
        for _ in range(12):
            rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
        vbits = ver << 12 | rem
        for i in range(18):
            dark = (vbits >> i) & 1 != 0
            a, b = size - 11 + i % 3, i // 3
            put(a, b, dark)
            put(b, a, dark)

    # The codewords, in the zigzag.
    stream = _codewords(data, ver)
    i = 0
    right = size - 1
    while right >= 1:
        if right == 6:
            right = 5
        for vert in range(size):
            for j in range(2):
                x = right - j
                upward = (right + 1) & 2 == 0
                y = size - 1 - vert if upward else vert
                if not function[y][x] and i < len(stream) * 8:
                    modules[y][x] = (stream[i >> 3] >> (7 - (i & 7))) & 1 != 0
                    i += 1
        right -= 2

    masks = (lambda x, y: (x + y) % 2 == 0, lambda x, y: y % 2 == 0, lambda x, y: x % 3 == 0,
             lambda x, y: (x + y) % 3 == 0, lambda x, y: (x // 3 + y // 2) % 2 == 0,
             lambda x, y: x * y % 2 + x * y % 3 == 0, lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
             lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0)

    def apply(mask):
        for y in range(size):
            for x in range(size):
                if not function[y][x] and masks[mask](x, y):
                    modules[y][x] = not modules[y][x]

    def penalty():
        score = 0
        lines = [row[:] for row in modules] + [[modules[y][x] for y in range(size)] for x in range(size)]
        for line in lines:  # runs of five or more, and finder-like patterns
            run, prev = 0, None
            for m in line:
                run = run + 1 if m == prev else 1
                prev = m
                if run == 5:
                    score += 3
                elif run > 5:
                    score += 1
            s = ''.join('1' if m else '0' for m in line)
            for pattern in ('10111010000', '00001011101'):
                score += 40 * sum(1 for k in range(len(s) - 10) if s[k:k + 11] == pattern)
        for y in range(size - 1):  # 2x2 blocks
            for x in range(size - 1):
                if modules[y][x] == modules[y][x + 1] == modules[y + 1][x] == modules[y + 1][x + 1]:
                    score += 3
        dark = sum(sum(row) for row in modules)
        total = size * size
        k = (abs(dark * 20 - total * 10) + total - 1) // total - 1  # how far from half dark, in 5% steps
        return score + k * 10

    best, best_score = 0, None
    for mask in range(8):
        apply(mask)
        draw_format(mask)
        score = penalty()
        if best_score is None or score < best_score:
            best, best_score = mask, score
        apply(mask)  # undo (XOR)
    apply(best)
    draw_format(best)
    return modules


def qr_terminal(text, border=2):
    """`text` as a QR code in half-block characters, dark on light whatever the theme."""
    modules = qr_matrix(text)
    size = len(modules)
    dark = lambda x, y: 0 <= x < size and 0 <= y < size and modules[y][x]
    lines = []
    for y in range(-border, size + border, 2):
        row = ''
        for x in range(-border, size + border):
            top, bottom = dark(x, y), dark(x, y + 1)
            # The upper half block takes the foreground colour, the lower the background.
            row += f"\x1b[{'30' if top else '97'};{'40' if bottom else '107'}m▀"
        lines.append(row + '\x1b[0m')
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('action', nargs='?', default='login', choices=('login', 'renew', 'attest'),
                        help='login (default): a link for a new device; renew NAME: bring a timed-out device '
                             'back; attest "WORDS": post a statement signed with your key into #general')
    parser.add_argument('words', nargs='*', help="renew: the device's name; attest: the statement")
    parser.add_argument('--name', default=os.environ.get('MOTION_NAME'),
                        help='Only needed if one key is listed for several people; the key names you')
    parser.add_argument('--key', default=default_key(), help='SSH private key (default: ~/.ssh/id_ed25519, …)')
    parser.add_argument('--base', default=os.environ.get('MEMORY_LANE_URL', DEFAULT_BASE))
    parser.add_argument('--no-qr', dest='qr', action='store_false', help="Don't draw the link as a QR code")
    args = parser.parse_args()
    if not args.key:
        sys.exit('No SSH key found; pass --key.')
    if not shutil.which('ssh-keygen'):
        sys.exit('ssh-keygen not found; install OpenSSH.')
    words = ' '.join(args.words).strip()
    if args.action in ('renew', 'attest') and not words:
        sys.exit(f'{args.action}: say ' + ("which device (its name, as you gave it at sign-in)" if args.action == 'renew'
                                           else 'what to attest, in quotes'))

    base = args.base.rstrip('/')
    offer = call(f'{base}/api/auth/challenge/')
    challenge = offer['challenge']
    if args.action == 'renew':
        text = signed_message(challenge, base, purpose=f'renew {words}')
    elif args.action == 'attest':
        text = signed_message(challenge, base, purpose='attest') + '\n' + words
    else:
        text = signed_message(challenge, base)
    try:
        signature = sign(text, args.key)
    except subprocess.CalledProcessError:
        sys.exit('ssh-keygen could not sign with that key.')

    if args.action == 'renew':
        result = call(f'{base}/api/auth/renew/', {'challenge': challenge, 'signature': signature, 'label': words})
        print(f"\n{result['name']}'s {result['label']} is signed in again"
              + (f" (until {result['times_out_at'][:10]}, if unused)." if result.get('times_out_at') else '.') + '\n')
        return
    if args.action == 'attest':
        result = call(f'{base}/api/attest/', {'challenge': challenge, 'signature': signature, 'text': words})
        print(f"\nAttested, in #general: {result['url']}\n")
        return

    payload = {'challenge': challenge, 'signature': signature}
    if args.name:
        payload['name'] = args.name
    result = call(f'{base}/api/auth/enroll/', payload)

    url = result['url']
    minutes = result.get('expires_in', 900) // 60
    print(f"\nWrite as {result['name']}: open this on the device you want, within {minutes} minutes.\n")
    if args.qr and sys.stdout.isatty():
        if shutil.which('qrencode'):
            subprocess.run(['qrencode', '-t', 'ansiutf8', url])
        else:
            print(qr_terminal(url) + '\n')
    print(url + '\n')

if __name__ == '__main__':
    main()
