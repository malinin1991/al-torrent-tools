"""Only self-generated media are written. User originals are stat'ed read-only."""
import hashlib,json,shutil,subprocess
from pathlib import Path
root=Path('/private/tmp/altt-operation-eval-20261013/fixtures')
gen=root/'generated';gen.mkdir(parents=True,exist_ok=True)
seed=root/'seed.mkv'
command=['/opt/homebrew/bin/ffmpeg','-hide_banner','-loglevel','error','-y','-f','lavfi','-i','testsrc2=size=320x180:rate=24','-f','lavfi','-i','sine=frequency=440:sample_rate=48000','-t','3','-c:v','libx264','-threads','2','-preset','fast','-pix_fmt','yuv420p','-c:a','aac','-metadata','title=ALTT generated benchmark',str(seed)]
subprocess.run(command,check=True)
for i in range(96):shutil.copyfile(seed,gen/f'generated-{i:03}.mkv')
real=Path('/Users/geekaz0id/Downloads/kensetsu-8cbb50a3')
paths=sorted(real.glob('*.mkv'));assert len(paths)==4
inventory=[dict(name=p.name,size=p.stat().st_size,mtime_ns=p.stat().st_mtime_ns) for p in paths]
def bencode(x):
 if isinstance(x,int):return b'i'+str(x).encode()+b'e'
 if isinstance(x,str):x=x.encode()
 if isinstance(x,bytes):return str(len(x)).encode()+b':'+x
 if isinstance(x,list):return b'l'+b''.join(map(bencode,x))+b'e'
 return b'd'+b''.join(bencode(k)+bencode(v) for k,v in sorted(x.items()))+b'e'
info={b'name':b'real',b'piece length':262144,b'pieces':b'\0'*20,b'files':[{b'length':p.stat().st_size,b'path':[p.name.encode()]}for p in paths]}
torrent=bencode({b'announce':b'http://invalid.invalid/announce',b'info':info})
(root/'torrents').mkdir(exist_ok=True)
(root/'torrents'/'real.torrent').write_bytes(torrent)
(root/'torrents'/'real.manifest.json').write_text(json.dumps(dict(info_hash=hashlib.sha1(bencode(info)).hexdigest(),torrent_sha256=hashlib.sha256(torrent).hexdigest(),inventory=inventory),indent=2))
manifest=dict(real_inventory=inventory,real_identification='size+mtime_ns+native parsed output; no extra full content digest',generated=dict(files=96,seed_bytes=seed.stat().st_size,seed_sha256=hashlib.sha256(seed.read_bytes()).hexdigest(),command=command,copies_identical=True),ffmpeg_version=subprocess.run(['/opt/homebrew/bin/ffmpeg','-version'],capture_output=True,text=True,check=True).stdout)
(root/'dataset-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
print(json.dumps(dict(real_files=len(paths),real_bytes=sum(p.stat().st_size for p in paths),generated_files=96,generated_bytes=96*seed.stat().st_size)))
