"""Migrate an isolated PG16 DB, seed a release, run real uvicorn HTTP probes."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import httpx
from sqlalchemy import text

subprocess.run([sys.executable, 'scripts/migrate_with_lock.py'], check=True, stdout=sys.stderr)
from alembic.config import Config
from alembic.script import ScriptDirectory
from app.db.models import Release, TorrentArchive, TorrentFile, TrackedRelease
from app.db.session import SessionLocal
from app.utils.datetime_fmt import utcnow

with SessionLocal() as db:
    migration = db.execute(text('SELECT version_num FROM alembic_version')).scalar_one()
    assert migration == ScriptDirectory.from_config(Config('alembic.ini')).get_current_head()
    assert int(db.execute(text('SHOW server_version_num')).scalar_one()) // 10000 == 16
    db.add(Release(release_id=10317, release_alias='hotaru-no-yomeiri', title='Свадьба светлячков', source='site', source_checked_at=utcnow()))
    db.add(TrackedRelease(release_id=10317, release_alias='hotaru-no-yomeiri', title='Свадьба светлячков'))
    db.add(TorrentArchive(info_hash='0' * 40, torrent_id=40514, release_id=10317, release_alias='hotaru-no-yomeiri', anime_name='Свадьба светлячков', torrent_description='1-3', torrent_type='WEB-DL 1080p AVC', quality_json={'type': {'value': 'WEB-DL'}, 'quality': {'value': '1080p'}, 'codec': {'label': 'AVC'}}, file_path='/tmp/test.torrent', api_created_at=utcnow()))
    db.add(TorrentFile(torrent_id=40514, info_hash='0' * 40, release_id=10317, relative_path='Hotaru no Yomeiri/03.mkv', size=100, ui_status='new'))
    db.commit()

paths = [
    '/health/live', '/', '/releases?search=hotaru-no-yomeiri',
    '/releases/live?search=hotaru-no-yomeiri', '/jobs', '/jobs/live',
    '/jobs/catalog/live', '/pipeline', '/pipeline/live', '/archive', '/archive/live',
    '/hevc-workbench', '/hevc-workbench/live', '/automatic-encoder',
    '/automatic-encoder/live?release_id=10317', '/settings', '/encoder-history',
    '/encoder-history/live', '/static/automatic_encoder.js',
    '/static/vendor/htmx-1.9.12.min.js',
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
                    assert 'Свадьба светлячков' in response.text, path
                checks.append({'path': path, 'status': response.status_code, 'sha256': hashlib.sha256(response.content).hexdigest()})
        print(json.dumps({'ok': True, 'migration': migration, 'postgres_major': 16, 'seed_release': 10317, 'requests': checks, 'external_network_blocked': True}, ensure_ascii=False, indent=2))
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
