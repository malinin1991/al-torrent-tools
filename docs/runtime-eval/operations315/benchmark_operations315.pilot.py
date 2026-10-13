#!/usr/bin/env python3
"""Whole-operation evaluation, real PostgreSQL/native IO; never production services.

The only simulated boundary is FileTracker's external qB inventory. The ASGI
HTTP transport is in-process; real route, Session, SQL, Jinja and response run.
Sources are clean readonly snapshots. All database names are experiment-owned.
"""
from __future__ import annotations
import argparse
import asyncio
from datetime import datetime, timedelta
import gc
import hashlib
import json
import os
from pathlib import Path
import random
import re
import subprocess
import sys
import threading
import time

import benchmark_runtime as common
from benchmark_free_threading import cpu_constraints, gil_state, linux_memory

MODES = ('regular', 'ft_on', 'ft_off')
WRAPPERS = dict(zip(MODES, (
    '/opt/runtime-modes/run_regular.sh', '/opt/runtime-modes/run_t_gil1.sh',
    '/opt/runtime-modes/run_t_default.sh')))
CASES = ('hevc_http', 'mediainfo_real_full', 'mediainfo_real_changed',
         'mediainfo_generated_full', 'mediainfo_generated_changed', 'hash_real')
DBS = {'hevc_http': 'operations_hevc', 'hash_real': 'operations_hash',
       'real': 'operations_media_real', 'generated': 'operations_media_generated'}
MARGIN = .03
BENCH_NOW = datetime(2026, 10, 13, 0, 0)
PG_HOST = 'altt-operations-315-pg-20261013'


def dsn(name):
    return f'postgresql+psycopg://postgres@{PG_HOST}:5432/{name}'


def case_db(case):
    return DBS.get(case) or DBS['real' if '_real_' in case else 'generated']


def configure(args):
    os.environ.update(DATABASE_URL=dsn(case_db(args.child)),
                      ANILIBRIA_MEDIA_ROOT='/bench/media', FILE_HASH_WORKERS='3',
                      TORRENT_STORAGE_DIR='/bench/fixtures/torrents',
                      ANILIBRIA_BASE_URL='http://invalid.invalid',
                      ANILIBRIA_FALLBACK_BASE_URL='http://invalid.invalid',
                      APP_ENV='test', SECRET_KEY='operations-experiment-only')
    sys.path.insert(0, str(args.source))
    # Jinja paths follow production's backend cwd. Snapshot has no .env.
    os.chdir(args.source)


def files(dataset):
    return sorted((Path('/bench/media') / dataset).glob('*.mkv'))


