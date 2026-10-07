from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('conversations', '0017_push_subscriptions'),
    ]

    operations = [
        migrations.AddField(
            model_name='media',
            name='license',
            field=models.CharField(default='cc-by-sa-4.0', max_length=20),
        ),
    ]
