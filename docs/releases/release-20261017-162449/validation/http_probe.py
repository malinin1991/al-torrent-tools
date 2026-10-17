import hashlib,json,time,urllib.error,urllib.request
from html.parser import HTMLParser
from pathlib import Path
class Parse(HTMLParser):
 def __init__(self):super().__init__();self.keys=[]
 def handle_starttag(self,tag,attrs):
  attrs=dict(attrs)
  if tag=='tr' and 'data-hevc-row' in attrs:self.keys.append(attrs['data-source-key'])
urls=['/health/live','/hevc-workbench?view=all','/hevc-workbench/live?view=all','/hevc-workbench?view=overdue','/hevc-workbench/live?view=ready']
records=[]
for url in urls:
 deadline=time.monotonic()+120
 while True:
  try:
   with urllib.request.urlopen('http://127.0.0.1:8000'+url,timeout=15) as response:raw=response.read();status=response.status
   break
  except (OSError,urllib.error.URLError):
   if time.monotonic()>=deadline:raise
   time.sleep(.5)
 assert status==200
 body=raw.decode();parser=Parse();parser.feed(body)
 if 'hevc-workbench' in url:
  assert parser.keys==['a'*40+':current/ep-09.mkv'],parser.keys
  for text in ('HEVC отстаёт','changed','current/ep-09.mkv','Fixture &lt;historical &amp; AVC&gt;','26.0 ч'):
   assert text in body,(url,text)
  assert 'Якорь SLA' in body
 records.append({'url':url,'status':status,'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),'row_keys':parser.keys,'body':body})
print(json.dumps({'ok':True,'requests':records,'transport':'real TCP localhost to entrypoint-started Uvicorn, full lifespan+migration'},ensure_ascii=False,indent=2))