def init_dbs(args):
    import psycopg
    with psycopg.connect(f'host={PG_HOST} user=postgres dbname=postgres', autocommit=True) as conn:
        for name in sorted(set(DBS.values())):
            exists = conn.execute('SELECT 1 FROM pg_database WHERE datname=%s', (name,)).fetchone()
            if not exists:
                conn.execute(f'CREATE DATABASE {name}')
    sys.path.insert(0, str(args.source))
    from sqlalchemy import create_engine, insert, text
    from sqlalchemy.orm import Session
    from app.db.base import Base
    from app.db.models import Release, TorrentArchive, TorrentFile
    manifest = {}
    for name in sorted(set(DBS.values())):
        engine = create_engine(dsn(name), connect_args={'prepare_threshold': None})
        Base.metadata.create_all(engine)
        with engine.begin() as conn:
            conn.execute(text('TRUNCATE ' + ','.join('"'+t.name+'"' for t in Base.metadata.sorted_tables) + ' RESTART IDENTITY CASCADE'))
        if name == DBS['hevc_http']:
            # Eight real pairing scenarios, replicated as distinct releases.
            rows, keys = common.hevc_fixture()
            archives, tracked, releases = [], [], []
            offset = timedelta(0)  # Exact common.hevc_fixture/BENCH_NOW clock, preserve fresh cases.
            for block in range(args.hevc_blocks):
                shift = block * 128
                for rid in range(1, 129):
                    nrid = rid + shift
                    releases.append(dict(release_id=nrid, release_alias=f'bench-{nrid}',
                                         title=f'Benchmark release {nrid:05}', source='site',
                                         genres_json=['Benchmark'], is_abandoned=False))
                for row in rows:
                    values = vars(row).copy()
                    aid = row.id + block * 12800
                    values.update(id=aid, release_id=row.release_id+shift, torrent_id=aid,
                                  info_hash=f'{aid:040x}', file_path=f'/bench/fixtures/torrents/{aid}.torrent',
                                  release_alias=f'bench-{row.release_id+shift}',
                                  anime_name=f'Benchmark release {row.release_id+shift:05}',
                                  created_at=row.created_at+offset, api_created_at=row.api_created_at+offset)
                    archives.append(values)
                    for index, key in enumerate(sorted(keys[row.id])):
                        tracked.append(dict(torrent_id=aid, info_hash=values['info_hash'],
                                            release_id=values['release_id'], relative_path=key,
                                            full_path=f'/bench/media/hevc/{aid}/{key}',
                                            size=123456789, file_index=index, selected=True,
                                            ui_status='new', media_present=True))
            with Session(engine, autoflush=False) as db:
                db.execute(insert(Release), releases)
                db.execute(insert(TorrentArchive), archives)
                for start in range(0, len(tracked), 2000):
                    db.execute(insert(TorrentFile), tracked[start:start+2000])
                db.commit()
            manifest[name] = dict(releases=len(releases), archives=len(archives), files=len(tracked))
        elif name in (DBS['real'], DBS['generated']):
            dataset = 'real' if name == DBS['real'] else 'generated'
            paths = files(dataset)
            assert paths, dataset
            with Session(engine, autoflush=False) as db:
                db.add_all(TorrentFile(torrent_id=1, info_hash='a'*40, release_id=1,
                           relative_path=f'{dataset}/{p.name}', full_path=str(p),
                           size=p.stat().st_size, file_index=i, ui_status='new')
                           for i,p in enumerate(paths))
                db.commit()
            manifest[name] = dict(files=len(paths), bytes=sum(p.stat().st_size for p in paths))
        else:
            manifest[name] = {'files_per_job': len(files('real'))}
        with engine.begin() as conn:
            conn.execute(text('ANALYZE'))
        engine.dispose()
    print(json.dumps({'initialized': manifest, 'cpu_constraints': cpu_constraints()}))


def instrument(engine):
    from sqlalchemy import event
    counts = dict(sql_statements=0, commits=0, native_parses=0, hash_files=0)
    lock = threading.Lock()
    @event.listens_for(engine, 'before_cursor_execute')
    def sql_count(*unused):
        with lock: counts['sql_statements'] += 1
    @event.listens_for(engine, 'commit')
    def commit_count(*unused):
        with lock: counts['commits'] += 1
    def native():
        from pymediainfo import MediaInfo
        original = MediaInfo.parse
        def parse(cls, *a, **kw):
            with lock: counts['native_parses'] += 1
            return original(*a, **kw)
        MediaInfo.parse = classmethod(parse)
        from app.services import file_hasher
        original_hash = file_hasher.hash_file_blake3
        def hash_file(*a, **kw):
            with lock: counts['hash_files'] += 1
            return original_hash(*a, **kw)
        file_hasher.hash_file_blake3 = hash_file
    return counts, native


def metadata_rows(db):
    from sqlalchemy import select
    from app.db.models import FileMediaInfo
    return [dict(path=row.full_path, size=row.file_size, mtime=row.mtime,
                 summary=row.summary_json, raw=row.raw_json, text=row.raw_text,
                 attachments=row.attachments_json,
                 count=row.attachments_count, attachment_bytes=row.attachments_total_bytes)
            for row in db.scalars(select(FileMediaInfo).order_by(FileMediaInfo.full_path))]


