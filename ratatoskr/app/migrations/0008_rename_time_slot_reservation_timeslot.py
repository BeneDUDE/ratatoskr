# Generated by Django 3.2.12 on 2022-03-07 12:56

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('app', '0007_auto_20220217_2342'),
    ]

    operations = [
        migrations.RenameField(
            model_name='reservation',
            old_name='time_slot',
            new_name='timeslot',
        ),
    ]
