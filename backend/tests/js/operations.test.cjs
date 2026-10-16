/* Execute the real operations client across paging, SSE and explicit unknown retry. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const listeners = {}, bodyListeners = {}, requests = [], navigations = [], refreshes = [];
let checks = [], confirmed = false;
const counter = {textContent:''}, retryResult = {textContent:''}, outboxResult = {textContent:''};
const detail = {dataset:{uiSseUrl:'/jobs/detail/live?job_id=1'}};
const panel = {dataset:{syncFailuresJob:'1',failureAfterId:'0'},
  querySelectorAll:selector => selector === '.sync-failure-select' ? checks : [counter],
  querySelector:() => retryResult,
};
const document = {
  querySelector:() => panel,
  getElementById:id => id === 'job-detail-live' ? detail : outboxResult,
  addEventListener:(name,fn) => {listeners[name]=fn;},
  body:{addEventListener:(name,fn) => {bodyListeners[name]=fn;}},
};
const location = {origin:'http://isolated.test',assign:url=>navigations.push(url)};
vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../../app/static/operations.js'),'utf8'),{
  document,location,URL,console,confirm:()=>confirmed,
  refreshUiSseTargets:channel=>refreshes.push(channel),
  fetch:async (url,options)=>{requests.push({url,body:JSON.parse(options.body)});return {ok:true,json:async()=>({url:'/jobs?job_id=9'})};},
});
function checkbox(id){return {checked:false,dataset:{failureId:String(id)},matches:selector=>selector==='.sync-failure-select'};}
function change(input,value){input.checked=value;listeners.change({target:input});}
function retryButton(all=false){const button={disabled:false,hasAttribute:name=>name===(all?'data-sync-retry-all':'data-sync-retry-selected')};return {button,target:{closest:selector=>selector.includes('data-sync-retry-selected')?button:null}};}

(async()=>{
  const first=checkbox(1);checks=[first];change(first,true);
  assert.equal(first.checked,true);assert.equal(counter.textContent,1);
  const second=checkbox(2);checks=[second];panel.dataset.failureAfterId='50';bodyListeners['htmx:afterSwap']();
  assert.equal(second.checked,false);assert.equal(counter.textContent,1);
  change(second,true);assert.equal(counter.textContent,2);
  detail.dataset.uiSseUrl='/jobs/detail/live?job_id=1';bodyListeners['htmx:afterSettle']();
  assert.match(detail.dataset.uiSseUrl,/failure_after_id=50/);
  const restored=checkbox(1);checks=[restored];panel.dataset.failureAfterId='0';bodyListeners['htmx:afterSwap']();
  assert.equal(restored.checked,true);assert.equal(counter.textContent,2);
  const retry=retryButton();await listeners.click({target:retry.target});
  assert.deepEqual(requests.at(-1),{url:'/api/jobs/1/retry-failures',body:{failure_ids:[1,2]}});
  assert.equal(navigations.at(-1),'/jobs?job_id=9');
  panel.dataset.syncFailuresJob='2';checks=[checkbox(99)];bodyListeners['htmx:afterSwap']();
  assert.equal(counter.textContent,0);assert.equal(checks[0].checked,false);
  const before=requests.length;await listeners.click({target:retryButton().target});
  assert.equal(requests.length,before);assert.match(retryResult.textContent,/Выберите/);
  const button={disabled:false,dataset:{outboxRetry:'17',botKey:'hevc',deliveryUnknown:'1'}};
  const target={closest:selector=>selector==='[data-outbox-retry]'?button:null};
  await listeners.click({target});assert.equal(requests.length,before);
  confirmed=true;await listeners.click({target});
  assert.deepEqual(requests.at(-1),{url:'/api/telegram-outbox/17/retry',body:{bot_key:'hevc',acknowledge_unknown:true}});
  assert.equal(refreshes.at(-1),'telegram-outbox');assert.equal(button.disabled,false);
  for (let id=1000; id<1100; id++) change(checkbox(id),true);
  const overflow=checkbox(1100);checks=[overflow];change(overflow,true);
  assert.equal(counter.textContent,100);assert.equal(overflow.checked,false);
  assert.match(retryResult.textContent,/до 100 ошибок/);
  const clear={closest:selector=>selector==='[data-sync-clear-selection]'?{}:null};
  await listeners.click({target:clear});
  assert.equal(counter.textContent,0);assert.match(retryResult.textContent,/всех страницах/);
  checks=[checkbox(1000)];bodyListeners['htmx:afterSwap']();assert.equal(checks[0].checked,false);
  console.log('Operations client: exact selections, paging/SSE cursor, selection limit/reset, job isolation and unknown acknowledgement passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
