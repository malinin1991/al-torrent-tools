const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const source = fs.readFileSync(path.join(__dirname,"../../app/static/automatic_encoder.js"),"utf8");
const initial = {release_id:42,revision:3,video_id:"hevc",audio_id:"opus",audio_downmix:"stereo",video_label:"HEVC",audio_label:"Opus"};

function harness(page = "automatic", showStatus = true) {
  const listeners = {}, bodyListeners = {}, status = {textContent:""};
  const summary = {textContent:"before"};
  const row = {
    dataset:{policy:JSON.stringify(initial)},
    querySelectorAll(selector) {return selector === "[data-auto-policy-summary]" ? [summary] : [clear];},
    querySelector() {return edit;}
  };
  const button = kind => ({disabled:false, hidden:false,
    closest() {return row;}, hasAttribute(name){return name === "data-auto-policy-" + kind;},
    focus(options) {assert.equal(options.preventScroll,true);document.activeElement=this;}
  });
  const edit = button("edit"), clear = button("clear");
  const root = {id:page === "automatic" ? "automatic-encoder-live" : "releases-live", dataset:{}, checks:[],
    querySelectorAll(selector) {
      if (selector === "[data-auto-release]") return [row];
      return selector.includes(":checked") ? this.checks.filter(c => c.checked) : this.checks;
    },
    querySelector(selector) {return selector === "[data-auto-state]" ? {dataset:{liveQuery:"release_id=42&preview_page=2"}} : row;}
  };
  const requests = [], refreshes = [], dialogs = [];
  let resolveDialog;
  const document = {
    activeElement:null,
    body:{addEventListener:(name,fn) => {bodyListeners[name]=fn;}},
    addEventListener:(name,fn) => {listeners[name]=fn;},
    getElementById:id => id === root.id ? root : showStatus && id === (page === "automatic" ? "auto-encoder-result" : "releases-action-result") ? status : null,
    querySelectorAll:() => [row]
  };
  const sandbox = {
    document,
    fetch:async (url, options) => {
      requests.push({url,...options});
      const payload = JSON.parse(options.body);
      return {ok:true,json:async () => ({...payload,release_id:42,revision:payload.revision+1,
        video_label:payload.video_id === "copy" ? "Copy video" : payload.video_id,
        audio_label:payload.audio_id === "copy" ? "Copy audio" : payload.audio_id})};
    },
    pickEncoderPreset:options => {dialogs.push(options);return new Promise(resolve => {resolveDialog=resolve;});},
    refreshUiSseTargets:channel => refreshes.push(channel)
  };
  vm.runInNewContext(source,sandbox);
  return {listeners,bodyListeners,status,root,row,summary,edit,clear,requests,refreshes,dialogs,document,
    click(button = edit) {return listeners.click({target:{closest:() => button},preventDefault(){}});},
    resolve(layers) {resolveDialog(layers);}
  };
}

(async () => {
  const h = harness();
  const pending = h.click();
  await h.click();
  assert.equal(h.dialogs[0].initial.audio_downmix,"stereo");
  assert.equal(h.dialogs[0].submitLabel,"Сохранить для релиза");
  const swap = {detail:{target:h.root,shouldSwap:true}};
  h.bodyListeners["htmx:beforeSwap"](swap);
  assert.equal(swap.detail.shouldSwap,false,"An open preset dialog must defer SSE swaps");
  h.resolve({video_id:"copy",audio_id:"copy",audio_downmix:"none"});
  await pending;
  assert.equal(h.requests.length,1,"Repeated clicks must not save twice");
  assert.equal(h.requests[0].url,"/api/automatic-encoder/releases/42");
  assert.equal(h.requests[0].method,"PUT");
  assert.equal(JSON.parse(h.requests[0].body).revision,3);
  assert.equal(JSON.parse(h.row.dataset.policy).revision,4,"Confirmed revision must be usable before SSE finishes");
  assert.match(h.summary.textContent,/Copy video \/ Copy audio/);
  assert.match(h.status.textContent,/сохранены/);
  assert.deepEqual(h.refreshes,["automatic_encoder","releases"]);
  assert.equal(h.edit.disabled,false);

  // A live response started before PUT must not restore an obsolete assignment.
  h.row.dataset.policy = JSON.stringify(initial);
  h.bodyListeners["htmx:afterSwap"]({detail:{target:h.root}});
  assert.equal(JSON.parse(h.row.dataset.policy).revision,4);
  const reopened = h.click();
  assert.equal(h.dialogs[1].initial.revision,4);
  assert.equal(h.dialogs[1].initial.video_id,"copy");
  h.resolve(null);
  await reopened;
  assert.equal(h.requests.length,1,"Cancelling does not change the assignment");

  h.root.checks=[{checked:true,disabled:false,dataset:{selectionKey:"1:hash:A/ep.mkv"}},
                 {checked:true,disabled:false,dataset:{selectionKey:"2:hash:B/ep.mkv"}}];
  h.bodyListeners["htmx:beforeSwap"]({detail:{target:h.root,shouldSwap:true}});
  h.root.checks=[{checked:false,disabled:false,dataset:{selectionKey:"1:hash:A/ep.mkv"}},
                 {checked:false,disabled:true,dataset:{selectionKey:"2:hash:B/ep.mkv"}},
                 {checked:false,disabled:false,dataset:{selectionKey:"3:new:C/ep.mkv"}}];
  h.bodyListeners["htmx:afterSwap"]({detail:{target:h.root}});
  assert.equal(h.root.checks[0].checked,true);
  assert.equal(h.root.checks[1].checked,false);
  assert.equal(h.root.checks[2].checked,false);
  assert.equal(h.root.dataset.uiSseUrl,"/automatic-encoder/live?release_id=42&preview_page=2");

  const releases = harness("releases");
  const releaseDialog = releases.click();
  const releaseSwap = {detail:{target:releases.root,shouldSwap:true}};
  releases.bodyListeners["htmx:beforeSwap"](releaseSwap);
  assert.equal(releaseSwap.detail.shouldSwap,false,"Release SSE must also preserve the dialog's assignment");
  releases.resolve(null);
  await releaseDialog;
  releases.document.activeElement = releases.clear;
  await releases.click(releases.clear);
  assert.deepEqual(JSON.parse(releases.requests[0].body),{video_id:null,audio_id:null,audio_downmix:"none",revision:3});
  assert.match(releases.summary.textContent,/Автоматический · настройки Kensetsu по умолчанию/);
  assert.equal(releases.clear.hidden,true);
  assert.equal(releases.document.activeElement,releases.edit,"Reset keeps keyboard focus on a visible control");
  assert.match(releases.status.textContent,/сохранены/);
  assert.equal(releases.dialogs.length,1,"Reset does not require a preset dialog");

  const noStatus = harness("releases",false);
  await noStatus.click(noStatus.clear);
  assert.equal(JSON.parse(noStatus.row.dataset.policy).revision,4,"A missing optional status element cannot prevent saving");
  process.stdout.write("Automatic encoder UI: release controls, default reset, confirmed revisions, SSE and exact selection passed.\n");
})().catch(error => {console.error(error);process.exitCode=1;});
