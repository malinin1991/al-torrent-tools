"""Run only after both-platform smoke completion; delete these exact resources."""
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import time

ROOT=Path(__file__).resolve().parent
fixture=json.loads((ROOT/'postgres-fixture.json').read_text())
key,value=fixture['label'].split('=',1)
receipt=ROOT/'cleanup-postgres.json'
if receipt.exists():raise SystemExit('Cleanup receipt already exists')
records=[]
def run(argv):
 start=time.monotonic();result=subprocess.run(argv,capture_output=True,text=True,check=False)
 records.append({'argv':argv,'exit_code':result.returncode,'wall_sec':time.monotonic()-start,'stdout':result.stdout,'stderr':result.stderr})
 if result.returncode:raise RuntimeError(result.stderr)
 return result.stdout
container=json.loads(run(['docker','inspect',fixture['container_id']]))[0]
network=json.loads(run(['docker','network','inspect',fixture['network_id']]))[0]
assert container['Id']==fixture['container_id'] and container['Config']['Labels'].get(key)==value
assert network['Id']==fixture['network_id'] and network['Labels'].get(key)==value and network['Internal']
assert set(network['Containers']) <= {fixture['container_id']}, 'Other attached containers still need network'
run(['docker','rm','-f',fixture['container_id']])
run(['docker','network','rm',fixture['network_id']])
receipt.write_text(json.dumps({'utc':datetime.now(timezone.utc).isoformat(),'ownership_checked':fixture['label'],
 'deleted_container':fixture['container_id'],'deleted_network':fixture['network_id'],
 'scope':'Only this disposable tmpfs PostgreSQL and its internal network; no global prune/images/other services',
 'commands':records},indent=2)+'\n')
print(receipt)
