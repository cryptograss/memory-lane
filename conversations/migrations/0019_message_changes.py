import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('conversations', '0018_media_license'),
    ]

    operations = [
        migrations.CreateModel(
            name='MessageChange',
            fields=[
                ('message', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, primary_key=True,
                                                 related_name='change', serialize=False, to='conversations.message')),
                ('kind', models.CharField(max_length=10)),
                ('by', models.CharField(max_length=50)),
                ('at', models.DateTimeField(auto_now=True, db_index=True)),
                ('reached', models.JSONField(blank=True, default=list)),
                ('mood', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='message_changes',
                                           to='conversations.mood')),
            ],
            options={'db_table': 'message_changes'},
        ),
    ]
