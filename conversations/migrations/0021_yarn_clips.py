import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('conversations', '0020_sealed_copies'),
    ]

    operations = [
        migrations.CreateModel(
            name='YarnClip',
            fields=[
                ('clip', models.UUIDField(primary_key=True, serialize=False)),
                ('kept_at', models.DateTimeField(auto_now_add=True)),
                ('media', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='yarn_clips',
                                            to='conversations.media')),
            ],
            options={'db_table': 'yarn_clips'},
        ),
    ]
