from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('conversations', '0019_message_changes'),
    ]

    operations = [
        migrations.CreateModel(
            name='SealedCopy',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('message_id', models.UUIDField(db_index=True)),
                ('mood_slug', models.CharField(max_length=100)),
                ('kind', models.CharField(max_length=10)),
                ('by', models.CharField(db_index=True, max_length=50)),
                ('at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('sealed', models.TextField()),
                ('digest', models.CharField(max_length=64)),
            ],
            options={'db_table': 'sealed_copies'},
        ),
    ]
