import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createHmac,randomUUID} from 'node:crypto';
import {verifyTicket} from '../src/auth';

const secret='synthetic-test-key-at-least-thirty-two-characters';
function sign(overrides:Record<string,unknown>={},key=secret) {
 const now=Math.floor(Date.now()/1000);
 const claims={aud:'archive-peers-v1',room:'a'.repeat(64),peer:randomUUID(),iat:now,exp:now+300,...overrides};
 const encode=(v:unknown)=>Buffer.from(JSON.stringify(v)).toString('base64url');
 const body=encode({alg:'HS256',typ:'JWT'})+'.'+encode(claims);
 return body+'.'+createHmac('sha256',key).update(body).digest('base64url');
}
test('device invitations bind a room and reject tampering, wrong keys, expiry and excessive lifetime',async()=>{
 const good=sign();assert.equal((await verifyTicket(good,secret)).room,'a'.repeat(64));
 for (const token of [sign({},'wrong key'),sign({exp:1}),sign({exp:Math.floor(Date.now()/1000)+7200}),sign({aud:'another-service'}),sign({room:'../../other-room'}),sign({peer:'guest'}),good+'.extra']) {
  await assert.rejects(()=>verifyTicket(token,secret));
 }
 const parts=good.split('.');parts[1]=Buffer.from(JSON.stringify({room:'b'.repeat(64)})).toString('base64url');
 await assert.rejects(()=>verifyTicket(parts.join('.'),secret));
});
