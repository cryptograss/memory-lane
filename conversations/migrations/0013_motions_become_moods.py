from django.db import migrations


def forward(apps, schema_editor):
    """Every Motion becomes a Mood with the same slug, title, description, start
    and block height; every message, claimed session, setting and read mark that
    pointed at it points at the Mood. One UPDATE per Mood per table."""
    Motion = apps.get_model('conversations', 'Motion')
    Mood = apps.get_model('conversations', 'Mood')
    for motion in Motion.objects.all():
        mood = Mood.objects.create(slug=motion.slug, title=motion.title, description=motion.description,
                                   eth_blockheight=motion.eth_blockheight)
        Mood.objects.filter(pk=mood.pk).update(created_at=motion.created_at)  # auto_now_add set it to now
        for model in ('Message', 'MotionSession', 'Setting', 'ReadMark'):
            apps.get_model('conversations', model).objects.filter(motion_id=motion.slug).update(mood_id=mood.pk)


def backward(apps, schema_editor):
    Motion = apps.get_model('conversations', 'Motion')
    Mood = apps.get_model('conversations', 'Mood')
    for mood in Mood.objects.all():
        Motion.objects.update_or_create(slug=mood.slug, defaults={
            'title': mood.title, 'description': mood.description, 'eth_blockheight': mood.eth_blockheight})
        for model in ('Message', 'MotionSession', 'Setting', 'ReadMark'):
            apps.get_model('conversations', model).objects.filter(mood_id=mood.pk).update(motion_id=mood.slug)


class Migration(migrations.Migration):
    """Motions become Moods (the name people use), keyed by id so a Mood can be renamed."""

    dependencies = [
        ('conversations', '0012_moods_alongside_motions'),
    ]

    operations = [
        migrations.RunPython(forward, backward),
    ]
