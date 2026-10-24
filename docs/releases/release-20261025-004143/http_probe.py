"""Migrate an isolated PG16 DB, seed a release, run real uvicorn HTTP probes."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import httpx
from sqlalchemy import inspect, text
from sqlalchemy.dialects.postgresql import JSONB

subprocess.run([sys.executable, 'scripts/migrate_with_lock.py'], check=True, stdout=sys.stderr)
from alembic.config import Config
from alembic.script import ScriptDirectory
from app.db.models import BackofficeTorrentCache, Release, TorrentArchive, TorrentFile, TrackedRelease
from app.db.session import SessionLocal
from app.providers.anilibria.backoffice import normalize_torrent
from app.services.backoffice_torrent_cache import BackofficeTorrentCacheStore
from app.utils.datetime_fmt import utcnow

cache_time = utcnow()
uploaded = '2026-10-24T12:22:13+00:00'
cache_release = 10318
cache_torrent = 40515
provider = 'https://validation-admin.test'
resource = {'id': {'value': cache_torrent}, 'title': 'WEB-DL 1080p AVC 1-3', 'fields': [
    {'attribute': 'release', 'component': 'belongs-to-field', 'belongsToId': cache_release},
    {'attribute': 'description', 'component': 'text-field', 'value': '1-3'},
    {'attribute': 'updated_at', 'component': 'date-time-field', 'value': uploaded},
    {'attribute': 'ComputedField', 'component': 'text-field', 'name': 'Размер торрента', 'value': '1.25 GB'},
]}
detail = {'id': {'value': cache_torrent}, 'fields': resource['fields'] + [
    {'attribute': 'hash', 'component': 'text-field', 'value': 'A' * 40},
    {'attribute': 'type', 'component': 'select-field', 'value': 'WEB-DL'},
    {'attribute': 'quality', 'component': 'select-field', 'value': '1080p'},
    {'attribute': 'codec', 'component': 'select-field', 'value': 'x264/AVC'},
]}
normalized = normalize_torrent(detail)
assert normalized['hash'] == 'a' * 40 and normalized['codec']['label'] == 'AVC'

with SessionLocal() as db:
    migration = db.execute(text('SELECT version_num FROM alembic_version')).scalar_one()
    assert migration == ScriptDirectory.from_config(Config('alembic.ini')).get_current_head()
    if os.environ.get('EXPECTED_MIGRATION'):
        assert migration == os.environ['EXPECTED_MIGRATION']
    assert int(db.execute(text('SHOW server_version_num')).scalar_one()) // 10000 == 16
    inspector = inspect(db.connection())
    assert inspector.get_pk_constraint('backoffice_torrent_cache')['constrained_columns'] == ['provider_key', 'release_id', 'torrent_id']
    columns = {item['name']: item for item in inspector.get_columns('backoffice_torrent_cache')}
    assert isinstance(columns['payload_json']['type'], JSONB)
    assert columns['fetched_at']['type'].timezone is False
    assert db.execute(text('SELECT count(*) FROM backoffice_torrent_cache')).scalar_one() == 0
    db.add(Release(release_id=10317, release_alias='hotaru-no-yomeiri', title='Свадьба светлячков', source='site', source_checked_at=utcnow()))
    db.add(TrackedRelease(release_id=10317, release_alias='hotaru-no-yomeiri', title='Свадьба светлячков'))
    db.add(TorrentArchive(info_hash='0' * 40, torrent_id=40514, release_id=10317, release_alias='hotaru-no-yomeiri', anime_name='Свадьба светлячков', torrent_description='1-3', torrent_type='WEB-DL 1080p AVC', quality_json={'type': {'value': 'WEB-DL'}, 'quality': {'value': '1080p'}, 'codec': {'label': 'AVC'}}, file_path='/tmp/test.torrent', api_created_at=utcnow()))
    db.add(TorrentFile(torrent_id=40514, info_hash='0' * 40, release_id=10317, relative_path='Hotaru no Yomeiri/03.mkv', size=100, ui_status='new'))
    db.add(Release(release_id=cache_release, release_alias='geo-only-validation', title='Geo-only validation', source='backoffice', source_checked_at=cache_time, is_hidden=False, is_blocked_by_copyrights=False, is_blocked_by_geo=True))
    db.add(TorrentArchive(info_hash='a' * 40, torrent_id=cache_torrent, release_id=cache_release, release_alias='geo-only-validation', anime_name='Geo-only validation', torrent_description='1-3', torrent_type='WEB-DL 1080p AVC', quality_json=normalized, file_path='/tmp/geo-test.torrent', api_created_at=cache_time, backoffice_trackers=True, notification_baseline=True))
    cache = BackofficeTorrentCacheStore(db, provider, now=lambda: cache_time)
    cache.remember(resource, cache_release, normalized)
    db.commit()

# Reopen a real Session to prove persistence, provider isolation, defensive copies,
# and that the cache does not change the archive/SLA clocks while reading it.
with SessionLocal() as db:
    cache = BackofficeTorrentCacheStore(db, provider, now=lambda: cache_time)
    restored = cache.lookup(resource, cache_release)
    assert restored == normalized
    restored['description'] = 'caller mutation'
    assert cache.lookup(resource, cache_release) == normalized
    assert BackofficeTorrentCacheStore(db, 'https://other-validation-admin.test', now=lambda: cache_time).lookup(resource, cache_release) is None
    row = db.get(BackofficeTorrentCache, (cache.provider_key, cache_release, cache_torrent))
    assert row.fetched_at == cache_time and row.fetched_at.tzinfo is None
    assert db.execute(text('SELECT api_created_at FROM torrent_archive WHERE release_id=:rid'), {'rid': cache_release}).scalar_one() == cache_time

paths = [
    '/health/live', '/', '/releases?search=hotaru-no-yomeiri',
    '/releases/live?search=hotaru-no-yomeiri', '/jobs', '/jobs/live',
    '/jobs/catalog/live', '/pipeline', '/pipeline/live', '/archive', '/archive/live',
    '/hevc-workbench', '/hevc-workbench/live', '/automatic-encoder',
    '/automatic-encoder/live?release_id=10317', '/settings', '/encoder-history',
    '/encoder-history/live', '/static/automatic_encoder.js',
    '/static/vendor/htmx-1.9.12.min.js',
    '/releases?search=geo-only-validation', '/releases/live?search=geo-only-validation',
]
with tempfile.TemporaryFile(mode='w+') as log:
    proc = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8000'], stdout=log, stderr=subprocess.STDOUT)
    try:
        with httpx.Client(base_url='http://127.0.0.1:8000', timeout=45) as client:
            for _ in range(200):
                if proc.poll() is not None:
                    raise RuntimeError('uvicorn exited before readiness')
                try:
                    ready = client.get('/health/live')
                    if ready.status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                time.sleep(0.1)
            else:
                raise RuntimeError('uvicorn readiness timed out')
            checks = []
            for path in paths:
                response = client.get(path)
                assert response.status_code == 200, (path, response.status_code, response.text[:1200])
                if path.startswith('/releases'):
                    if 'geo-only-validation' in path:
                        assert 'Geo-only validation' in response.text and 'Геоблок' in response.text, path
                        assert 'Источник: админка, нет на сайте' in response.text, path
                    else:
                        assert 'Свадьба светлячков' in response.text, path
                checks.append({'path': path, 'status': response.status_code, 'sha256': hashlib.sha256(response.content).hexdigest()})
        print(json.dumps({'ok': True, 'migration': migration, 'postgres_major': 16, 'seed_release': 10317, 'geo_only_seed_release': cache_release, 'cache_roundtrip': True, 'cache_provider_isolation': True, 'cache_naive_utc': True, 'cache_preserves_archive_sla_clock': True, 'requests': checks, 'external_network_blocked': True}, ensure_ascii=False, indent=2))
    except BaseException:
        log.seek(0)
        print(log.read()[-20000:], file=sys.stderr)
        raise
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
