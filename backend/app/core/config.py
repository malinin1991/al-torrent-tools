from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


def default_torrent_storage_dir() -> str:
    """Канонический default для архива .torrent.

    - Локально: <repo>/data/torrents (совпадает с host-путём volume в compose).
    - В Docker (WORKDIR=/app, parents[3] == /): /app/data/torrents — тот же путь, что volume.
    Переопределяется через TORRENT_STORAGE_DIR / settings.torrent_storage_dir.
    """
    # backend/app/core/config.py → parents[2] = backend root (= /app в контейнере)
    backend_root = Path(__file__).resolve().parents[2]
    repo_root = backend_root.parent
    if repo_root == Path(repo_root.anchor):
        # Контейнер: не уходим в /data/torrents мимо volume.
        return str(backend_root / "data" / "torrents")
    return str(repo_root / "data" / "torrents")


class Settings(BaseSettings):
    app_name: str = "AL Torrent Tools"
    app_env: str = "dev"
    secret_key: str = "change-me"
    # Pending/running без активности (логи) дольше порога → cancelled (reclaim).
    job_stale_minutes: int = 30
    database_url: str = "postgresql+psycopg://altt:altt@postgres:5432/altt"

    anilibria_base_url: str = "https://anilibria.top/api/v1"
    anilibria_fallback_base_url: str = "https://anilibria.top/api/v1"
    anilibria_site_url: str = "https://www.anilibria.top"
    anilibria_bearer_token: str = ""
    anilibria_passkey: str = ""
    anilibria_request_retries: int = 3
    anilibria_retry_delay_ms: int = 750

    scrape_pause_every: int = 10
    scrape_pause_sec: int = 2
    ongoing_interval_sec: int = 120
    cleanup_interval_sec: int = 3600
    # False = только отчёт; True = можно удалять при dry_run=false (кнопка / API).
    cleanup_allow_delete: bool = True
    pipeline_master_min_age_min: int = 5
    # Полная сверка pipeline ↔ master (кнопка / scheduler).
    pipeline_reconcile_interval_sec: int = 600

    torrent_storage_dir: str = default_torrent_storage_dir()
    # Корень медиа AniLibria на диске (save_path qB master). Сканирование только под ним.
    anilibria_media_root: str = "/anilibria"
    # Чанк BLAKE3 (~4 MiB, как в AniLibria_hasher).
    file_hash_chunk_size: int = 4_194_304
    # Параллельное хеширование: 3 ≈ щадящий режим для Unraid (6 data + cache), Ryzen 2700.
    # Диапазон 1–8, настраивается в UI (file_hash_workers).
    file_hash_workers: int = 3

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


settings = Settings()
