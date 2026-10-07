"""What happens when something lands in the record."""

import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Message

logger = logging.getLogger(__name__)


@receiver(post_save, sender=Message)
def handoffs_carried(sender, instance, created, **kwargs):
    """An agent's ```handoff block, taken to its Mood (services/handoff.py). Never in the way of the save."""
    if not created or not instance.mood_id:
        return
    try:
        from .services import handoff
        handoff.from_message(instance)
    except Exception:  # noqa: BLE001
        logger.exception('a handoff in %s could not be made', instance.id)