def reset_media(db, dataset, changed):
    from sqlalchemy import text
    from app.db.models import Job
    from app.services.mediainfo import upsert_file_mediainfo
    db.execute(text('TRUNCATE file_mediainfo, job_logs, jobs RESTART IDENTITY CASCADE'))
    db.commit()
    if changed:
        # Populate real native reports, then stale only our database gate.
        for p in files(dataset):
            assert upsert_file_mediainfo(db, str(p), force=True) is not None
        db.execute(text('UPDATE file_mediainfo SET mtime = mtime - 60'))
        db.commit()
    job = Job(type='mediainfo_sync', status='running', params_json={})
    db.add(job); db.commit()
    return job.id


def prepare_hash(db):
    from sqlalchemy import text
    from app.db.base import Base
    from app.db.models import Job, TorrentArchive, Release, TorrentPipeline
    db.execute(text('TRUNCATE ' + ','.join('"'+t.name+'"' for t in Base.metadata.sorted_tables) + ' RESTART IDENTITY CASCADE'))
    db.commit()
    record = json.loads(Path('/bench/fixtures/torrents/real.manifest.json').read_text())
    info_hash = record['info_hash']
    db.add(Release(release_id=1, title='Read-only real media benchmark', source='site'))
    db.add(TorrentArchive(info_hash=info_hash, torrent_id=1, release_id=1,
                         file_path='/bench/fixtures/torrents/real.torrent',
                         torrent_type='WEBRip 1080p AVC', torrent_description='1-4',
                         quality_json={'codec': {'value':'h264','label':'AVC'},
                                       'type': {'value':'WEBRip'},'quality':{'value':'1080p'}},
                         api_present=True, notification_baseline=True))
    db.add(TorrentPipeline(info_hash=info_hash, torrent_id=1, release_id=1, status='master_complete'))
    job=Job(type='hash_torrent', status='running', params_json={})
    db.add(job); db.commit()
    return job.id, dict(info_hash=info_hash, torrent_id=1, release_id=1)


def timed(run, counts):
    gc.collect()
    for name in counts: counts[name]=0
    before_mem=linux_memory()
    c0,t0=time.process_time(),time.perf_counter()
    result=run()
    elapsed=time.perf_counter()-t0
    cpu=time.process_time()-c0
    return dict(elapsed_sec=elapsed, cpu_sec=cpu, cpu_average_cores=cpu/elapsed,
                peak_rss_bytes=common.peak_rss_bytes(), **linux_memory(),
                rss_before_work_bytes=before_mem['linux_rss_after_work_bytes'],
                counts=counts.copy()), result


