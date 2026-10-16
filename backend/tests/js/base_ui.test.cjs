/* Execute the real shared client: dialog reuse, delayed SSE and keyboard state. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const html = fs.readFileSync(path.join(__dirname, "../../app/templates/base.html"), "utf8");
const script = html.match(/  <script>\n([\s\S]*?)  <\/script>/)[1];
const flush = () => new Promise(resolve => setImmediate(resolve));

function input(name, form, type, value, checked=false, disabled=false) {
  const attrs = {name, form, type};
  return {name, value, checked, disabled, tagName:"INPUT", textContent:"", selectionStart:null,
    getAttribute:key => attrs[key] ?? null,
    hasAttribute:key => key in attrs,
    closest:() => null,
    focus(options) { this.focused = options; },
  };
}

function harness() {
  const listeners = {}, bodyListeners = {}, timers = [], requests = [], scroll = [];
  const status = {textContent:"", hidden:true}, actionError = {textContent:"", hidden:true};
  let options = [], expanded = false;
  let scroller = {scrollLeft:0,getAttribute:() => "Торренты релиза 2"};
  const details = {getAttribute:() => "details-a", open:false, dataset:{}, querySelector:() => null};
  const attrs = {"data-ui-sse-channel":"jobs", "data-ui-sse-url":"/jobs/catalog/live", "data-ui-sse-target":"#live"};
  const root = {id:"live", getAttribute:key => attrs[key] ?? null,
    contains:el => options.includes(el),
    querySelectorAll(selector) {
      if (selector === "input[name][form]:not([type=hidden])" || selector.startsWith("input[")) return options;
      if (selector === "details[open][data-sse-key]") return expanded ? [details] : [];
      if (selector === ".table-scroll[aria-label]") return [scroller];
      return [];
    },
    querySelector(selector) {
      if (selector.startsWith("details[")) return details;
      if (selector.startsWith(".table-scroll[")) return scroller;
      return options.find(el => selector.includes(`name="${el.name}"`)) || null;
    },
  };
  const document = {
    activeElement:null, visibilityState:"visible",
    addEventListener:(name,fn) => { listeners[name] = fn; },
    getElementById:id => ({"ui-live-status":status,"ui-action-error":actionError})[id] || null,
    querySelector:() => null,
    querySelectorAll:selector => selector.startsWith("[data-ui-sse-channel") ? [root] : [],
    body:{classList:{add(){},remove(){}},addEventListener:(name,fn) => { (bodyListeners[name] ||= []).push(fn); }},
  };
  const window = {scrollY:100,scrollTo:(...args) => scroll.push(args)};
  const htmx = {ajax:(method,url,settings) => new Promise((resolve,reject) => requests.push({method,url,settings,resolve,reject}))};
  window.htmx = htmx;
  class EventSource {
    constructor(url) { this.url = url; this.readyState = 1; this.listeners = {}; events.push(this); }
    addEventListener(name, fn) { this.listeners[name] = fn; }
    close() {}
  }
  const events = [];
  const context = vm.createContext({document,window,htmx,EventSource,console,URL,URLSearchParams,
    setTimeout:(fn,delay) => { timers.push({fn,delay}); },
    navigator:{}, fetch:async () => { throw new Error("unexpected network"); },
  });
  vm.runInContext(script,context);
  const emit = (name,event) => (bodyListeners[name] || []).forEach(fn => fn(event));
  return {context,document,window,root,status,actionError,timers,requests,events,scroll,emit,
    setOptions(next) { options = next; },
    table:() => scroller,
    setExpanded(value) { expanded = value; }, details,
    refresh() { context.refreshUiSseTargets("jobs"); },
    swap(next) {
      emit("htmx:beforeSwap",{detail:{target:root,shouldSwap:true}});
      options = next;
      scroller = {scrollLeft:0,getAttribute:() => "Торренты релиза 2"};
      details.open = false;
      emit("htmx:afterSwap",{detail:{target:root}});
    },
  };
}

(async () => {
  // An Escape cancellation on the second use must never reuse an earlier "ok".
  const dialogUi = harness();
  const select = () => ({innerHTML:"",value:"",options:[],selectedOptions:[],
    appendChild(option) { this.options.push(option); this.selectedOptions = [option]; },
    addEventListener(){},removeEventListener(){},
  });
  const video = select(), audio = select(), downmix = select();
  downmix.options = [{value:"none"},{value:"stereo"}];
  const dialog = {returnValue:"ok",listeners:{},
    querySelector:selector => ({"#encoder-video-select":video,"#encoder-audio-select":audio,"#encoder-downmix-select":downmix})[selector],
    showModal() { this.openedWith = this.returnValue; },
    addEventListener(name,fn) { this.listeners[name] = fn; },
    removeEventListener(name) { delete this.listeners[name]; },
  };
  dialogUi.context.ensureEncoderPresetDialog = () => dialog;
  dialogUi.document.createElement = () => ({dataset:{}});
  dialogUi.context.fetch = async () => ({ok:true,json:async()=>({videos:[{id:"hevc"}],audios:[{id:"opus"}]})});
  const picking = dialogUi.context.pickEncoderPreset();
  await flush();
  assert.equal(dialog.openedWith, "");
  dialog.listeners.close(); // Escape closes without changing returnValue.
  assert.equal(await picking, null);

  const ui = harness();
  const oldOption = input("workers","job-run-mediainfo_sync","number","4");
  ui.setOptions([oldOption]); ui.document.activeElement = oldOption;
  ui.refresh(); await flush();
  assert.equal(ui.requests.length,1);
  assert.equal(ui.timers.length,0); // A slow response cannot lose its in-flight lock.
  ui.refresh(); await flush(); assert.equal(ui.requests.length,1);
  ui.window.scrollY = 480;
  oldOption.value = "7";
  ui.table().scrollLeft = 240;
  ui.setExpanded(true);
  const replacement = input("workers","job-run-mediainfo_sync","number","4");
  ui.swap([replacement]);
  assert.equal(replacement.value,"7");
  assert.equal(replacement.focused.preventScroll,true);
  assert.equal(ui.details.open,true);
  assert.equal(ui.table().scrollLeft,240);
  assert.deepEqual(ui.scroll.at(-1),[0,480]);
  ui.requests[0].resolve(); await flush();
  assert.equal(ui.timers[0].delay,3000);
  ui.timers.shift().fn(); await flush();
  assert.equal(ui.requests.length,2); // The event arriving during the request is caught up.

  // Disabled server controls win over a stale local option.
  ui.requests[1].resolve(); await flush(); ui.timers.shift().fn();
  ui.document.activeElement = replacement;
  replacement.value = "8";
  ui.refresh(); await flush();
  const disabled = input("workers","job-run-mediainfo_sync","number","4",false,true);
  ui.swap([disabled]);
  assert.equal(disabled.value,"4");
  assert.equal(disabled.focused,undefined);
  ui.requests[2].resolve(); await flush(); ui.timers.shift().fn();
  ui.refresh(); await flush();
  ui.emit("htmx:responseError",{detail:{target:ui.root,xhr:{status:500}}});
  ui.requests[3].resolve(); await flush(); // HTMX 1.x resolves its Promise on HTTP 500.
  assert.match(ui.status.textContent,/Данные не обновились/);
  ui.timers.shift().fn(); await flush(); assert.equal(ui.requests.length,5);
  ui.requests[4].reject(new Error("offline")); await flush();
  ui.timers.shift().fn(); await flush(); assert.equal(ui.requests.length,6);

  const feedback = harness();
  feedback.context.initUiSse();
  feedback.events[0].onopen(); assert.match(feedback.status.textContent,/подключено/);
  feedback.events[0].onerror(); assert.match(feedback.status.textContent,/Нет связи/);
  feedback.emit("htmx:responseError",{detail:{xhr:{status:422}}});
  assert.equal(feedback.actionError.hidden,false);
  assert.match(feedback.actionError.textContent,/HTTP 422/);
  feedback.emit("htmx:beforeRequest",{detail:{requestConfig:{verb:"post"}}});
  assert.equal(feedback.actionError.hidden,true);
  feedback.events[0].onopen(); await flush();
  assert.equal(feedback.requests.length,1); // Reconnect repairs stale data.
  console.log("Shared UI: dialog cancellation, slow SSE, latest scroll/options/focus, retry and feedback passed");
})().catch(error => { console.error(error); process.exitCode=1; });
