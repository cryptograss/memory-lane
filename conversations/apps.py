from django.apps import AppConfig


class ConversationsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'conversations'

    def ready(self):
        from . import signals  # noqa: F401 -- what happens when something lands in the record
