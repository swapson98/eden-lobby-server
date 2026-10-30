"""EDEN lobby directory. No relay, UPnP, public-IP lookup or remote administration.
Run locally: python registry.py --local-test. Production requires TLS cert/key.
Room leases expire after 60s. All state is ephemeral; never log tokens/endpoints.
"""
import os
from social import SocialStore, SocialError
from social_storage import PostgresStorage, StorageUnavailable
import argparse, collections, hashlib, hmac, http.server, ipaddress, json, re, secrets, ssl, threading, time
class Directory:
 def __init__(self,social_db=None):
  self.rooms={}; self.rates={}; self.lock=threading.Lock(); self.rate_lock=threading.Lock()
  path=social_db or os.environ.get("EDEN_SOCIAL_DB")
  self.social=(SocialStore(path) if path else SocialStore(storage=PostgresStorage()) if os.environ.get('EDEN_SOCIAL_DATABASE_URL') else None)
 def handle(self,method,path,data,auth,peer):
  now=time.monotonic()
  with self.rate_lock:
   self.rates={k:v for k,v in self.rates.items() if now-v[0]<60}
   count=self.rates.setdefault(peer,[now,0]);count[1]+=1
   if count[1]>180 or len(self.rates)>10000:return 429,{'error':'Rate limited; retry later'}
  # Public listing is a memory-only snapshot: never wait for social SQL/locks.
  # Copy the dictionary values before iteration; expose public fields only.
  if method=='GET' and path=='/v1/rooms':
   return 200,{'rooms':[self.public(r) for r in tuple(self.rooms.values()) if r['public'] and r['expiry']>now][:100]}
  with self.lock:
   self.rooms={k:r for k,r in self.rooms.items() if r['expiry']>now}
   if self.social:
    self.social.prune()
    for reservation in self.social.reservations.values():
     if reservation['room'] not in self.rooms and reservation['state'] in ('preparing','committed'):reservation['state']='cancelled'
   if path.startswith('/v1/social/'):
    return self.social.handle(method,path,data,auth,peer,self) if self.social else (503,{'error':'Social service persistence is not configured'})
   if method=='GET' and path=='/v1/rooms':return 200,{'rooms':[self.public(r) for r in self.rooms.values() if r['public']][:100]}
   if method=='POST' and path=='/v1/rooms':
    endpoint=data.get('endpoint','')
    if not isinstance(endpoint,str):return 400,{'error':'Invalid endpoint'}
    host,sep,port=endpoint.rpartition(':')
    if not sep or not port.isascii() or not port.isdigit() or not 1024<=int(port)<=65535:return 400,{'error':'Invalid endpoint port'}
    try:
     ip=ipaddress.IPv4Address(host)
     if ip.is_multicast or ip.is_unspecified:raise ValueError()
    except ValueError:
     if re.fullmatch(r'[0-9.]+',host):return 400,{'error':'Invalid IPv4'}
     if len(host)>253 or not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?',host) or any(not 1<=len(x)<=63 or x.startswith('-') or x.endswith('-') for x in host.split('.')):return 400,{'error':'Invalid endpoint host'}
    if type(data.get('public')) is not bool or data.get('map') not in ['BioLab','Biocore','Forge','Frostline'] or data.get('mode') not in [0,1,2]:return 400,{'error':'Invalid room settings'}
    name=data.get('name','')
    if not isinstance(name,str) or not 1<=len(name.strip())<=40 or any(ord(c)<32 for c in name):return 400,{'error':'Invalid room name'}
    host_id=None
    if auth:
     if not self.social:return 503,{'error':'Social service persistence is not configured'}
     try:host_id=self.social.actor(auth)
     except SocialError:return 401,{'error':'Host device credential invalid'}
     except StorageUnavailable:return 503,{'error':'Social storage temporarily unavailable; retry later'}
    request_id=data.get('requestId');request_hash=None
    if request_id is not None:
     if not self.social or not self.social.identifier(request_id):return 400,{'error':'Invalid request ID'}
     if not host_id:return 401,{'error':'Device authentication required for request replay'}
     request_hash=hashlib.sha256(json.dumps(data,sort_keys=True,separators=(',',':')).encode()).hexdigest()
     for previous in self.rooms.values():
      if previous.get('hostId')==host_id and previous.get('requestId')==request_id:
       if previous.get('requestHash')!=request_hash:return 409,{'error':'Request ID reused for another room'}
       return 201,dict(id=previous['id'],ownerToken=previous['owner'],inviteCode=previous['code'])
    if len(self.rooms)>=200:return 503,{'error':'Directory capacity reached'}
    r=dict(id=secrets.token_hex(16),name=name.strip(),public=data['public'],map=data['map'],mode=data['mode'],players=1,endpoint=endpoint,owner=secrets.token_hex(32),code=secrets.token_hex(16),expiry=now+60,tickets={})
    if host_id:r['hostId']=host_id
    if request_id is not None:r.update(requestId=request_id,requestHash=request_hash)
    self.rooms[r['id']]=r;return 201,dict(id=r['id'],ownerToken=r['owner'],inviteCode=r['code'])
   if method=='POST' and path=='/v1/join':
    r=None;code=data.get('code','');rid=data.get('roomId','')
    if code:
     for room in self.rooms.values():
      if isinstance(code,str) and hmac.compare_digest(room['code'],code):r=room;break
    elif rid in self.rooms and self.rooms[rid]['public']:r=self.rooms[rid]
    if not r:return 404,{'error':'Room not found, expired or invitation invalid'}
    reserved=sum(len(x['tickets']) for x in self.social.room_reservations(r['id'])) if self.social else 0
    if r['players']+reserved>=8:return 409,{'error':'Room full (8 players)'}
    r['tickets']={k:v for k,v in r['tickets'].items() if v>now}
    if len(r['tickets'])>=64:return 429,{'error':'Too many pending joins'}
    ticket=secrets.token_hex(32);r['tickets'][ticket]=now+30
    return 200,dict(endpoint=r['endpoint'],ticket=ticket,roomId=r['id'])
   match=re.fullmatch(r'/v1/rooms/([a-f0-9]{32})(/verify|/party/commit|/party/cancel|/party/complete)?',path)
   if not match or match[1] not in self.rooms:return 404,{'error':'Room closed or expired'}
   r=self.rooms[match[1]]
   if not hmac.compare_digest('Bearer '+r['owner'],auth):return 403,{'error':'Host authorization required'}
   if method=='POST' and match[2] and match[2].startswith('/party/'):
    if not self.social:return 503,{'error':'Social service persistence is not configured'}
    key=data.get('reservationId');reservation=self.social.reservations.get(key) if isinstance(key,str) else None
    if not reservation or reservation['room']!=r['id']:return 404,{'error':'Party reservation not found'}
    if match[2]=='/party/cancel':
     reservation['state']='cancelled';return 200,{'ok':True}
    members=data.get('members')
    if not isinstance(members,list) or len(members)!=len(reservation['tickets']) or set(members)!=set(reservation['tickets']):return 400,{'error':'Whole party member set required'}
    if match[2]=='/party/commit':
     if reservation['state']!='preparing' or reservation['until']<=now or reservation['verified']!=set(reservation['tickets']):return 409,{'error':'Whole party verification required before commit'}
     reservation['state']='committed';reservation['until']=now+30;return 200,{'ok':True}
    if reservation['state']!='committed':return 409,{'error':'Party commit required'}
    reservation['state']='accepted';reservation['until']=now+60;return 200,{'ok':True}
   if method=='POST' and match[2]=='/verify':
    ticket=data.get('ticket','')
    if self.social and isinstance(ticket,str):
     reservation,player=self.social.reservation_ticket(r['id'],ticket)
     if reservation:
      if player in reservation['verified']:return 403,{'error':'Party invitation ticket already used'}
      reservation['verified'].add(player)
      return 200,dict(ok=True,reservationId=reservation['id'],playerId=player,team=reservation['team'],members=list(reservation['tickets']),expiresIn=max(0,int(reservation['until']-now)))

    ticket=data.get('ticket','');deadline=r['tickets'].pop(ticket,0) if isinstance(ticket,str) else 0
    return (200,{'ok':True}) if deadline>now else (403,{'error':'Invitation ticket invalid, expired or already used'})
   if method=='DELETE':
    if self.social:
     for reservation in self.social.room_reservations(r['id']):reservation['state']='cancelled'
    del self.rooms[r['id']];return 200,{'ok':True}
   if method=='PATCH':
    if type(data.get('players')) is not int or not 0<=data['players']<=8 or data.get('map') not in ['BioLab','Biocore','Forge','Frostline'] or data.get('mode') not in [0,1,2]:return 400,{'error':'Invalid heartbeat'}
    if 'supportsParty' in data:
     if type(data['supportsParty']) is not bool:return 400,{'error':'Invalid party capability'}
     if data['supportsParty']:
      if any(type(data.get(k)) is not int or not 0<=data[k]<=4 for k in ('teamA','teamB')) or data['teamA']+data['teamB']!=data['players']:return 400,{'error':'Invalid party team capacity'}
      if 'hostTeam' in data and (type(data['hostTeam']) is not int or data['hostTeam'] not in (0,1)):return 400,{'error':'Invalid host team'}
      r.update(teamA=data['teamA'],teamB=data['teamB'])
      if 'hostTeam' in data:r['hostTeam']=data['hostTeam']
     r['supportsParty']=data['supportsParty']
    r.update(players=data['players'],map=data['map'],mode=data['mode'],expiry=now+60);return 200,{'ok':True}
   return 405,{'error':'Method not allowed'}
 @staticmethod
 def public(r):return {k:r[k] for k in ['id','name','map','mode','players']}
