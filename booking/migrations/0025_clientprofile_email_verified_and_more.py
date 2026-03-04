from django.db import migrations, models
from django.utils import timezone


def mark_existing_client_profiles_verified(apps, schema_editor):
    ClientProfile = apps.get_model("booking", "ClientProfile")
    ClientProfile.objects.filter(email_verified=False).update(
        email_verified=True,
        email_verified_at=timezone.now(),
    )


class Migration(migrations.Migration):

    dependencies = [
        ("booking", "0024_client_trainer_notes"),
    ]

    operations = [
        migrations.AddField(
            model_name="clientprofile",
            name="email_verified",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="clientprofile",
            name="email_verified_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(mark_existing_client_profiles_verified, migrations.RunPython.noop),
    ]
