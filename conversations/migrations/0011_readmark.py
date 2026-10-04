from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    """How far each person has read in each Mood, kept on the server so every device agrees."""

    dependencies = [
        ('conversations', '0010_device_tier'),
    ]

    operations = [
        migrations.CreateModel(
            name='ReadMark',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('seen_at', models.DateTimeField()),
                ('entity', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='read_marks',
                                             to='conversations.thinkingentity')),
                ('motion', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='read_marks',
                                             to='conversations.motion')),
            ],
            options={'db_table': 'read_marks'},
        ),
        migrations.AddConstraint(
            model_name='readmark',
            constraint=models.UniqueConstraint(fields=('entity', 'motion'), name='one_read_mark_per_person_per_mood'),
        ),
    ]
