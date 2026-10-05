from django.db import migrations
from django.utils.text import slugify


def free_slug(Mood, MoodAlias, title, mood):
    """As Mood.free_slug: from the title, and naming no other Mood, now or before a rename."""
    base = slugify(title)[:60].strip('-') or 'mood'
    slug, n = base, 2
    while (Mood.objects.filter(slug=slug).exclude(pk=mood.pk).exists()
           or MoodAlias.objects.filter(slug=slug).exclude(mood=mood).exists()):
        slug, n = f'{base}-{n}', n + 1
    return slug


def forward(apps, schema_editor):
    """Moods renamed before a rename moved the URL get the slug their title
    gives them now; the old slug stays theirs, as an alias."""
    Mood = apps.get_model('conversations', 'Mood')
    MoodAlias = apps.get_model('conversations', 'MoodAlias')
    for mood in Mood.objects.exclude(title='').order_by('pk'):
        slug = free_slug(Mood, MoodAlias, mood.title, mood)
        if slug == mood.slug:
            continue
        MoodAlias.objects.get_or_create(slug=mood.slug, defaults={'mood': mood})
        MoodAlias.objects.filter(slug=slug, mood=mood).delete()  # an old name of its own, taken back
        mood.slug = slug
        mood.save(update_fields=['slug'])


class Migration(migrations.Migration):
    """Slugs follow titles, for the Moods renamed before they did."""

    dependencies = [
        ('conversations', '0015_mood_names_in_the_record'),
    ]

    operations = [
        # Backward leaves the new slugs: the old ones still find their Moods.
        migrations.RunPython(forward, migrations.RunPython.noop),
    ]
