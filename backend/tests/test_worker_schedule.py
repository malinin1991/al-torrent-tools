"""Расписание worker: ежедневный full_sync в 08:00."""

from apscheduler.triggers.cron import CronTrigger

from app.services.job_catalog import FULL_SYNC_DAILY_HOUR, FULL_SYNC_DAILY_MINUTE


def test_full_sync_daily_cron_trigger() -> None:
    trigger = CronTrigger(hour=FULL_SYNC_DAILY_HOUR, minute=FULL_SYNC_DAILY_MINUTE)
    fields = {field.name: str(field) for field in trigger.fields}
    assert fields["hour"] == str(FULL_SYNC_DAILY_HOUR)
    assert fields["minute"] == str(FULL_SYNC_DAILY_MINUTE)
