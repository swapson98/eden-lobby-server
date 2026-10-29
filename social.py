"""Device-credential social service. SQLite/PostgreSQL; no external account service.
Room transport remains host-owned. Party reservations require a capability-aware host,
which stages admissions and confirms all members before starting gameplay.
"""
import copy, hashlib, hmac, json, re, secrets, threading, time
from social_storage import SQLiteStorage, StorageUnavailable

class SocialError(Exception):
    def __init__(self,status,message):self.status=status;self.message=message

class SocialStore:
    def __init__(self,path=None,storage=None):
        self.storage=storage or SQLiteStorage(path)
        self.db=getattr(self.storage,'db',None)  # legacy SQLite test inspection
        self.lock=threading.RLock()
        self.parties={};self.invites={};self.reservations={};self.presence={};self.rates={}
        self.restored_until={}
    def close(self):self.storage.close()
    @staticmethod
    def digest(token):return hashlib.sha256(token.encode()).hexdigest()
    @staticmethod
    def identifier(value):return isinstance(value,str) and re.fullmatch('[a-f0-9]{32}',value) is not None
    @staticmethod
    def text(value,limit):
        return isinstance(value,str) and 1<=len(value.strip())<=limit and not any(ord(c)<32 for c in value)
    def fail(self,status,message):raise SocialError(status,message)
    def actor(self,auth):
        token=auth[7:] if auth.startswith('Bearer ') else ''
        if not re.fullmatch('[a-f0-9]{64}',token):self.fail(401,'Sign in to the social service')
        if self.storage.persistent_parties and self.storage.db is None:
            with self.lock,self.storage.transaction():player=self.storage.actor(self.digest(token))
        else:player=self.storage.actor(self.digest(token))
        if not player:self.fail(401,'Device credential invalid')
        return player
    def prune(self):
        now=time.monotonic()
        for key,party in list(self.parties.items()):
            online=[m for m in party['members'] if self.presence.get(m,('offline',0))[0]!='offline' and now-self.presence.get(m,('offline',0))[1]<=35]
            if self.storage.persistent_parties and (not online or now<self.restored_until.get(key,0)):continue
            if party['leader'] not in online and not any(r['party']==key and r['state']=='committed' for r in self.reservations.values()):
                self.cancel_party_reservation(key)
                if online:
                    party['leader']=online[0]
                    for member in party['members']:party['members'][member]=False
                else:del self.parties[key]
        self.invites={k:v for k,v in self.invites.items() if v['until']>now and v['party'] in self.parties}
        for r in self.reservations.values():
            if r['state'] in ('preparing','committed') and r['until']<=now:r['state']='expired'
        self.reservations={k:v for k,v in self.reservations.items() if v['until']+120>now}
    def profile(self,player):
        row=self.storage.profile(player)
        if not row:return None
        status,seen=self.presence.get(player,('offline',0))
        if time.monotonic()-seen>35:status='offline'
        return dict(id=row['id'],name=row['name'],status=status)
    def party_for(self,player):
        return next((p for p in self.parties.values() if player in p['members']),None)
    def active_reservation(self,party):
        return next((r for r in self.reservations.values() if r['party']==party and r['state']=='preparing'),None)
    def cancel_party_reservation(self,party):
        for r in self.reservations.values():
            if r['party']==party and r['state']=='preparing':r['state']='cancelled'
    def snapshot(self,actor):
        friend_ids,request_rows=self.storage.relations(actor)
        invite_rows=[(k,v) for k,v in self.invites.items() if v['to']==actor]
        party=self.party_for(actor)
        ids={actor,*friend_ids}
        ids.update(row['b'] if row['a']==actor else row['a'] for row in request_rows)
        ids.update(v['from'] for _,v in invite_rows)
        if party:ids.update(party['members'])
        # Request-scoped batch only: no stale relationship/authentication cache.
        profiles=self.storage.profiles(ids)
        def view(player):
            row=profiles.get(player)
            if not row:return None
            status,seen=self.presence.get(player,('offline',0))
            if time.monotonic()-seen>35:status='offline'
            return dict(id=row['id'],name=row['name'],status=status)
        friends=[view(player) for player in friend_ids]
        requests=[]
        for row in request_rows:
            value=view(row['b'] if row['a']==actor else row['a']);value['direction']='outgoing' if row['a']==actor else 'incoming';requests.append(value)
        invitations=[dict(id=k,**{'from':view(v['from'])},party=v['party'],expiresIn=max(0,int(v['until']-time.monotonic()))) for k,v in invite_rows]
        public=None if not party else dict(id=party['id'],leader=party['leader'],members=[dict(**view(m),ready=ready) for m,ready in party['members'].items()])
        admission=None
        for r in reversed(list(self.reservations.values())):
            if actor in r['tickets'] or actor==r.get('resident'):
                admission=dict(reservation=r['id'],reservationId=r['id'],roomId=r['room'],endpoint=r['endpoint'],ticket=r['tickets'].get(actor,''),resident=actor==r.get('resident'),team=r['team'],state=r['state'],expiresIn=max(0,int(r['until']-time.monotonic())));break
        return dict(self=view(actor),friends=friends,requests=requests,invitations=invitations,party=public,admission=admission)
    def room_reservations(self,room):
        return [r for r in self.reservations.values() if r['room']==room and r['state'] in ('preparing','committed')]
    def reservation_ticket(self,room,ticket):
        for r in self.room_reservations(room):
            if r['state']!='preparing':continue
            for player,t in r['tickets'].items():
                if hmac.compare_digest(t,ticket):return r,player
        return None,None
    def handle(self,method,path,data,auth,peer,directory):
        # Roll back both durable writes and transient admissions on failed operations.
        class Rejected(Exception):pass
        with self.lock:
            saved=copy.deepcopy((self.parties,self.invites,self.reservations,self.presence,self.rates,self.restored_until))
            try:
                with self.storage.transaction():
                    before={}
                    if self.storage.persistent_parties:
                        before=self.storage.load_parties()
                        for key,party in before.items():
                            old=self.parties.get(key)
                            if old and old['leader']==party['leader']:
                                party['members']={m:old['members'].get(m,False) for m in party['members']}
                            if key not in self.restored_until:self.restored_until[key]=time.monotonic()+35
                        self.parties=copy.deepcopy(before)
                    result=self._handle(method,path,data,auth,peer,directory)
                    if result[0]>=400:raise Rejected()
                    if self.storage.persistent_parties:self.storage.save_parties(before,self.parties)
                return result
            except (Rejected,StorageUnavailable) as error:
                self.parties,self.invites,self.reservations,self.presence,self.rates,self.restored_until=saved
                if isinstance(error,Rejected):return result
                return 503,dict(error='Social storage temporarily unavailable; retry later')

    def _handle(self,method,path,data,auth,peer,directory):
        with self.lock:
            try:
                self.prune();now=time.monotonic()
                if method=='POST' and path=='/v1/social/register':
                    rate=self.rates.setdefault(peer,[]);rate[:]=[t for t in rate if now-t<3600]
                    if len(rate)>=12:self.fail(429,'Too many new device registrations')
                    name=data.get('name','')
                    if not self.text(name,24):self.fail(400,'Display name must be 1-24 characters')
                    player=secrets.token_hex(16);token=secrets.token_hex(32)
                    self.storage.register(player,name.strip(),self.digest(token))
                    rate.append(now);self.presence[player]=('online',now)
                    return 201,dict(id=player,name=name.strip(),token=token)
                actor=self.actor(auth)
                previous=self.presence.get(actor,('online',now))[0];self.presence[actor]=(previous if previous!='offline' else 'online',now)
                if method=='GET' and path=='/v1/social/state':return 200,self.snapshot(actor)
                if method=='POST' and path=='/v1/social/presence':
                    status=data.get('status')
                    if status not in ('online','playing','offline'):self.fail(400,'Invalid presence')
                    self.presence[actor]=(status,now);return 200,self.snapshot(actor)
                if method=='POST' and path=='/v1/social/search':
                    query=data.get('query','')
                    if not self.text(query,40):self.fail(400,'Search text required')
                    # Stable exact ID or case-insensitive name substring; no credential material.
                    rows=self.storage.search(query)
                    return 200,dict(players=[self.profile(player) for player in rows if player!=actor])
                if method!='POST' or path!='/v1/social/action':self.fail(404,'Social operation not found')
                op=data.get('op');target=data.get('target','');party=self.party_for(actor)
                if op in ('friend_request','friend_accept','friend_decline','friend_remove','party_invite'):
                    if not self.identifier(target) or target==actor or not self.profile(target):self.fail(400,'Invalid target player')
                pair=tuple(sorted((actor,target))) if isinstance(target,str) else (actor,'')
                if op=='friend_request':
                    if self.storage.has_friend(pair):self.fail(409,'Already friends')
                    if (self.storage.pending(actor,target) or self.storage.pending(target,actor)):self.fail(409,'Friend request already pending')
                    if self.storage.request_count(actor)>=32:self.fail(429,'Pending friend request limit')
                    self.storage.request(actor,target)
                elif op in ('friend_accept','friend_decline'):
                    if not self.storage.pending(target,actor):self.fail(404,'Incoming request not found')
                    self.storage.resolve(target,actor,op=='friend_accept')
                elif op=='friend_remove':
                    self.storage.remove(pair)
                elif op=='party_create':
                    if party:self.fail(409,'Already in a party')
                    party=dict(id=secrets.token_hex(16),leader=actor,members={actor:False});self.parties[party['id']]=party
                elif op=='party_invite':
                    if not party or party['leader']!=actor:self.fail(403,'Only party leader can invite')
                    if len(party['members'])>=4:self.fail(409,'Party full (4 players)')
                    if not self.storage.has_friend(pair):self.fail(403,'Invite an accepted friend')
                    if self.party_for(target):self.fail(409,'Player already in a party')
                    if any(v['party']==party['id'] and v['to']==target for v in self.invites.values()):self.fail(409,'Invitation already pending')
                    if sum(v['from']==actor for v in self.invites.values())>=16:self.fail(429,'Pending invitation limit')
                    self.invites[secrets.token_hex(16)]=dict(party=party['id'],**{'from':actor},to=target,until=now+60)
                elif op in ('party_accept','party_decline'):
                    invite=data.get('invite');inv=self.invites.get(invite)
                    if not inv or inv['to']!=actor:self.fail(404,'Invitation expired or not addressed to you')
                    if op=='party_accept':
                        if party:self.fail(409,'Leave your existing party first')
                        other=self.parties[inv['party']]
                        if len(other['members'])>=4:self.fail(409,'Party full (4 players)')
                        if self.active_reservation(other['id']):self.fail(409,'Party admission in progress')
                        other['members'][actor]=False
                        for m in other['members']:other['members'][m]=False
                    del self.invites[invite]
                elif op in ('party_disband','party_promote'):
                    if not party or party['leader']!=actor:self.fail(403,'Only party leader can change leadership or disband')
                    if any(r['party']==party['id'] and r['state']=='committed' for r in self.reservations.values()):self.fail(409,'Party admission in progress')
                    if op=='party_promote' and (not isinstance(target,str) or target not in party['members'] or target==actor):self.fail(400,'Choose another current party member')
                    self.cancel_party_reservation(party['id'])
                    if op=='party_disband':del self.parties[party['id']]
                    else:
                        party['leader']=target
                        for member in party['members']:party['members'][member]=False
                elif op=='party_leave':
                    if not party:self.fail(404,'Not in a party')
                    if any(r['party']==party['id'] and r['state']=='committed' for r in self.reservations.values()):self.fail(409,'Party admission in progress')
                    self.cancel_party_reservation(party['id']);del party['members'][actor]
                    if not party['members']:del self.parties[party['id']]
                    else:
                        if party['leader']==actor:party['leader']=next(iter(party['members']))
                        for m in party['members']:party['members'][m]=False
                elif op=='party_ready':
                    if not party:self.fail(404,'Not in a party')
                    if type(data.get('ready')) is not bool:self.fail(400,'Ready must be boolean')
                    if self.active_reservation(party['id']):self.fail(409,'Party admission in progress')
                    party['members'][actor]=data['ready']
                elif op=='party_join':self.reserve(actor,party,data,directory,now)
                else:self.fail(400,'Unsupported social action')
                return 200,self.snapshot(actor)
            except SocialError as e:return e.status,dict(error=e.message)
    def reserve(self,actor,party,data,directory,now):
        if not party or party['leader']!=actor:self.fail(403,'Only party leader can join a room')
        if not all(party['members'].values()):self.fail(409,'All party members must be ready')
        if self.active_reservation(party['id']):self.fail(409,'Party admission already in progress')
        room=None;code=data.get('code','');rid=data.get('roomId','')
        if code:
            if not self.identifier(code):self.fail(400,'Invalid invitation')
            room=next((r for r in directory.rooms.values() if hmac.compare_digest(r['code'],code)),None)
        elif isinstance(rid,str) and rid in directory.rooms and (directory.rooms[rid]['public'] or directory.rooms[rid].get('hostId')==actor):room=directory.rooms[rid]
        if not room:self.fail(404,'Room not found or invitation invalid')
        if not room.get('supportsParty'):self.fail(409,'Host version does not support atomic party admission')
        resident=actor if room.get('hostId')==actor else None
        if room.get('hostId') in party['members'] and not resident:self.fail(409,'The resident host must lead this party')
        members=[m for m in party['members'] if m!=resident]
        count=len(members);pending=self.room_reservations(room['id'])
        if room['players']+sum(len(r['tickets']) for r in pending)+count>8:self.fail(409,'Room cannot fit the whole party')
        teams=[room['teamA'],room['teamB']]
        for r in pending:teams[r['team']]+=len(r['tickets'])
        possible=[t for t in (0,1) if teams[t]+count<=4]
        if resident:
            if room.get('hostTeam') not in (0,1):self.fail(409,'Host team presence not yet available')
            possible=[t for t in possible if t==room['hostTeam']]
        if not possible:self.fail(409,'No team can fit the whole party')
        team=min(possible,key=lambda t:teams[t]);key=secrets.token_hex(16)
        self.reservations[key]=dict(id=key,party=party['id'],room=room['id'],endpoint=room['endpoint'],team=team,tickets={m:secrets.token_hex(32) for m in members},resident=resident,verified=set(),until=now+30,state='preparing' if members else 'accepted')
