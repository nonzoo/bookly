"""Public storefront fields and the cancellation-policy snapshot.

free_cancellation_hours moves from Business (read live) onto Booking
(snapshotted), so editing the policy cannot retroactively change the deal
existing customers agreed to. The three-step dance below (nullable add,
backfill, tighten) is how a non-null column is added to a table that may
already contain bookings.
"""
from django.db import migrations, models


def backfill_free_cancellation_hours(apps, schema_editor):
    Booking = apps.get_model("bookings", "Booking")
    Business = apps.get_model("bookings", "Business")
    for business_id, hours in Business.objects.values_list("pk", "free_cancellation_hours"):
        Booking.objects.filter(business_id=business_id).update(
            free_cancellation_hours=hours
        )


def noop(apps, schema_editor):
    """Reversal: the column is dropped by AlterField/RemoveField ordering."""


class Migration(migrations.Migration):

    dependencies = [
        ("bookings", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="business",
            name="description",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="business",
            name="phone",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
        migrations.AddField(
            model_name="business",
            name="logo_url",
            field=models.URLField(blank=True, default="", max_length=500),
        ),
        migrations.AddField(
            model_name="business",
            name="brand_color",
            field=models.CharField(blank=True, default="", max_length=7),
        ),
        migrations.AddConstraint(
            model_name="business",
            constraint=models.CheckConstraint(
                condition=models.Q(("brand_color", ""))
                | models.Q(("brand_color__regex", "^#[0-9A-Fa-f]{6}$")),
                name="business_brand_color_hex",
            ),
        ),
        migrations.AddField(
            model_name="booking",
            name="free_cancellation_hours",
            field=models.PositiveSmallIntegerField(null=True),
        ),
        migrations.RunPython(backfill_free_cancellation_hours, noop),
        migrations.AlterField(
            model_name="booking",
            name="free_cancellation_hours",
            field=models.PositiveSmallIntegerField(),
        ),
    ]