def child(args):
    configure(args)
    initial=gil_state()
    expected=args.mode!='ft_off'
    assert initial==expected, (args.mode, initial)
    if args.child == 'hevc_http':
        # Harness-only fixed domain clock, before service/main imports. No production edit.
        from app.utils import datetime_fmt
        datetime_fmt.utcnow = lambda: BENCH_NOW
    from app.db.session import engine, SessionLocal
    from sqlalchemy import select
    from app.db.models import JobLog, DiskFileHash, TorrentFile, PipelineEvent
    counts,native=instrument(engine)
    result={}
    if args.child=='hevc_http':
        from fastapi.testclient import TestClient
        from app.main import app
        # No context-manager lifespan: no schedules/external services are started.
        client=TestClient(app)
        def request():
            response=client.get('/releases/live?hevc_filter=overdue')
            assert response.status_code==200, response.text[:400]
            ids=re.findall(r'data-sse-key="release-(\d+)"',response.text)
            assert len(ids)==30, len(ids)
            total=re.search(r'Релизов: (\d+)', response.text)
            # Wall-clock display strings vary; stable HTML semantic output is asserted.
            return dict(release_ids=ids, total=total.group(1) if total else None,
                        status='200', html_sha256=hashlib.sha256(response.content).hexdigest(),
                        body_bytes=len(response.content))
        result['cold_request'],output=timed(request,counts)
        for _ in range(2): request()
        result['warm_request'],warm_output=timed(request,counts)
        assert output == warm_output, 'Cold/warm full HTML differs under fixed domain clock'
        output.pop('body_bytes',None)
        client.close()
    else:
        native()
        with SessionLocal() as db:
            if args.child=='hash_real':
                from app.jobs.hash_torrent import run_hash_torrent
                from app.services.file_tracker import FileTrackerService
                # ONLY external qB inventory is deterministic; native reads/hash/DB remain real.
                count=len(files('real'))
                FileTrackerService._qb_paths_and_priorities=lambda self, info_hash: (
                    '/bench/media','/bench/media/real', {i:1 for i in range(count)},
                    {i:1.0 for i in range(count)})
                job_id,params=prepare_hash(db)
                result['operation'],_=timed(lambda: asyncio.run(run_hash_torrent(db,job_id,params)),counts)
                db.expire_all()
                disk=list(db.scalars(select(DiskFileHash).order_by(DiskFileHash.full_path)))
                sticky=list(db.scalars(select(TorrentFile.ui_status)))
                reports=metadata_rows(db)
                logs=list(db.scalars(select(JobLog.message)))
                assert counts['hash_files']==count, counts
                assert counts['native_parses']==count and len(reports)==count, counts
                assert len(disk)==count and set(sticky)=={'new'}, (len(disk),sticky)
                assert any(f'hashed={count}, gated=0, errors=0' in s for s in logs), logs[-4:]
                events=list(db.scalars(select(PipelineEvent).order_by(PipelineEvent.id)))
                assert {'hash_progress','hash_done','ui_status'} <= {e.event_type for e in events}, [e.event_type for e in events]
                audit=[dict(type=e.event_type, from_status=e.from_status,to_status=e.to_status,details=e.details_json) for e in events]
                output=dict(metadata=reports, hashes=[(r.full_path,r.content_hash,r.size) for r in disk],
                            ui_status=sticky, hashed=count, parsed=count, audit=audit)
            else:
                from app.jobs.mediainfo_sync import run_mediainfo_sync
                dataset='real' if '_real_' in args.child else 'generated'
                changed=args.child.endswith('changed')
                job_id=reset_media(db,dataset,changed)
                params={'mode':'incremental' if changed else 'full'}
                result['operation'],_=timed(lambda:asyncio.run(run_mediainfo_sync(db,job_id,params)),counts)
                db.expire_all()
                reports=metadata_rows(db)
                logs=list(db.scalars(select(JobLog.message)))
                n=len(files(dataset))
                assert counts['native_parses']==n and len(reports)==n,(n,counts,len(reports),logs[-3:])
                assert any(f'обновлено={n}' in s and 'ошибок=0' in s for s in logs), logs[-3:]
                output=dict(metadata=reports, parsed=n)
    assert gil_state()==expected, 'extension enabled GIL during operation'
    result.update(case=args.child,mode=args.mode,gil_initial=initial,gil_after_work=gil_state(),
                  checksum=common.digest(output))
    print(json.dumps(result,sort_keys=True,default=str))


