import argparse
import json
from pathlib import Path
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from app.core.config import settings
from app.db.models import Release, Setting, TorrentArchive, TorrentFile, TorrentPipeline, PipelineEvent

parser = argparse.ArgumentParser()
parser.add_argument('mode', choices=('seed', 'verify'))
parser.add_argument('snapshot', type=Path)
args = parser.parse_args()
engine = create_engine(settings.database_url)
release_id = 100000001
info_hash = 'e' * 40
if args.mode == 'seed':
    with Session(engine) as session:
        assert session.get(Release, release_id) is None
        session.add(Release(release_id=release_id, release_alias='evaluation-fixture', title='Disposable migration fixture', source='backoffice', is_hidden=True, is_abandoned=True, genres_json=['fixture']))
        session.add(Setting(key='evaluation_only', value='preserve'))
        session.add(TorrentArchive(info_hash=info_hash, torrent_id=release_id, release_id=release_id, file_path='/evaluation/fixture.torrent', api_present=False, superseded=True, ignore_hevc=True, backoffice_trackers=True, notification_baseline=True))
        session.add(TorrentFile(info_hash=info_hash, torrent_id=release_id, release_id=release_id, relative_path='fixture/episode-01.mkv', full_path='/evaluation/episode-01.mkv', size=1234, ui_status='changed', media_present=True))
        pipeline=TorrentPipeline(info_hash=info_hash, torrent_id=release_id, release_id=release_id, status='done')
        session.add(pipeline)
        session.flush()
        session.add(PipelineEvent(pipeline_id=pipeline.id, event_type='evaluation', from_status='slave_added', to_status='done', message='Disposable migration fixture', details_json={'preserve': True}))
        session.commit()

queries = {
    'release': 'SELECT row_to_json(t) FROM releases t WHERE release_id=100000001',
    'archive': "SELECT row_to_json(t) FROM torrent_archive t WHERE info_hash='" + info_hash + "'",
    'file': "SELECT row_to_json(t) FROM torrent_files t WHERE info_hash='" + info_hash + "'",
    'pipeline': "SELECT row_to_json(t) FROM torrent_pipeline t WHERE info_hash='" + info_hash + "'",
    'event': "SELECT row_to_json(t) FROM pipeline_events t WHERE event_type='evaluation'",
    'setting': "SELECT row_to_json(t) FROM settings t WHERE key='evaluation_only'",
}
with engine.connect() as conn:
    snapshot = {name: conn.execute(text(query)).scalar_one() for name, query in queries.items()}
    revision = conn.execute(text('SELECT version_num FROM alembic_version')).scalar_one()
    if args.mode == 'seed':
        assert revision == '0024_backoffice_source', revision
        args.snapshot.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True))
        print('Seeded six legacy entity types at revision', revision)
    else:
        assert revision == '0025_encoder_history', revision
        assert snapshot == json.loads(args.snapshot.read_text()), 'Legacy data changed'
        for table in ('encoder_submissions', 'encoder_submission_items'):
            assert conn.execute(text(f'SELECT COUNT(*) FROM {table}')).scalar_one() == 0
        print('Legacy release/archive/file sticky state/pipeline/audit/settings preserved; new tables empty at', revision)
