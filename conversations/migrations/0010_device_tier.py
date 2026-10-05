from django.db import migrations, models


class Migration(migrations.Migration):
    """A device's tier: enrolled by SSH key (every device so far) or by PickiPedia sign-in."""

    dependencies = [
        ('conversations', '0009_settings'),
    ]

    operations = [
        migrations.AddField(
            model_name='device',
            name='tier',
            field=models.CharField(choices=[('key', 'SSH key'), ('wiki', 'PickiPedia')], default='key', max_length=10),
        ),
    ]
