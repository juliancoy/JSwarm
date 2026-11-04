import {test} from 'node:test';
import assert from 'node:assert/strict';
import {connectionRows,ConnectionFeed} from '../view/machine-connections.js';
test('stale and failed feeds do not preserve connected status',()=>{
 const data={hosts:[{host:'laptop',status:'connected',checked_at:100}],stale_after_seconds:5};
 assert.equal(connectionRows(data,102000)[0].status,'connected');
 assert.equal(connectionRows(data,106000)[0].status,'unknown');
 assert.equal(connectionRows({...data,inventory_available:false},102000)[0].status,'unknown');
});
test('reconnect immediately starts a fresh request and ignores old responses',async()=>{
 const replies=[],changes=[];const feed=new ConnectionFeed({read:()=>new Promise(resolve=>replies.push(resolve)),changed:rows=>changes.push(rows),interval:100000});
 feed.start();feed.reconnect();assert.equal(replies.length,2);
 const response=host=>({ok:true,json:async()=>({hosts:[{host,status:'connected',checked_at:Date.now()/1000}]})});
 replies[1](response('fresh'));await new Promise(r=>setTimeout(r,0));
 replies[0](response('stale'));await new Promise(r=>setTimeout(r,0));
 assert.deepEqual(changes.flat().map(row=>row.host),['fresh']);feed.stop();
});
test('feed failures retain names but remove online claims',async()=>{
 const changes=[];let fail=false;const feed=new ConnectionFeed({read:async()=>{if(fail)throw Error('offline');return {ok:true,json:async()=>({hosts:[{host:'peer',status:'connected',checked_at:Date.now()/1000}]})}},changed:(rows,error)=>changes.push({rows,error}),interval:100000});
 feed.start();await new Promise(r=>setTimeout(r,0));clearTimeout(feed.timer);fail=true;await feed.refresh();feed.stop();
 assert.equal(changes.at(-1).rows[0].status,'unknown');assert.match(changes.at(-1).error,/unavailable/);
});
