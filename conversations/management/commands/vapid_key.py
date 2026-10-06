"""A new VAPID private key for Web Push (conversations/services/push.py).

Run it once, anywhere, and put what it prints in the vault as
memory_lane_webpush_vapid_key; maybelle hands it to memory-lane as
WEBPUSH_VAPID_PRIVATE_KEY. Changing it later unsubscribes every device:
each turns its bell on again.
"""

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Print a new VAPID private key (base64url) for WEBPUSH_VAPID_PRIVATE_KEY.'

    def handle(self, *args, **options):
        from conversations.services.push import new_private_key
        self.stdout.write(new_private_key())
