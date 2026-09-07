from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.utils.datetime_fmt import utcnow


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class ExtraUrl(Base):
    __tablename__ = "extra_urls"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    release_alias: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    release_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", index=True)
    params_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    logs: Mapped[list["JobLog"]] = relationship(back_populates="job", cascade="all, delete-orphan")


class JobLog(Base):
    __tablename__ = "job_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    level: Mapped[str] = mapped_column(String(20), nullable=False, default="info")
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    job: Mapped["Job"] = relationship(back_populates="logs")


class SeenTorrent(Base):
    __tablename__ = "seen_torrents"
    __table_args__ = (UniqueConstraint("info_hash", name="uq_seen_torrents_info_hash"),)
    torrent_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    info_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    release_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    processed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ReleaseCheckpoint(Base):
    """Последние markers релиза с AniLibria — чтобы не дергать API без изменений."""

    __tablename__ = "release_checkpoints"
    release_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    api_updated_at: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    api_fresh_at: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    torrents_fingerprint: Mapped[str] = mapped_column(Text, nullable=False, default="")
    processed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Release(Base):
    """Карточка релиза AniLibria (состав, жанры, блокировки) — source of truth для UI.

    Торренты по-прежнему в ``torrent_archive``; ``quality_json`` может дублировать
    жанры/members для обратной совместимости (qB tags, старые строки).
    """

    __tablename__ = "releases"
    release_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    release_alias: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    original_title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    genres_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # None = ещё не синхронизировано с API (fallback на quality_json в UI).
    is_blocked_by_geo: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=None)
    is_blocked_by_copyrights: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True, default=None
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    members: Mapped[list["ReleaseMember"]] = relationship(
        back_populates="release",
        cascade="all, delete-orphan",
        order_by="ReleaseMember.sort_order",
    )


