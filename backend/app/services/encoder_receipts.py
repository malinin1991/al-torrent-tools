"""Read-only recovery of modern Kensetsu receipts, never repeats a POST."""
from datetime import timedelta

from sqlalchemy import or_, select

from app.db.models import EncoderAutoItem, EncoderSubmission, EncoderSubmissionItem
from app.services import encoder_history
from app.services.automatic_encoder import lock, settle_item
from app.services.video_kensetsu import is_video_kensetsu_enabled, request_receipt
from app.utils.datetime_fmt import utcnow


async def recover_receipts(factory, *, limit=20):
    with factory() as db:
        if not is_video_kensetsu_enabled(db):
            return
        lock(db)
        now = utcnow()
        groups = db.scalars(select(EncoderSubmission).where(
            EncoderSubmission.request_started_at.is_not(None),
            EncoderSubmission.payload_json != {},
            EncoderSubmission.receipt_next_at <= now,
            EncoderSubmission.items.any(EncoderSubmissionItem.status.in_(("sending", "unknown")))
        ).order_by(EncoderSubmission.receipt_next_at).limit(limit)).all()
        targets = [(g.id, g.base_url, g.request_id) for g in groups]
        for group in groups:
            group.receipt_next_at = now + timedelta(seconds=600)
        db.commit()
    for gid, base_url, rid in targets:
        try:
            response = await request_receipt(base_url, rid)
            error = None
        except Exception as exc:
            response = None
            error = str(encoder_history.safe_json(exc))
        with factory() as db:
            lock(db)
            group = encoder_history.get_submission(db, gid)
            if not group:
                continue
            now = utcnow()
            group.receipt_checked_at = group.updated_at = now
            group.receipt_error = error
            group.receipt_next_at = now + timedelta(seconds=300)
            if response:
                code, body = response["status_code"], response["body"]
                group.recovery_json = encoder_history.safe_json(response)
                if not isinstance(body, dict) or body.get("request_id") != rid:
                    group.receipt_error = "Квитанция отсутствует или не подтверждает ID запроса; проверьте Kensetsu"
                    # A lost database or a legacy server cannot authorize a retry.
                    group.receipt_next_at = None
                elif code == 202 and body.get("request_status") == "processing":
                    group.finished_at = None
                    group.expires_at = now + timedelta(seconds=600)
                    for item in group.items:
                        if item.status == "unknown":
                            item.status, item.error = "sending", None
                    raw = response.get("retry_after")
                    delay = int(raw) if str(raw).isdigit() else 30
                    group.receipt_next_at = now + timedelta(seconds=max(2, min(delay, 300)))
                elif code == 202:
                    group.receipt_error = "Приём прерван в Kensetsu; требуется ручная проверка"
                    group.receipt_next_at = None
                elif code in (200, 207, 400, 403, 404, 422):
                    from app.services.encoder_submission import check_source_conflicts
                    check_source_conflicts(group)
                    original, original_code, original_error = group.response_json, group.http_status, group.error
                    for item in group.items:
                        if item.status in ("sending", "unknown"):
                            item.status, item.error = "sending", None
                    if code in (200, 207):
                        encoder_history.settle_response(group, code, body)
                    else:
                        for item in group.items:
                            if item.status == "sending":
                                item.status = "rejected"
                                item.error = str(encoder_history.safe_json(body.get("error", body)))
                        group.finished_at = now
                    group.response_json, group.http_status, group.error = original, original_code, original_error
                    group.receipt_next_at = None
                    encoder_history.safe_json(body)
                else:
                    group.receipt_error = f"Квитанция не подтверждена: HTTP {code}"
            if group.receipt_error:
                for item in group.items:
                    if item.status == "sending":
                        item.status, item.error = "unknown", group.receipt_error
                group.finished_at = group.finished_at or now
            if group.auto_item_id:
                item = db.get(EncoderAutoItem, group.auto_item_id)
                if item:
                    settle_item(db, item, group)
            db.commit()
