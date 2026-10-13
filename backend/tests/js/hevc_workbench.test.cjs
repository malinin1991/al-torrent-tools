/* Execute the real page script with a small DOM boundary, no network/packages. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function element() {
  return {dataset:{}, listeners:{}, textContent:"", disabled:false, children:[],
    addEventListener(type, handler) { this.listeners[type] = handler; },
    append(child) { this.children.push(child); },
  };
}
function check(id, identity, disabled=false) {
  return {...element(), checked:false, disabled, dataset:{fileId:String(id), selectionKey:`${id}:${identity}`}, closest() { return this; }};
}
function harness(initial) {
  const root = element();
  let checks = initial;
  let details = [{dataset:{sseKey:"details-a"}, open:true}];
  const state = {dataset:{hevcLiveUrl:"/hevc-workbench/live?view=all&page=1"}};
  root.querySelector = selector => selector === "[data-hevc-state]" ? state : selector === "[data-encoder-enabled='1']" ? state : null;
  root.querySelectorAll = selector => selector === ".hevc-encode-check" ? checks : selector.includes("[open]") ? details.filter(item => item.open) : details;
  const buttons = Object.fromEntries(["send","clear","select","refresh"].map(name => [`[data-hevc-${name}]`, element()]));
  const actions = {...element(), querySelector:selector => buttons[selector]};
  const nodes = {"hevc-workbench-live":root, "hevc-workbench-actions":actions, "hevc-workbench-result":element(), "hevc-workbench-count":element(), "hevc-workbench-selection-notice":element()};
  const body = element();
  const requests = [];
  const submissions = [];
  const scroll = [];
  const window = {scrollY:321, requestAnimationFrame:fn => fn(), scrollTo:(...args) => scroll.push(args),
    htmx:{ajax:(...args) => requests.push(args)},
    pickEncoderPreset:async () => ({video_id:"hevc",audio_id:"opus"}),
    submitEncoderFiles:async (...args) => { submissions.push(args); return {file_ids:[1],items:[{file_id:1,status:"accepted"}]}; },
    showEncoderReceipt:(target,payload) => { target.textContent = `accepted ${payload.file_ids.length}`; },
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,"../../app/static/hevc_workbench.js"),"utf8"), {
    window, document:{body,getElementById:id => nodes[id],createElement:() => element()}, console,
  });
  return {root,nodes,body,buttons,window,requests,submissions,scroll,
    swap(nextChecks, nextDetails=[{dataset:{sseKey:"details-a"},open:false}]) {
      const event = {detail:{target:root,shouldSwap:true}};
      body.listeners["htmx:beforeSwap"](event);
      if (event.detail.shouldSwap) { checks = nextChecks; details = nextDetails; body.listeners["htmx:afterSwap"]({detail:{target:root}}); }
      return {event,checks,details};
    },
  };
}

(async () => {
  const a = check(1,"hash-a:Show/a.mkv"), b = check(2,"hash-b:Show/b.mkv"), blocked = check(3,"hash-c:Show/c.mkv",true);
  const ui = harness([a,b,blocked]);
  ui.buttons["[data-hevc-select]"].listeners.click();
  assert.equal(ui.nodes["hevc-workbench-count"].textContent,"2");
  assert.equal(blocked.checked,false);
  const nextA = check(1,"hash-a:Show/a.mkv"), changedB = check(2,"hash-new:Show/b.mkv");
  const swapped = ui.swap([nextA,changedB]);
  assert.equal(nextA.checked,true);
  assert.equal(changedB.checked,false); // Same file ID cannot select another version.
  assert.equal(ui.nodes["hevc-workbench-count"].textContent,"1");
  assert.match(ui.nodes["hevc-workbench-selection-notice"].textContent,/Снят выбор с 1/);
  assert.equal(swapped.details[0].open,true);
  assert.deepEqual(ui.scroll.at(-1),[0,321]);
  let choose;
  ui.window.pickEncoderPreset = () => new Promise(resolve => { choose = resolve; });
  const sending = ui.buttons["[data-hevc-send]"].listeners.click();
  const duringSend = ui.swap([check(1,"hash-a:Show/a.mkv")]);
  assert.equal(duringSend.event.detail.shouldSwap,false);
  assert.equal(ui.buttons["[data-hevc-send]"].disabled,true);
  choose({video_id:"hevc",audio_id:"opus"});
  await sending;
  assert.deepEqual(JSON.parse(JSON.stringify(ui.submissions[0])),[[1],{video_id:"hevc",audio_id:"opus"},{noRepeatPrompt:true}]);
  assert.equal(ui.nodes["hevc-workbench-count"].textContent,"0");
  assert.equal(nextA.disabled,true);
  assert.equal(ui.requests.length,1);
  assert.equal(ui.requests[0][1],"/hevc-workbench/live?view=all&page=1");

  const failure = harness([check(1,"hash-a:Show/a.mkv")]);
  failure.buttons["[data-hevc-select]"].listeners.click();
  failure.window.submitEncoderFiles = async () => { const error = new Error("409: проверьте историю"); error.historyUrl = "/encoder-history?submission_id=7"; throw error; };
  await failure.buttons["[data-hevc-send]"].listeners.click();
  assert.match(failure.nodes["hevc-workbench-result"].textContent,/409/);
  assert.equal(failure.nodes["hevc-workbench-result"].children.at(-1).href,"/encoder-history?submission_id=7");
  assert.equal(failure.nodes["hevc-workbench-count"].textContent,"1");
  assert.equal(failure.requests.length,1);
  console.log("HEVC workbench client: selection/version/SSE/details/scroll/send/conflict checks passed");
})().catch(error => { console.error(error); process.exitCode = 1; });
