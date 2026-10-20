"""The hashing in SPEC.md: salted leaves, RFC 6962 trees, two hash functions.

verify_memory.py carries its own copy of this on purpose, so that file alone
is enough to check a bundle; test_commitment.py holds the two to agreement.
"""
import hashlib
import hmac

ALGS = ('sha256', 'sha3_256')
LEAF, NODE = b'\x00', b'\x01'


def h(alg, data):
    return hashlib.new(alg, data).digest()


def salt_for(secret, key):
    """A leaf's salt: HMAC-SHA256 of its key under the keepers' secret."""
    return hmac.new(secret, key.encode('utf-8'), hashlib.sha256).digest()


def leaf_hash(alg, salt, record):
    return h(alg, LEAF + salt + record)


def tree_hash(alg, hashes):
    """RFC 6962 section 2.1, over hashes that are already leaf hashes."""
    def mth(lo, hi):
        n = hi - lo
        if n == 1:
            return hashes[lo]
        k = 1 << ((n - 1).bit_length() - 1)  # the largest power of two smaller than n
        return h(alg, NODE + mth(lo, lo + k) + mth(lo + k, hi))

    return mth(0, len(hashes)) if hashes else h(alg, b'')


def chunk_entry_hash(alg, name, root):
    """A chunk's entry in the tree that gives the commitment's root."""
    return h(alg, LEAF + name.encode('utf-8') + b'\x00' + root)


def root_of_chunks(alg, chunks):
    """chunks: [(name, chunk root bytes)], in any order."""
    return tree_hash(alg, [chunk_entry_hash(alg, name, root)
                           for name, root in sorted(chunks, key=lambda c: c[0].encode('utf-8'))])
