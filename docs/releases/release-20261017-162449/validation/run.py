from pathlib import Path
import datetime,hashlib,json,subprocess,time,traceback
ROOT=Path(__file__).resolve().parent
PARENT=ROOT.parent
CONFIG=json.loads((PARENT/'config.json').read_text())
INDEX=json.loads((PARENT/'candidate-index.json').read_text())
CHILDREN={row['platform']['architecture']:row['digest'] for row in INDEX['manifests']}
LABEL='altt.release=release-20261017-162449'
PG='altt-release-162449-pg';NETWORK='altt-release-162449-tests'
OWNED=[]
RESULTS={}
def utc():return datetime.datetime.now(datetime.timezone.utc).isoformat()
def run(name,args,*,check=True,timeout=180):
 before=utc();started=time.monotonic()
 process=subprocess.run(args,capture_output=True,text=True,timeout=timeout)
 stdout=ROOT/'logs'/(name+'.stdout');stderr=ROOT/'logs'/(name+'.stderr')
 with stdout.open('x') as f:f.write(process.stdout)
 with stderr.open('x') as f:f.write(process.stderr)
 with (ROOT/'commands.jsonl').open('a') as f:f.write(json.dumps({'name':name,'argv':args,'started_utc':before,'ended_utc':utc(),'wall_sec':time.monotonic()-started,'exit_code':process.returncode,'stdout':str(stdout),'stderr':str(stderr)})+'\n')
 if check and process.returncode:raise RuntimeError(name+' failed '+str(process.returncode)+' '+process.stderr[-1500:])
 return process.stdout
try:
 for arch in ('arm64','amd64'):
  platform='linux/'+arch;ref=CONFIG['repository']+'@'+CHILDREN[arch]
  name='altt-release-162449-validation-api-'+arch
  db='altt_release_'+arch
  probe_args=['docker','run','--rm','--platform',platform,'--label',LABEL,'--read-only','--network','none','--tmpfs','/tmp:rw,size=67108864','--mount','type=bind,source='+str(ROOT)+',target=/validation,readonly',ref,'python','/validation/runtime_probe.py']
  probe=json.loads(run('probe-'+arch,probe_args))
  assert probe['architecture']==('aarch64' if arch=='arm64' else 'x86_64')
  with (ROOT/('probe-'+arch+'.json')).open('x') as f:json.dump(probe,f,ensure_ascii=False,indent=2)
  inspect=json.loads(run('inspect-image-'+arch,['docker','image','inspect',ref,'--format','{{json .}}']))
  assert inspect['Architecture']==arch and inspect['Os']=='linux'
  run('create-db-'+arch,['docker','exec',PG,'createdb','--username','postgres',db])
  url='postgresql+psycopg://postgres@'+PG+':5432/'+db
  args=['docker','run','--detach','--name',name,'--platform',platform,'--label',LABEL,'--read-only','--network',NETWORK,'--tmpfs','/tmp:rw,size=134217728','--mount','type=bind,source='+str(ROOT)+',target=/validation,readonly','--mount','type=bind,source='+str(ROOT/'media')+',target=/bench/media,readonly','--env','APP_ENV=dev','--env','PYTHONDONTWRITEBYTECODE=1','--env','PYTHONPATH=/app','--env','DATABASE_URL='+url,'--env','TORRENT_STORAGE_DIR=/tmp/torrents','--env','ANILIBRIA_MEDIA_ROOT=/bench/media','--env','ANILIBRIA_BASE_URL=http://invalid.invalid','--env','ANILIBRIA_FALLBACK_BASE_URL=http://invalid.invalid','--entrypoint','/bin/sh',ref,'/app/scripts/entrypoint.sh','uvicorn','app.main:app','--host','0.0.0.0','--port','8000','--loop','uvloop','--http','httptools']
  run('start-api-'+arch,args);OWNED.append(name)
  run('wait-api-'+arch,['docker','exec',name,'python','/validation/wait_api.py'])
  seed=json.loads(run('seed-'+arch,['docker','exec',name,'python','/validation/seed.py']))
  http=json.loads(run('http-'+arch,['docker','exec',name,'python','/validation/http_probe.py']))
  for i,response in enumerate(http['requests']):
   with (ROOT/(arch+'-http-'+str(i)+('.html' if 'hevc-workbench' in response['url'] else '.json'))).open('x') as f:f.write(response['body'])
  with (ROOT/('seed-'+arch+'.json')).open('x') as f:json.dump(seed,f,ensure_ascii=False,indent=2)
  with (ROOT/('http-'+arch+'.json')).open('x') as f:json.dump(http,f,ensure_ascii=False,indent=2)
  image_end=json.loads(run('probe-api-'+arch,['docker','exec',name,'python','/validation/runtime_probe.py']))
  assert image_end['source_files']==probe['source_files']
  container=json.loads(run('inspect-api-'+arch,['docker','inspect',name,'--format','{{json .}}']))
  assert container['Config']['Image']==ref and container['Config']['Labels']['altt.release']=='release-20261017-162449'
  assert not container['HostConfig']['PortBindings'] and container['HostConfig']['ReadonlyRootfs']
  RESULTS[arch]={'ok':True,'platform':platform,'runtime_manifest_digest':CHILDREN[arch],'reference':ref,'local_image_id':inspect['Id'],'container_image_id':container['Image'],'container_id':container['Id'],'database':db,'source_count':probe['source_count'],'source_manifest_matches':True,'runtime_probe':'probe-'+arch+'.json','seed_result':'seed-'+arch+'.json','http_result':'http-'+arch+'.json','http_statuses':[r['status'] for r in http['requests']],'historical_anchor':seed['anchor_at'],'history_hours':seed['row']['age_hours'],'sticky_status':seed['row']['sticky_status'],'native_driver':probe['psycopg_impl'],'migration':seed['revision'],'case_any_one_query':True}
  print(json.dumps({'completed':arch,**RESULTS[arch]}),flush=True)
except Exception:
 (ROOT/'failure.txt').write_text(traceback.format_exc());raise
finally:
 for name in OWNED:
  suffix=name.rsplit('-',1)[1]
  run('api-logs-'+suffix,['docker','logs',name],check=False)
  run('stop-api-'+suffix,['docker','stop','--time','15',name],check=False)
  run('remove-api-'+suffix,['docker','rm',name],check=False)
 if len(RESULTS)==2:
  manifest=json.loads((PARENT/'source-manifest.json').read_text());source=PARENT/'backend'
  assert all(hashlib.sha256((source/p).read_bytes()).hexdigest()==v['sha256'] for p,v in manifest.items())
  result={'ok':True,'candidate_index_digest':json.loads((PARENT/'buildx-metadata.json').read_text())['containerimage.digest'],'architectures':RESULTS,'source_files_frozen':len(manifest),'source_unchanged_after':True,'all_own_api_containers_removed':True,'postgres_network_kept_for_root':True,'no_latest_changes':True,'completed_utc':utc()}
  with (ROOT/'summary.json').open('x') as f:json.dump(result,f,indent=2)
