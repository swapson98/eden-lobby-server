import unittest
from registry import Directory
class RegistryTests(unittest.TestCase):
 def setUp(self):self.d=Directory();self.settings=dict(name='EDEN',public=True,map='BioLab',mode=0,endpoint='203.0.113.10:7777')
 def call(self,m,p,d=None,a=''):return self.d.handle(m,p,d or {},a,'test')
 def create(self,public=True):return self.call('POST','/v1/rooms',dict(self.settings,public=public))[1]
 def test_public_private_and_secrets(self):
  public=self.create();private=self.create(False);status,body=self.call('GET','/v1/rooms');self.assertEqual(len(body['rooms']),1);self.assertNotIn('endpoint',body['rooms'][0]);self.assertNotIn('ownerToken',body['rooms'][0]);self.assertEqual(self.call('POST','/v1/join',dict(roomId=private['id']))[0],404);self.assertEqual(self.call('POST','/v1/join',dict(code='bad'))[0],404);self.assertEqual(self.call('POST','/v1/join',dict(code=private['inviteCode']))[0],200)
 def test_ticket_single_use_and_host_authority(self):
  room=self.create(False);ticket=self.call('POST','/v1/join',dict(code=room['inviteCode']))[1]['ticket'];p='/v1/rooms/'+room['id']+'/verify';self.assertEqual(self.call('POST',p,dict(ticket=ticket))[0],403);self.assertEqual(self.call('POST',p,dict(ticket=ticket),'Bearer '+room['ownerToken'])[0],200);self.assertEqual(self.call('POST',p,dict(ticket=ticket),'Bearer '+room['ownerToken'])[0],403)
 def test_full_expired_and_closed(self):
  room=self.create();p='/v1/rooms/'+room['id'];self.assertEqual(self.call('PATCH',p,dict(players=8,map='Forge',mode=1),'Bearer '+room['ownerToken'])[0],200);self.assertEqual(self.call('POST','/v1/join',dict(roomId=room['id']))[0],409);self.assertEqual(self.call('DELETE',p)[0],403);self.assertEqual(self.call('DELETE',p,a='Bearer '+room['ownerToken'])[0],200);self.assertEqual(self.call('POST','/v1/join',dict(roomId=room['id']))[0],404);room=self.create();self.d.rooms[room['id']]['expiry']=0;self.assertEqual(self.call('GET','/v1/rooms')[1]['rooms'],[])
 def test_validation_and_limits(self):
  for value in ['http://bad:7777','a.com?listen:7777','a.com:0','a.com:7777/Map']:
   self.assertEqual(self.call('POST','/v1/rooms',dict(self.settings,endpoint=value))[0],400)
  for i in range(181):status,_=self.call('GET','/v1/rooms')
  self.assertEqual(status,429)
if __name__=='__main__':unittest.main()
