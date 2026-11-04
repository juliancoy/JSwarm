import test from 'node:test';
import assert from 'node:assert/strict';
import {pollVisible} from '../view/visible-poll.js';
function clock(hidden=false) {
  let listener, next=0;
  const timers=new Map();
  return {document:{hidden,addEventListener:(_,fn)=>listener=fn,removeEventListener:()=>listener=null},
    setTimeout:fn=>{timers.set(++next,fn);return next;},clearTimeout:id=>timers.delete(id),timers,
    show(value){this.document.hidden=!value;listener?.();}};
}
const settle=()=>new Promise(resolve=>setImmediate(resolve));
test('hidden pages stop polling, keep their snapshot and refresh immediately on return',async()=>{
  const env=clock();let calls=0,snapshot;
  const stop=pollVisible(async()=>{snapshot=++calls;},1000,env);
  await settle();assert.equal(calls,1);assert.equal(env.timers.size,1);
  env.show(false);assert.equal(env.timers.size,0);assert.equal(snapshot,1);
  env.show(true);await settle();assert.equal(calls,2);assert.equal(env.timers.size,1);
  stop();assert.equal(env.timers.size,0);
});
test('initially hidden pages fetch nothing until visible',async()=>{
  const env=clock(true);let calls=0;
  const stop=pollVisible(async()=>{calls++;},1000,env);
  await settle();assert.equal(calls,0);assert.equal(env.timers.size,0);
  env.show(true);await settle();assert.equal(calls,1);stop();
});
test('visibility changes cannot overlap requests or discard an in-flight result',async()=>{
  const env=clock();let calls=0,finish,snapshot;
  const stop=pollVisible(async()=>{calls++;snapshot=await new Promise(resolve=>finish=resolve);},1000,env);
  env.show(false);env.show(true);assert.equal(calls,1);
  env.show(false);finish('complete');await settle();
  assert.equal(snapshot,'complete');assert.equal(env.timers.size,0);stop();
});
