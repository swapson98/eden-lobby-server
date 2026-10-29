"""Run behind platform HTTPS: uvicorn asgi:app --host 0.0.0.0 --port $PORT --no-access-log.
The stdlib registry.py listener is for isolated local tests only.
Set EDEN_SOCIAL_DB to a persistent writable SQLite path to enable device accounts and friends.
Alternatively set EDEN_SOCIAL_DATABASE_URL to a server-only TLS PostgreSQL DSN.
Without it the deployed legacy room API is unchanged and social endpoints return 503.
"""
import asyncio, json
from registry import Directory
directory=Directory()
work=asyncio.Semaphore(8)
async def app(scope,receive,send):
 if scope['type']!='http':return
 body=b''
 while True:
  event=await receive()
  if event['type']=='http.disconnect':return
  body+=event.get('body',b'')
  if len(body)>4096:status,result=413,{'error':'Request too large'};break
  if not event.get('more_body',False):
   try:
    data=json.loads(body) if body else {}
    if not isinstance(data,dict):raise ValueError()
    headers=dict(scope.get('headers',[]));auth=headers.get(b'authorization',b'').decode('ascii')
    args=(scope['method'],scope['path'],data,auth,scope.get('client',('unknown',))[0])
    if args[:2]==('GET','/v1/rooms'):
     status,result=directory.handle(*args)
    else:
     async with work:status,result=await asyncio.to_thread(directory.handle,*args)
   except (ValueError,TypeError,UnicodeError):status,result=400,{'error':'Invalid request'}
   break
 raw=json.dumps(result).encode()
 await send({'type':'http.response.start','status':status,'headers':[(b'content-type',b'application/json'),(b'cache-control',b'no-store')]})
 await send({'type':'http.response.body','body':raw})
