import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('conversations', '0021_yarn_clips'),
    ]

    operations = [
        migrations.AddField(
            model_name='yarnclip',
            name='still',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                    related_name='yarn_stills', to='conversations.media'),
        ),
    ]
