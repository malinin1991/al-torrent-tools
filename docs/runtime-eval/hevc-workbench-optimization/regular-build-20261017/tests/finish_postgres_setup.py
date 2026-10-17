from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import time
ROOT=Path(__file__).resolve().parent
records=[json.loads(line) for line in (ROOT/'setup-commands.jsonl').read_text().splitlines()]
image,network_id,container_id=[record['stdout'].strip() for record in records[:3]]
network='altt-regular-workbench-tests-20261017-161151'
container='altt-regular-workbench-pg-20261017-161151'
database='altt_workbench_test'
argv=['docker','exec',container,'psql','-h','127.0.0.1','-U','postgres','-d',database,'-At','-c',"SELECT current_database(), version(), current_setting('fsync'), current_setting('full_page_writes'), current_setting('synchronous_commit')"]
start=time.monotonic()
result=subprocess.run(argv,capture_output=True,text=True,check=False)
with (ROOT/'setup-commands.jsonl').open('a') as stream:
 stream.write(json.dumps({'utc':datetime.now(timezone.utc).isoformat(),'argv':argv,'exit_code':result.returncode,'wall_sec':time.monotonic()-start,'stdout':result.stdout,'stderr':result.stderr,'phase':'confirm-final-TCP-server-after-initialization'})+'\n')
if result.returncode:raise SystemExit(result.stderr)
metadata={'network':network,'network_id':network_id,'container':container,'container_id':container_id,'image_id':image,'database_name':database,'database_url':f'postgresql+psycopg://postgres@{container}:5432/{database}','host_ports':[],'data_storage':'container-only tmpfs 512MiB','label':'altt.regular-workbench=20261017-161151','configuration':result.stdout.strip(),'setup_note':'First readiness check saw temporary initialization socket; final TCP server verified separately. No resource recreation.'}
path=ROOT/'postgres-fixture.json'
with path.open('x') as stream:stream.write(json.dumps(metadata,indent=2)+'\n')
print(json.dumps(metadata,indent=2))
