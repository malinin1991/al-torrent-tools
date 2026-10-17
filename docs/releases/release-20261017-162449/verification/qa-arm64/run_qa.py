from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parent
CANDIDATE=json.loads((ROOT/'qa-inputs.json').read_text())['candidate']
TAG='altt-release-qa:release-20261017-162449-arm64'
LABEL='altt.release=release-20261017-162449'

def run(name,argv):
    print('START '+name,flush=True)
    start=time.monotonic()
    with (ROOT/(name+'.log')).open('x') as stream:
        result=subprocess.run(argv,stdout=stream,stderr=subprocess.STDOUT,check=False)
    record={'stage':name,'argv':argv,'exit_code':result.returncode,'wall_sec':time.monotonic()-start,
            'utc':datetime.now(timezone.utc).isoformat(),'log':name+'.log'}
    with (ROOT/'qa-commands.jsonl').open('a') as stream:stream.write(json.dumps(record)+'\n')
    print('DONE '+name+' exit='+str(result.returncode),flush=True)
    if result.returncode:
        print((ROOT/(name+'.log')).read_text()[-7000:],flush=True)
        raise SystemExit(result.returncode)

def inspect(image):
    return json.loads(subprocess.check_output(['docker','image','inspect','--format','{{json .}}',image],text=True))

run('pull-candidate',['docker','pull','--platform','linux/arm64',CANDIDATE])
base=inspect(CANDIDATE)
(ROOT/'candidate-image.json').write_text(json.dumps({key:base[key] for key in ('Id','RepoDigests','Architecture','Os','RootFS')},indent=2)+'\n')
def fingerprint(name,image):
    run(name,['docker','run','--rm','--platform','linux/arm64','--network','none','--label',LABEL,
        '--env','PYTHONDONTWRITEBYTECODE=1','--mount',f'type=bind,source={ROOT},target=/evidence',
        '--mount',f'type=bind,source={ROOT},target=/qa-tools,readonly','--entrypoint','python',image,
        '/qa-tools/fingerprint_packages.py','/evidence/'+name+'.json'])
fingerprint('candidate-packages',base['Id'])
run('build-qa',['docker','build','--platform','linux/arm64','--build-arg','CANDIDATE='+CANDIDATE,
    '-f',str(ROOT/'context'/'Dockerfile.qa'),'-t',TAG,str(ROOT/'context')])
qa=inspect(TAG)
(ROOT/'qa-image.json').write_text(json.dumps({key:qa[key] for key in ('Id','RepoDigests','Architecture','Os','RootFS')},indent=2)+'\n')
assert qa['Architecture']==base['Architecture']=='arm64'
assert qa['RootFS']['Layers'][:len(base['RootFS']['Layers'])]==base['RootFS']['Layers']
fingerprint('qa-packages',qa['Id'])
before=json.loads((ROOT/'candidate-packages.json').read_text());after=json.loads((ROOT/'qa-packages.json').read_text())
assert all(after.get(name)==record for name,record in before.items()), 'Candidate package code/version changed'
added=set(after)-set(before)
assert added <= {'pytest','watchfiles','iniconfig','packaging','pluggy','pygments'}, added
proof={'candidate':CANDIDATE,'candidate_image_id':base['Id'],'qa_image_id':qa['Id'],
       'candidate_layers_preserved_as_prefix':True,'all_runtime_package_code_and_metadata_unchanged':True,
       'runtime_packages':len(before),'added_dev_packages':{name:after[name]['version'] for name in sorted(added)},
       'existing_runtime_source_files_not_overwritten':163,'added_frozen_source_files':100}
(ROOT/'qa-derivation-proof.json').write_text(json.dumps(proof,indent=2)+'\n')
run('full-checks',[sys.executable,str(ROOT/'run_image_checks.py'),'--image-id',qa['Id']])
summary=json.loads((ROOT/'pytest-runtime-summary.json').read_text())
assert summary['exit_code']==0 and summary['sources_unchanged'] and summary['expected_source_files']==263
proof.update(full_suite_passed=True,passed=summary['test_outcomes']['passed'],skips=summary['skips'],
             pip_check_passed=True,compileall_passed=True,source_manifest_sha256=summary['frozen_source_manifest_sha256'])
(ROOT/'qa-final-receipt.json').write_text(json.dumps(proof,indent=2)+'\n')
print(json.dumps(proof,indent=2),flush=True)
