from sqlalchemy.orm import Session


async def run_placeholder(db: Session, job_id: int, params: dict) -> None:
    _ = (db, job_id, params)
    # Бизнес-логика будет добавлена в фазе 1.
