import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    """Motions go: everything points at its Mood (0013 copied them across).

    MotionSession is renamed, not recreated, so the claimed sessions keep
    their Moods; motion_sessions becomes mood_sessions.
    """

    dependencies = [
        ('conversations', '0013_motions_become_moods'),
    ]

    operations = [
        migrations.RemoveConstraint(model_name='readmark', name='one_read_mark_per_person_per_mood'),
        migrations.RemoveIndex(model_name='message', name='conversatio_motion__d1f81a_idx'),
        migrations.RemoveIndex(model_name='setting', name='settings_key_713aa4_idx'),
        migrations.RemoveField(model_name='message', name='motion'),
        migrations.RemoveField(model_name='setting', name='motion'),
        migrations.RemoveField(model_name='motionsession', name='motion'),
        migrations.RemoveField(model_name='readmark', name='motion'),
        migrations.DeleteModel(name='Motion'),
        migrations.AlterField(
            model_name='readmark', name='mood',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='read_marks',
                                    to='conversations.mood'),
        ),
        migrations.AlterField(
            model_name='motionsession', name='mood',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='claimed_sessions',
                                    to='conversations.mood'),
        ),
        migrations.RenameModel(old_name='MotionSession', new_name='MoodSession'),
        migrations.AlterModelTable(name='moodsession', table='mood_sessions'),
        migrations.AddIndex(
            model_name='message',
            index=models.Index(fields=['mood', 'created_at'], name='conversatio_mood_id_92f359_idx'),
        ),
        migrations.AddIndex(
            model_name='setting',
            index=models.Index(fields=['key', 'mood', 'agent', 'created_at'], name='settings_key_3e4024_idx'),
        ),
        migrations.AddConstraint(
            model_name='readmark',
            constraint=models.UniqueConstraint(fields=('entity', 'mood'), name='one_read_mark_per_person_per_mood'),
        ),
    ]