class ReleaseMember(Base):
    """Участник релиза (озвучка, сведение, тайминг, …)."""

    __tablename__ = "release_members"
    __table_args__ = (
        UniqueConstraint(
            "release_id",
            "role",
            "nickname",
            name="uq_release_members_release_role_nickname",
        ),
        Index("ix_release_members_nickname", "nickname"),
        Index("ix_release_members_role", "role"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    release_id: Mapped[int] = mapped_column(
        ForeignKey("releases.release_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # UUID участника из API, если есть.
    api_member_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    role_label: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    nickname: Mapped[str] = mapped_column(String(255), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    release: Mapped["Release"] = relationship(back_populates="members")


class TrackedRelease(Base):
    """Релизы, отслеживаемые для Telegram-уведомлений (/add или чекбокс в UI)."""

    __tablename__ = "tracked_releases"
    release_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    release_alias: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="ui")  # bot|ui
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class TelegramOutbox(Base):
    """Очередь исходящих Telegram-сообщений (ретраи при недоступности API)."""

    __tablename__ = "telegram_outbox"
    __table_args__ = (
        UniqueConstraint("bot_key", "dedupe_key", name="uq_telegram_outbox_bot_dedupe"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pipeline_id: Mapped[int | None] = mapped_column(
        ForeignKey("torrent_pipeline.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    bot_key: Mapped[str] = mapped_column(String(32), nullable=False, default="primary", index=True)
    dedupe_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    chat_id: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class TelegramBotAccess(Base):
    """Заявка Telegram-пользователя или чата на доступ к конкретному боту."""

    __tablename__ = "telegram_bot_access"
    __table_args__ = (
        UniqueConstraint(
            "bot_key",
            "subject_type",
            "telegram_id",
            name="uq_telegram_bot_access_subject",
        ),
        CheckConstraint(
            "subject_type IN ('user', 'chat')",
            name="ck_telegram_bot_access_subject_type",
        ),
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected')",
            name="ck_telegram_bot_access_status",
        ),
        Index(
            "ix_telegram_bot_access_bot_subject_status",
            "bot_key",
            "subject_type",
            "status",
        ),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bot_key: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(16), nullable=False)
    telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class TorrentPipeline(Base):
    __tablename__ = "torrent_pipeline"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    info_hash: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    release_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    torrent_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="discovered")
    # skipped | pending | queued | sent — не блокирует master→slave
    tg_status: Mapped[str] = mapped_column(String(16), nullable=False, default="skipped")
    master_added_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    slave_added_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    slave_completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    events: Mapped[list["PipelineEvent"]] = relationship(
        back_populates="pipeline",
        cascade="all, delete-orphan",
        order_by="PipelineEvent.id",
    )


class PipelineEvent(Base):
    """Audit trail жизненного пути пайплайна (независимо от job_logs)."""

    __tablename__ = "pipeline_events"
    __table_args__ = (
        Index("ix_pipeline_events_pipeline_id_id", "pipeline_id", "id"),
        Index("ix_pipeline_events_created_at", "created_at"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pipeline_id: Mapped[int] = mapped_column(
        ForeignKey("torrent_pipeline.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Без жёсткого FK: job может быть удалён cleanup’ом, в UI — «job удалён».
    job_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    from_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    details_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    pipeline: Mapped["TorrentPipeline"] = relationship(back_populates="events")


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
    # True = торрент сейчас в ответе AniLibria API; False = архивный (снят с раздачи).
    api_present: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    # True = заменён новой версией того же torrent_id (другой info_hash) — храним как историю.
    superseded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    # AVC: не требовать HEVC-пару (фильтры/бейджи missing|overdue|type_mismatch).
    # Сбрасывается при новой версии (supersede → новая строка с default False).
    ignore_hevc: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Дата загрузки версии на AniLibria (max API created_at/updated_at); SLA overdue.
    api_created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class TorrentFile(Base):
    """Ожидаемый состав файлов из .torrent + приоритеты qB master."""

    __tablename__ = "torrent_files"
    __table_args__ = (UniqueConstraint("info_hash", "relative_path", name="uq_torrent_files_hash_path"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    torrent_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    info_hash: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    release_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    file_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    selected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    full_path: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    # Sticky статус для UI: new|ok|changed — не пересчитывается с диска.
    ui_status: Mapped[str] = mapped_column(String(16), nullable=False, default="ok", index=True)
    # Временный overlay «проверка» (.!qB / progress<1 на master / hash_torrent). Не писать в ui_status.
    is_checking: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Complete media на диске (inventory / settle). Не sticky ui_status.
    media_present: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class DiskFileHash(Base):
    """Снимок BLAKE3 на диске (gate size+mtime)."""

    __tablename__ = "disk_file_hashes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    full_path: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    mtime: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    content_hash: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    hash_algo: Mapped[str] = mapped_column(String(32), nullable=False, default="blake3")
    last_checked_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_hashed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class FileChangeEvent(Base):
    """События изменений файлов для UI и Telegram."""

    __tablename__ = "file_change_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    release_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    torrent_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    info_hash: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    relative_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    full_path: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    details_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class CleanupRule(Base):
    __tablename__ = "cleanup_rules"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    tracker_host: Mapped[str] = mapped_column(String(255), nullable=False)
    message_contains: Mapped[str] = mapped_column(String(255), nullable=False)
    include_errored: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    delete_files: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    target_client: Mapped[str] = mapped_column(String(20), nullable=False, default="both")
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


class FileMediaInfo(Base):
    """Метаданные MediaInfo для медиафайлов под ANILIBRIA_MEDIA_ROOT (gate по size+mtime)."""

    __tablename__ = "file_mediainfo"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    full_path: Mapped[str] = mapped_column(Text, nullable=False, unique=True, index=True)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    mtime: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    summary_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    raw_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Вложения контейнера (Matroska AttachedFile): [{name, mime, size_bytes}, ...]
    attachments_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    attachments_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attachments_total_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

