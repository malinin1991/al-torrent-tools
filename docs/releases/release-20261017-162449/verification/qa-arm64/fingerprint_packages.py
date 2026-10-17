import hashlib
import importlib.metadata
import json
from pathlib import Path
import sys

result={}
for dist in importlib.metadata.distributions():
    records={}
    for entry in dist.files or []:
        if '__pycache__' in entry.parts or entry.suffix in {'.pyc','.pyo'}:
            continue
        path=dist.locate_file(entry)
        if path.is_file():records[str(entry)]=hashlib.sha256(path.read_bytes()).hexdigest()
    name=dist.metadata['Name'].lower().replace('_','-')
    result[name]={'version':dist.version,'file_count':len(records),
                  'code_metadata_sha256':hashlib.sha256(json.dumps(records,sort_keys=True).encode()).hexdigest()}
Path(sys.argv[1]).write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps({'packages':len(result),'output':sys.argv[1]}))
