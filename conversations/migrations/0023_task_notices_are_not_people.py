from django.db import migrations


def forward(apps, schema_editor):
    """A background task's notice, fed back into a session as a user turn, was
    filed under the container's owner. It's the harness speaking: system."""
    Message = apps.get_model('conversations', 'Message')
    Participant = apps.get_model('conversations', 'ConversationParticipant')
    people = list(apps.get_model('conversations', 'ThinkingEntity').objects
                  .filter(is_biological_human=True).values_list('name', flat=True))
    from django.db.models import TextField
    from django.db.models.functions import Cast
    # Stored as a JSON string: its text form starts with the quote.
    ids = list(Message.objects.filter(sender_id__in=people).annotate(text=Cast('content', TextField()))
               .filter(text__startswith='"<task-notification>').values_list('id', flat=True))
    if not ids:
        return
    system, _ = Participant.objects.get_or_create(name='system', defaults={'participant_type': 'system'})
    Message.objects.filter(id__in=ids).update(sender=system)


class Migration(migrations.Migration):

    dependencies = [
        ('conversations', '0022_yarn_stills'),
    ]

    operations = [
        migrations.RunPython(forward, migrations.RunPython.noop),
    ]
