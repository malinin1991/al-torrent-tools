"""Reparse one stored Kensetsu response; dry run by default, no network calls.

Run from backend: python scripts/repair_encoder_receipts.py --submission-id 2
Add --apply only after reviewing the proposed local receipt correction.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.session import SessionLocal
from app.services.encoder_history import get_submission, repair_saved_receipt


def main(argv=None, *, session_factory=SessionLocal) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--submission-id", type=int, required=True)
    parser.add_argument("--apply", action="store_true", help="Commit the local receipt correction")
    args = parser.parse_args(argv)
    if args.submission_id <= 0:
        parser.error("submission-id must be positive")
    with session_factory() as db:
        group = get_submission(db, args.submission_id)
        if group is None:
            parser.error("submission not found")
        repaired = repair_saved_receipt(group)
        result = {"submission_id": group.id, "dry_run": not args.apply,
                  "repaired_file_ids": repaired,
                  "external_job_ids": {str(i.file_id): i.external_job_ids for i in group.items if i.file_id in repaired}}
        if args.apply:
            db.commit()
        else:
            db.rollback()
        print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
