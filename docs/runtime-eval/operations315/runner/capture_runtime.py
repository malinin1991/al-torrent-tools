import json,subprocess,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent))
from benchmark_runtime import metadata
from benchmark_free_threading import cpu_constraints,gil_state
MODES={'regular':'/opt/runtime-modes/run_regular.sh','ft_on':'/opt/runtime-modes/run_t_gil1.sh','ft_off':'/opt/runtime-modes/run_t_default.sh'}
if len(sys.argv)>1:
 import psycopg.pq,yaml
 from pymediainfo import MediaInfo
 result=metadata()
 result.update(gil_after_native_imports=gil_state(),psycopg_backend=psycopg.pq.__impl__,libpq_version=psycopg.pq.version(),mediainfo_version=MediaInfo._get_library()[2],libyaml_version=list(yaml._yaml.get_version()))
 assert result['gil_enabled']==(sys.argv[1]!='ft_off')==result['gil_after_native_imports']
 print(json.dumps(result))
else:
 values={name:json.loads(subprocess.run([wrapper,__file__,name],capture_output=True,text=True,check=True).stdout) for name,wrapper in MODES.items()}
 assert len({json.dumps(v['packages'],sort_keys=True)for v in values.values()})==1
 print(json.dumps(dict(runtime=values,cpu_constraints=cpu_constraints()),indent=2))
