import json,os,sys
from pathlib import Path
sys.path.insert(0,'/app')
from datetime import timedelta
from sqlalchemy import event,select,text
from app.db.models import Release,TorrentArchive,TorrentFile
from app.db.session import SessionLocal,engine
from app.services.hevc_workbench import list_hevc_workbench
from app.utils.datetime_fmt import utcnow,as_utc_iso
assert engine.url.database in ('altt_release_arm64','altt_release_amd64')
now=utcnow();rid=901
with SessionLocal() as db:
 assert db.scalar(select(Release.release_id).limit(1)) is None
 db.add(Release(release_id=rid,title='Fixture <historical & AVC>',release_alias='fixture-901',source='site'))
 def add(info_hash,tid,codec,desc,age,relative,**flags):
  row=TorrentArchive(info_hash=info_hash,torrent_id=tid,release_id=rid,anime_name='Fixture',release_alias='fixture-901',torrent_type='BDRip 1080p '+codec,torrent_description=desc,quality_json={'type':{'value':'BDRip'},'quality':{'value':'1080p'},'codec':{'value':codec}},file_path='/tmp/'+info_hash+'.torrent',api_created_at=now-timedelta(hours=age),created_at=now-timedelta(hours=age),api_present=flags.get('api_present',True),superseded=flags.get('superseded',False),ignore_hevc=flags.get('ignore_hevc',False))
  f=TorrentFile(release_id=rid,torrent_id=tid,info_hash=info_hash,relative_path=relative,full_path='/bench/media/'+relative,size=24,ui_status='changed',selected=True,media_present=True,is_checking=False,updated_at=now)
  db.add_all((row,f));return row,f
 hist,hf=add('b'*40,800,'AVC','1-8',50,'old/ep-09.mkv',api_present=False,superseded=True,ignore_hevc=True)
 hevc,ef=add('c'*40,799,'HEVC','1-8',51,'hevc/ep-08.mkv')
 active,af=add('a'*40,800,'AVC','1-9',1,'current/ep-09.mkv')
 db.commit();aid=active.id;fid=af.id
statements=[]
def audit(_c,_cur,stmt,params,_ctx,_many):statements.append({'sql':stmt,'parameters':str(params)})
with SessionLocal() as db:
 db.execute(text('SET TRANSACTION READ ONLY'))
 before=[tuple(r) for r in db.execute(select(TorrentFile.__table__).order_by(TorrentFile.id))]
 event.listen(engine,'before_cursor_execute',audit)
 try:ctx=list_hevc_workbench(db,now=now,view='all')
 finally:event.remove(engine,'before_cursor_execute',audit)
 after=[tuple(r) for r in db.execute(select(TorrentFile.__table__).order_by(TorrentFile.id))]
 assert before==after and not db.new and not db.dirty and not db.deleted
 assert ctx['total']==len(ctx['rows'])==1 and ctx['counts']['overdue']==1 and ctx['counts']['ready']==1
 row=ctx['rows'][0]
 assert row['archive_id']==aid and row['file_id']==fid and row['info_hash']=='a'*40
 assert row['relative_path']=='current/ep-09.mkv' and row['sticky_status']=='changed'
 assert row['hevc_status']=='overdue' and row['age_hours']==50.0 and row['overdue_hours']==26.0
 assert row['anchor_at']==as_utc_iso(now-timedelta(hours=50))
 assert row['db_ready'] and row['fs_available'] and row['selectable']
 revision=db.scalar(text('SELECT version_num FROM alembic_version'));assert revision=='0027_encoder_job_tracking'
 file_sql=[r for r in statements if 'FROM torrent_files' in r['sql']]
 assert len(file_sql)==1 and 'CASE WHEN' in file_sql[0]['sql'] and 'ANY (' in file_sql[0]['sql']
 assert all(r['sql'].lstrip().upper().startswith('SELECT') for r in statements)
 print(json.dumps({'ok':True,'db':engine.url.database,'revision':revision,'seed_now':as_utc_iso(now),'anchor_at':row['anchor_at'],'counts':ctx['counts'],'row':row,'archives':3,'files':3,'historical_metadata_preserved':True,'read_only_context':True,'one_file_select':file_sql,'sql_reads':len(statements)},default=str,ensure_ascii=False,indent=2))
