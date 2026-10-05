from django.db import migrations

# What the record itself says, not just the code: where rows came from, and
# the participant the importer files the runner's prompts under.
SOURCES = {
    'motion-web': 'mood-web',
    'motion-attest': 'mood-attest',
    'motion-new': 'mood-new',
    'motion-rename': 'mood-rename',
    'ingest-motion-runner': 'ingest-mood-runner',
}
SENDERS = {'motion-poller': 'mood-poller'}


def rename(apps, sources, senders):
    Message = apps.get_model('conversations', 'Message')
    Participant = apps.get_model('conversations', 'ConversationParticipant')
    for old, new in sources.items():
        Message.objects.filter(source_file=old).update(source_file=new)
    for old, new in senders.items():
        was = Participant.objects.filter(name=old).first()
        if was is None:
            continue
        Participant.objects.get_or_create(name=new, defaults={'participant_type': was.participant_type})
        Message.objects.filter(sender_id=old).update(sender_id=new)
        Message.objects.filter(recipient_id=old).update(recipient_id=new)
        was.delete()


def forward(apps, schema_editor):
    rename(apps, SOURCES, SENDERS)


def backward(apps, schema_editor):
    rename(apps, {v: k for k, v in SOURCES.items()}, {v: k for k, v in SENDERS.items()})


class Migration(migrations.Migration):
    """The record's own words for where rows came from say Mood now."""

    dependencies = [
        ('conversations', '0014_moods_replace_motions'),
    ]

    operations = [
        migrations.RunPython(forward, backward),
    ]
