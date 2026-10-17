import json,time,urllib.request
start=time.monotonic()
while True:
 try:
  with urllib.request.urlopen('http://127.0.0.1:8000/health/live',timeout=3) as response:
   body=json.loads(response.read());assert response.status==200 and body['status']=='ok'
  print(json.dumps({'ok':True,'health':body,'wait_sec':time.monotonic()-start}));break
 except OSError:
  if time.monotonic()-start>120:raise
  time.sleep(.5)
