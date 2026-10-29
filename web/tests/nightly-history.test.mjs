import test from 'node:test';
import assert from 'node:assert/strict';
import {normalizeHistory,fetchHistory,HISTORY_API} from '../nightly_history.js';
const row=(extra={})=>({id:123,path:'.github/workflows/nightly-readme.yml',event:'workflow_dispatch',created_at:'2026-10-27T13:00:00Z',status:'completed',conclusion:'success',head_sha:'a'.repeat(40),...extra});
test('only a real schedule event is called a scheduled execution',()=>{
  assert.equal(normalizeHistory({workflow_runs:[row()]})[0].trigger,'manual');
  assert.equal(normalizeHistory({workflow_runs:[row({event:'schedule'})]})[0].trigger,'scheduled');
});
test('report links are constructed from numeric run IDs, never API-supplied URLs',()=>{
  const r=normalizeHistory({workflow_runs:[row({html_url:'javascript:alert(1)',head_sha:'<script>'})]})[0];
  assert.equal(r.url,'https://github.com/johncat88425-del/fresh-eyes/actions/runs/123');assert.equal(r.toolCommit,null);
  assert.deepEqual(normalizeHistory({workflow_runs:[row({id:'123'}),row({id:-1}),row({path:'other.yml'})]}),[]);
});
test('incomplete workflow outcomes remain unknown',()=>{
  assert.equal(normalizeHistory({workflow_runs:[row({conclusion:null})]})[0].conclusion,null);
  assert.equal(normalizeHistory({workflow_runs:[row({status:'in_progress',conclusion:null})]})[0].status,'running');
  assert.throws(()=>normalizeHistory({items:[]}),/invalid_history/);
});
test('history reads only the fixed public endpoint without cookies or authentication',async()=>{
  let calls=0;const rows=await fetchHistory({fetcher:async(url,options)=>{calls++;assert.equal(url,HISTORY_API);assert.equal(options.credentials,'omit');assert.equal(options.headers.Authorization,undefined);return {ok:true,json:async()=>({workflow_runs:[row()]})};}});
  assert.equal(calls,1);assert.equal(rows.length,1);
});
test('rate limits and absent workflow history do not become fabricated successful runs',async()=>{
  for(const [status,reason] of [[404,'not_ready'],[403,'rate_limited'],[500,'history_unavailable']]){
    await assert.rejects(()=>fetchHistory({fetcher:async()=>({ok:false,status})}),new RegExp(reason));
  }
});
