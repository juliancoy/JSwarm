import {test} from 'node:test';
import assert from 'node:assert/strict';
import {issueTicket,verifyTicket,credentialHash,type Ticket} from '../src/auth';

const secret='synthetic-launcher-test-key-at-least-thirty-two-characters';
function claims(aud:Ticket['aud']):Ticket {
 const iat=Math.floor(Date.now()/1000);
 return {aud,room:'a'.repeat(64),peer:'11111111-1111-1111-1111-111111111111',iat,exp:iat+300};
}
test('launcher enrollment and admin tickets cannot open a data session',async()=>{
 for(const aud of ['launcher-enroll-v1','launcher-admin-v1','launcher-backup-v1'] as const){
  const token=await issueTicket(claims(aud),secret);
  await assert.rejects(()=>verifyTicket(token,secret));
  assert.equal((await verifyTicket(token,secret,Date.now(),[aud])).aud,aud);
 }
});
test('session tickets cannot enroll devices or revoke another device',async()=>{
 const token=await issueTicket(claims('launcher-session-v1'),secret);
 assert.equal((await verifyTicket(token,secret)).aud,'launcher-session-v1');
 await assert.rejects(()=>verifyTicket(token,secret,Date.now(),['launcher-enroll-v1']));
 await assert.rejects(()=>verifyTicket(token,secret,Date.now(),['launcher-admin-v1']));
 await assert.rejects(()=>verifyTicket(token,secret,Date.now(),['launcher-backup-v1']));
});
test('device credentials are validated and stored only as a hash',async()=>{
 const value='b'.repeat(64),hash=await credentialHash(value);
 assert.match(hash,/^[a-f0-9]{64}$/);assert.notEqual(hash,value);
 await assert.rejects(()=>credentialHash('short'));
});
