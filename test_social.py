import tempfile, time, unittest
from pathlib import Path
from registry import Directory

class SocialTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=str(Path(self.temp.name)/'social.sqlite3');self.d=Directory(self.path)
        self.users=[self.register('Player '+str(i)) for i in range(6)]
    def tearDown(self):self.d.social.close();self.temp.cleanup()
    def call(self,user,op=None,method='POST',path='/v1/social/action',**data):
        if op:data['op']=op
        return self.d.handle(method,path,data,'Bearer '+user['token'],'test')
    def register(self,name):
        status,body=self.d.handle('POST','/v1/social/register',dict(name=name),'','register')
        self.assertEqual(status,201);return body
    def state(self,u):return self.call(u,method='GET',path='/v1/social/state')[1]
    def friend(self,a,b):
        self.assertEqual(self.call(a,'friend_request',target=b['id'])[0],200)
        self.assertEqual(self.call(b,'friend_accept',target=a['id'])[0],200)
    def party(self,count=2):
        a=self.users[0];self.assertEqual(self.call(a,'party_create')[0],200)
        for b in self.users[1:count]:
            self.friend(a,b);self.assertEqual(self.call(a,'party_invite',target=b['id'])[0],200)
            inv=self.state(b)['invitations'][0]['id'];self.assertEqual(self.call(b,'party_accept',invite=inv)[0],200)
        for b in self.users[:count]:self.assertEqual(self.call(b,'party_ready',ready=True)[0],200)
        return a
    def room(self,players=1,a=1,b=0):
        status,r=self.d.handle('POST','/v1/rooms',dict(name='Room',public=True,map='BioLab',mode=0,endpoint='127.0.0.1:7777'),'','host');self.assertEqual(status,201)
        auth='Bearer '+r['ownerToken'];self.d.handle('PATCH','/v1/rooms/'+r['id'],dict(players=players,map='BioLab',mode=0,supportsParty=True,teamA=a,teamB=b),auth,'host');return r,auth
    def test_credential_and_identity(self):
        a,b=self.users[:2];self.assertNotEqual(a['id'],b['id']);self.assertNotEqual(a['token'],b['token'])
        self.assertEqual(self.d.handle('GET','/v1/social/state',{},'Bearer '+'0'*64,'test')[0],401)
        status,result=self.call(a,method='POST',path='/v1/social/search',query='Player')
        self.assertEqual(status,200);self.assertEqual(len(result['players']),5);self.assertNotIn('token',str(result));self.assertNotIn('secret',str(result))
        # Supplied actor identity is ignored; bearer is the only principal.
        self.call(a,'friend_request',target=b['id'],playerId=b['id'])
        self.assertEqual(self.state(b)['requests'][0]['id'],a['id'])
    def test_friend_accept_decline_remove_restart(self):
        a,b,c=self.users[:3];self.friend(a,b)
        self.assertEqual(self.call(a,'friend_request',target=a['id'])[0],400)
        self.assertEqual(self.call(a,'friend_request',target=b['id'])[0],409)
        self.call(c,'friend_request',target=a['id']);self.assertEqual(self.call(c,'friend_request',target=a['id'])[0],409)
        self.assertEqual(self.call(a,'friend_decline',target=c['id'])[0],200)
        self.d.social.close();self.d=Directory(self.path)
        self.assertEqual(self.state(a)['friends'][0]['id'],b['id'])
        self.assertEqual(self.call(a,'friend_remove',target=b['id'])[0],200);self.assertEqual(self.state(b)['friends'],[])
    def test_mutation_request_ids_are_authenticated_and_idempotent(self):
        a,b=self.users[:2];self.friend(a,b)
        key='a'*32
        self.assertEqual(self.call(a,'party_create',requestId=key)[0],200)
        self.assertEqual(self.call(a,'party_create',requestId=key)[0],200)
        self.assertEqual(self.call(a,'party_leave',requestId=key)[0],409)
        self.assertEqual(self.call(a,'party_invite',target=b['id'],requestId='b'*32)[0],200)
        self.assertEqual(self.call(a,'party_invite',target=b['id'],requestId='b'*32)[0],200)
        self.assertEqual(len(self.state(b)['invitations']),1)
        invite=self.state(b)['invitations'][0]['id']
        self.assertEqual(self.call(b,'party_accept',invite=invite,requestId='c'*32)[0],200)
        self.assertEqual(self.call(b,'party_accept',invite=invite,requestId='c'*32)[0],200)
        self.assertEqual(len(self.state(a)['party']['members']),2)
        # A request ID is not an authorization capability for another player.
        self.assertEqual(self.call(b,'party_invite',target=a['id'],requestId='b'*32)[0],403)
        self.assertEqual(self.call(a,'party_leave',requestId='not-a-request-id')[0],400)
        self.assertEqual(self.d.handle('POST','/v1/social/action',dict(op='party_create',requestId='d'*32),'Bearer '+'0'*64,'test')[0],401)
    def test_old_party_admission_does_not_follow_new_party(self):
        a=self.party(2);party=self.state(a)['party']['id']
        self.d.social.reservations['d'*32]=dict(id='d'*32,party=party,room='e'*32,endpoint='127.0.0.1:7777',tickets={a['id']:'f'*64},resident=None,team=0,state='accepted',until=time.monotonic()+30)
        self.assertEqual(self.state(a)['admission']['partyId'],party)
        self.assertEqual(self.call(a,'party_disband')[0],200)
        self.assertIsNone(self.state(a)['admission'])
        self.assertEqual(self.call(a,'party_create')[0],200)
        self.assertIsNone(self.state(a)['admission'])
    def test_authenticated_room_creation_retry_returns_same_room(self):
        a,b=self.users[:2];data=dict(name='reliable room',public=False,map='BioLab',mode=0,endpoint='127.0.0.1:7777',requestId='a'*32)
        create=lambda u,d:self.d.handle('POST','/v1/rooms',d,'Bearer '+u['token'] if u else '', 'test')
        first=create(a,data);self.assertEqual(first[0],201)
        self.assertEqual(create(a,data),first);self.assertEqual(len(self.d.rooms),1)
        self.assertEqual(create(a,dict(data,name='changed'))[0],409)
        self.assertEqual(create(None,data)[0],401)
        other=create(b,data);self.assertEqual(other[0],201);self.assertNotEqual(other[1]['ownerToken'],first[1]['ownerToken'])
    def test_first_invite_creates_party_atomically(self):
        a,b,c=self.users[:3];self.friend(a,b)
        self.assertEqual(self.call(a,'party_invite',target=b['id'],requestId='a'*32)[0],200)
        self.assertEqual(self.state(a)['party']['leader'],a['id'])
        self.assertEqual(self.call(a,'party_invite',target=b['id'],requestId='a'*32)[0],200)
        self.assertEqual(len(self.state(b)['invitations']),1)
        self.assertEqual(self.call(c,'party_invite',target=b['id'])[0],403)
        self.assertIsNone(self.state(c)['party'])
    def test_party_capacity_leader_and_permissions(self):
        a=self.party(4);b=self.users[1];fifth=self.users[4]
        self.friend(a,fifth);self.assertEqual(self.call(a,'party_invite',target=fifth['id'])[0],409)
        self.assertEqual(self.call(b,'party_invite',target=fifth['id'])[0],403)
        self.assertEqual(self.call(b,'party_join',roomId='0'*32)[0],403)
        self.assertEqual(self.call(a,'party_leave')[0],200)
        p=self.state(b)['party'];self.assertEqual(p['leader'],b['id']);self.assertEqual(len(p['members']),3);self.assertFalse(any(m['ready'] for m in p['members']))
        for u in self.users[1:4]:self.assertEqual(self.call(u,'party_leave')[0],200)
        self.assertIsNone(self.state(b)['party'])
    def test_invitation_ownership_decline_and_expiry(self):
        a,b,c=self.users[:3];self.friend(a,b);self.call(a,'party_create');self.call(a,'party_invite',target=b['id'])
        inv=self.state(b)['invitations'][0]['id'];self.assertEqual(self.call(c,'party_accept',invite=inv)[0],404)
        self.assertEqual(self.call(b,'party_decline',invite=inv)[0],200)
        self.call(a,'party_invite',target=b['id']);inv=self.state(b)['invitations'][0]['id'];self.d.social.invites[inv]['until']=0
        self.assertEqual(self.call(b,'party_accept',invite=inv)[0],404)
    def test_party_whole_team_reservation_commit(self):
        a=self.party(3);r,auth=self.room(players=4,a=2,b=2)
        self.assertEqual(self.call(a,'party_join',roomId=r['id'])[0],409)
        self.d.handle('PATCH','/v1/rooms/'+r['id'],dict(players=2,map='BioLab',mode=0,supportsParty=True,teamA=1,teamB=1),auth,'host')
        status,result=self.call(a,'party_join',roomId=r['id']);self.assertEqual(status,200);reservation=result['admission']['reservationId']
        tickets=[self.state(u)['admission'] for u in self.users[:3]];self.assertEqual(len(set(t['team'] for t in tickets)),1)
        self.assertIsNone(self.state(self.users[4])['admission'])
        prefix='/v1/rooms/'+r['id'];members=[u['id'] for u in self.users[:3]]
        self.assertEqual(self.d.handle('POST',prefix+'/party/commit',dict(reservationId=reservation,members=members),auth,'host')[0],409)
        for i,t in enumerate(tickets):
            code,body=self.d.handle('POST',prefix+'/verify',dict(ticket=t['ticket']),auth,'host');self.assertEqual(code,200);self.assertEqual(body['playerId'],self.users[i]['id']);self.assertEqual(set(body['members']),set(members))
        self.assertEqual(self.d.handle('POST',prefix+'/verify',dict(ticket=tickets[0]['ticket']),auth,'host')[0],403)
        self.assertEqual(self.d.handle('POST',prefix+'/party/commit',dict(reservationId=reservation,members=members),a['token'],'host')[0],403)
        self.assertEqual(self.d.handle('POST',prefix+'/party/commit',dict(reservationId=reservation,members=members[:1]),auth,'host')[0],400)
        self.assertEqual(self.d.handle('POST',prefix+'/party/commit',dict(reservationId=reservation,members=members),auth,'host')[0],200)
        self.assertEqual(self.state(a)['admission']['state'],'committed')
        self.assertEqual(self.d.handle('POST',prefix+'/party/complete',dict(reservationId=reservation,members=members),auth,'host')[0],200)
        self.assertEqual(self.state(a)['admission']['state'],'accepted')
    def test_unready_party_reserves_nothing_and_retry_issues_fresh_tickets(self):
        a=self.party(2);b=self.users[1];r,auth=self.room()
        self.call(b,'party_ready',ready=False)
        self.assertEqual(self.call(a,'party_join',roomId=r['id'])[0],409)
        self.assertEqual(self.d.social.reservations,{})
        self.call(b,'party_ready',ready=True)
        status,first=self.call(a,'party_join',roomId=r['id']);self.assertEqual(status,200)
        old_ticket=self.state(b)['admission']['ticket']
        self.assertEqual(self.call(a,'party_join',roomId=r['id'])[0],409)
        prefix='/v1/rooms/'+r['id']
        self.assertEqual(self.d.handle('POST',prefix+'/party/cancel',dict(reservationId=first['admission']['reservationId']),auth,'host')[0],200)
        status,second=self.call(a,'party_join',roomId=r['id']);self.assertEqual(status,200)
        self.assertNotEqual(first['admission']['reservationId'],second['admission']['reservationId'])
        self.assertNotEqual(old_ticket,self.state(b)['admission']['ticket'])
        self.assertEqual(self.d.handle('POST',prefix+'/verify',dict(ticket=old_ticket),auth,'host')[0],403)
    def test_legacy_host_and_cancel_expiry(self):
        a=self.party();r,auth=self.room();prefix='/v1/rooms/'+r['id']
        self.d.rooms[r['id']]['supportsParty']=False;self.assertEqual(self.call(a,'party_join',roomId=r['id'])[0],409)
        self.d.rooms[r['id']]['supportsParty']=True;status,result=self.call(a,'party_join',roomId=r['id']);self.assertEqual(status,200)
        key=result['admission']['reservationId'];self.assertEqual(self.d.handle('POST',prefix+'/party/cancel',dict(reservationId=key),auth,'host')[0],200)
        self.assertEqual(self.state(a)['admission']['state'],'cancelled')
        status,result=self.call(a,'party_join',roomId=r['id']);self.assertEqual(status,200);key=result['admission']['reservationId'];self.d.social.reservations[key]['until']=time.monotonic()-1
        self.assertEqual(self.state(a)['admission']['state'] if self.state(a)['admission'] else 'expired','expired')
    def test_disconnected_leader_transfers_after_presence_lease(self):
        a=self.party();b=self.users[1]
        self.d.social.presence[a['id']]=('offline',time.monotonic())
        self.assertEqual(self.state(b)['party']['leader'],b['id'])
        self.assertFalse(any(m['ready'] for m in self.state(b)['party']['members']))
    def test_resident_host_excluded_from_admission_and_same_team(self):
        a=self.party(4)
        status,r=self.d.handle('POST','/v1/rooms',dict(name='Host room',public=False,map='BioLab',mode=0,endpoint='127.0.0.1:7777'),'Bearer '+a['token'],'host');self.assertEqual(status,201)
        auth='Bearer '+r['ownerToken']
        self.assertEqual(self.d.handle('PATCH','/v1/rooms/'+r['id'],dict(players=1,map='BioLab',mode=0,supportsParty=True,teamA=1,teamB=0,hostTeam=0),auth,'host')[0],200)
        status,result=self.call(a,'party_join',roomId=r['id']);self.assertEqual(status,200)
        self.assertTrue(result['admission']['resident']);self.assertEqual(result['admission']['ticket'],'');self.assertEqual(result['admission']['team'],0)
        reservation=self.d.social.reservations[result['admission']['reservationId']]
        self.assertEqual(len(reservation['tickets']),3);self.assertNotIn(a['id'],reservation['tickets'])
        for u in self.users[1:4]:self.assertFalse(self.state(u)['admission']['resident']);self.assertEqual(self.state(u)['admission']['team'],0)
    def test_explicit_promote_and_disband(self):
        a=self.party();b=self.users[1]
        self.assertEqual(self.call(b,'party_disband')[0],403)
        self.assertEqual(self.call(a,'party_promote',target=self.users[5]['id'])[0],400)
        self.assertEqual(self.call(a,'party_promote',target=b['id'])[0],200)
        self.assertEqual(self.state(a)['party']['leader'],b['id'])
        self.assertEqual(self.call(b,'party_disband')[0],200);self.assertIsNone(self.state(a)['party'])
    def test_presence_and_disabled_storage(self):
        a,b=self.users[:2];self.friend(a,b);self.call(b,method='POST',path='/v1/social/presence',status='playing')
        self.assertEqual(self.state(a)['friends'][0]['status'],'playing')
        self.d.social.presence[b['id']]=('playing',0);self.assertEqual(self.state(a)['friends'][0]['status'],'offline')
        disabled=Directory();self.assertEqual(disabled.handle('POST','/v1/social/register',dict(name='x'),'','test')[0],503)
if __name__=='__main__':unittest.main()
