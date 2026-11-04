import test from 'node:test';
import assert from 'node:assert/strict';
import {categoryPercent,categoryMean,categoryDelta,deviationScale} from '../view/genome-comparison.js';
import data from '../view/genome-data.js';
const sample=(count,same=undefined)=>({genome_composition:{reference_positions:10000,counts:{single:count,same:count},same}});
test('category means use actual percentages and exclude unavailable matches, not observed zeros',()=>{
 const samples=[sample(10),sample(20),sample(0),sample(0,null)];
 assert.ok(Math.abs(categoryMean(samples,'single')-.075)<1e-12);
 assert.equal(categoryPercent(samples[3],'same'),null);
 assert.ok(Math.abs(categoryMean(samples,'same')-.1)<1e-12);
 assert.equal(categoryDelta(samples,samples[3],'same'),null);
 assert.equal(categoryMean([sample(0,null)],'same'),null);
});
test('differences are percentage points and sum to zero among available samples',()=>{
 for(const key of ['single','insertions','deletions','other','same','unknown']){
  const deltas=data.samples.map(sample=>categoryDelta(data.samples,sample,key)).filter(value=>value!==null);
  assert.ok(Math.abs(deltas.reduce((a,b)=>a+b,0))<1e-10);
 }
 const deltas=data.samples.map(sample=>categoryDelta(data.samples,sample,'single'));
 assert.ok(Math.min(...deltas)<0);assert.ok(Math.max(...deltas)>0);assert.ok(new Set(deltas).size>3);
});
test('linear and adjusted log scales preserve sign, zero, ordering and a shared maximum',()=>{
 for(const strength of [0,2,4,8]){
  assert.equal(deviationScale(0,42,strength),0);assert.equal(deviationScale(42,42,strength),1);
  assert.equal(deviationScale(-42,42,strength),-1);
  let previous=0;for(const value of [.000001,.0001,.01,1,42]){const scaled=deviationScale(value,42,strength);assert.ok(scaled>previous);assert.equal(deviationScale(-value,42,strength),-scaled);previous=scaled;}
 }
 assert.ok(deviationScale(.001,42,8)>deviationScale(.001,42,0));
});