class Handler(http.server.BaseHTTPRequestHandler):
 directory=Directory()
 def log_message(self,*args):pass
 def do_GET(self):self.dispatch()
 def do_POST(self):self.dispatch()
 def do_PATCH(self):self.dispatch()
 def do_DELETE(self):self.dispatch()
 def dispatch(self):
  self.connection.settimeout(5)
  try:
   n=int(self.headers.get('Content-Length','0'))
   if n<0 or n>4096:raise ValueError()
   data=json.loads(self.rfile.read(n)) if n else {}
   if not isinstance(data,dict):raise ValueError()
   status,body=self.directory.handle(self.command,self.path,data,self.headers.get('Authorization',''),self.client_address[0])
  except (ValueError,TypeError,UnicodeError):status,body=400,{'error':'Invalid request'}
  except (TimeoutError,ConnectionError):return
  except Exception:status,body=500,{'error':'Directory request failed'}
  raw=json.dumps(body,ensure_ascii=True).encode();self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(raw)));self.end_headers()
  try:self.wfile.write(raw)
  except (ConnectionError,TimeoutError):pass
if __name__=='__main__':
 a=argparse.ArgumentParser();a.add_argument('--local-test',action='store_true');a.add_argument('--bind',default='127.0.0.1');a.add_argument('--port',type=int,default=8787);a.add_argument('--cert');a.add_argument('--key');a.add_argument('--social-db');o=a.parse_args()
 if o.local_test and o.bind!='127.0.0.1':a.error('Local test must bind loopback only')
 if not o.local_test and not (o.cert and o.key):a.error('Production requires HTTPS cert/key; deployment needs user approval')
 if o.social_db:Handler.directory=Directory(o.social_db)
 server=http.server.ThreadingHTTPServer((o.bind,o.port),Handler)
 if not o.local_test:
  context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.minimum_version=ssl.TLSVersion.TLSv1_2;context.load_cert_chain(o.cert,o.key);server.socket=context.wrap_socket(server.socket,server_side=True)
 print('EDEN lobby directory ready; no access/endpoint logs',flush=True);server.serve_forever()