def controller(args):
    combinations=[(version,mode) for version in ('baseline','optimized') for mode in MODES]
    # Cyclic six-order blocks; reverse second block. Every combination occupies
    # every position twice in12 rounds, and BEFORE/AFTER order changes.
    orders=[]
    for r in range(args.rounds):
        order=combinations[r%6:]+combinations[:r%6]
        if (r//6)%2: order=list(reversed(order))
        orders.append(order)
    payload=dict(protocol_version=1, rounds=args.rounds, noise_margin_pct=3,
                 note='Whole real DB operations; warm host file cache, no OS cache eviction',
                 cpu_constraints=cpu_constraints(), samples=[],warmups=[],orders=orders)
    output=args.output
    def save():
        temporary=output.with_suffix('.partial.json')
        temporary.write_text(json.dumps(payload,indent=2,default=str)+'\n')
        temporary.replace(output)
    def run_one(version,mode,case):
        source=args.baseline if version=='baseline' else args.optimized
        cmd=[WRAPPERS[mode],str(Path(__file__).resolve()),'--child',case,'--mode',mode,'--source',str(source)]
        start=time.perf_counter()
        p=subprocess.run(cmd,capture_output=True,text=True,env=os.environ.copy(),cwd='/tmp')
        if p.returncode: raise RuntimeError(f'{version}/{mode}/{case}: {p.stderr}\n{p.stdout[-2000:]}')
        value=json.loads(p.stdout.strip().splitlines()[-1])
        value.update(version=version,process_elapsed_sec=time.perf_counter()-start)
        if p.stderr: value['stderr']=p.stderr[-4000:]
        return value
    checksums={}
    # One excluded correctness/warmup sweep, preserved independently.
    for version,mode in combinations:
        for case in args.cases:
            value=run_one(version,mode,case)
            expected=checksums.setdefault(case,value['checksum'])
            assert value['checksum']==expected,(version,mode,case,'different result')
            payload['warmups'].append(value); save()
    print('Correctness equivalence / excluded warmups complete',flush=True)
    for r,order in enumerate(orders):
        case_order=list(args.cases)
        if r%2: case_order.reverse()
        for version,mode in order:
            for case in case_order:
                value=run_one(version,mode,case)
                assert value['checksum']==checksums[case],(r,version,mode,case,'result changed')
                value['round']=r
                payload['samples'].append(value);save()
        print(f'Round {r+1}/{args.rounds} complete',flush=True)
    payload['comparisons']=analyze(payload)
    save()


def comparison(a,b):
    fields=('elapsed_sec','cpu_sec','peak_rss_bytes','linux_vm_hwm_bytes')
    result={}
    for field in fields:
        av,bv=[x[field] for x in a],[x[field] for x in b]
        if any(x is None for x in av+bv):continue
        result[field]=dict(baseline=common.summary(av),candidate=common.summary(bv),
                          median=common.paired_comparison(av,bv,MARGIN),
                          p95=common.paired_p95_comparison(av,bv,MARGIN))
        if field.endswith('_bytes'):result[field]['absolute_delta']=common.paired_absolute_change(av,bv)
    return result


def analyze(payload):
    output={}
    for case in sorted({s['case'] for s in payload['samples']}):
        stages=('cold_request','warm_request') if case=='hevc_http' else ('operation',)
        for stage in stages:
            def rows(version,mode):
                return [s[stage] for s in sorted(payload['samples'],key=lambda s:s['round'])
                        if s['case']==case and s['version']==version and s['mode']==mode]
            for mode in MODES:
                output[f'{case}/{stage}/optimized_vs_baseline/{mode}']=comparison(rows('baseline',mode),rows('optimized',mode))
            for mode in ('ft_on','ft_off'):
                output[f'{case}/{stage}/optimized_{mode}_vs_optimized_regular']=comparison(rows('optimized','regular'),rows('optimized',mode))
                output[f'{case}/{stage}/optimized_{mode}_vs_baseline_regular']=comparison(rows('baseline','regular'),rows('optimized',mode))
    return output


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--source',type=Path)
    p.add_argument('--child',choices=CASES)
    p.add_argument('--mode',choices=MODES,default='regular')
    p.add_argument('--prepare',action='store_true')
    p.add_argument('--hevc-blocks',type=int,default=8)
    p.add_argument('--baseline',type=Path)
    p.add_argument('--optimized',type=Path)
    p.add_argument('--output',type=Path)
    p.add_argument('--rounds',type=int,default=12)
    p.add_argument('--cases',nargs='+',choices=CASES,default=list(CASES))
    p.add_argument('--analyze',type=Path)
    args=p.parse_args()
    if args.prepare:init_dbs(args)
    elif args.child:child(args)
    elif args.analyze:
        payload=json.loads(args.analyze.read_text());payload['comparisons']=analyze(payload)
        args.analyze.write_text(json.dumps(payload,indent=2)+'\n')
    else:controller(args)

if __name__=='__main__':main()
