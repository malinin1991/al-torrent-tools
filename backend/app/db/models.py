from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ExtraUrl(Base):
    __tablename__ = "extra_urls"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    release_alias: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    release_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", index=True)
    params_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    logs: Mapped[list["JobLog"]] = relationship(back_populates="job", cascade="all, delete-orphan")


class JobLog(Base):
    __tablename__ = "job_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    level: Mapped[str] = mapped_column(String(20), nullable=False, default="info")
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    job: Mapped["Job"] = relationship(back_populates="logs")


class SeenTorrent(Base):
    __tablename__ = "seen_torrents"
    __table_args__ = (UniqueConstraint("info_hash", name="uq_seen_torrents_info_hash"),)
    torrent_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    info_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    release_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    processed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ReleaseCheckpoint(Base):
    """Последние markers релиза с AniLibria — чтобы не дергать API без изменений."""

    __tablename__ = "release_checkpoints"
    release_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    api_updated_at: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    api_fresh_at: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    torrents_fingerprint: Mapped[str] = mapped_column(Text, nullable=False, default="")
    processed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class TorrentPipeline(Base):
    __tablename__ = "torrent_pipeline"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    info_hash: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    release_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    torrent_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="discovered")
    master_added_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    slave_added_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class TorrentArchive(Base):
    __tablename__ = "torrent_archive"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    info_hash: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    torrent_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    release_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    release_alias: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    anime_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    category: Mapped[str | None] = mapped_column(String(255), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    torrent_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    torrent_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    quality_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    file_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class CleanupRule(Base):
    __tablename__ = "cleanup_rules"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    tracker_host: Mapped[str] = mapped_column(String(255), nullable=False)
    message_contains: Mapped[str] = mapped_column(String(255), nullable=False)
    include_errored: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    delete_files: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    target_client: Mapped[str] = mapped_column(String(20), nullable=False, default="master")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class QbClient(Base):
    __tablename__ = "qb_clients"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False, default=8080)
    username: Mapped[str] = mapped_column(String(255), nullable=False)
    # Пока plaintext (имя историческое); шифрование не реализовано. Не логировать.
    password_encrypted: Mapped[str] = mapped_column(Text, nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
