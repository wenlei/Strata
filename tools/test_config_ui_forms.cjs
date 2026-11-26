const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const html = fs.readFileSync(path.join(__dirname, "config_ui.html"), "utf8");
const script = (start, end) => html.slice(html.indexOf(start), html.indexOf(end, html.indexOf(start)));

function fixture(extra = {}) {
  const elements = new Map();
  const $ = id => {
    if (!elements.has(id)) elements.set(id, {value: "", scrollIntoView() {}});
    return elements.get(id);
  };
  return vm.createContext({
    $, tr: text => text, esc: text => text, LANG: "en",
    toast(message, failed) { if (failed) throw new Error(message); },
    document: {querySelectorAll: () => []}, ...extra,
  });
}

test("reapplying the memory preview resends the original request ID, row snapshots and guards", async () => {
  const calls = [];
  const env = fixture({
    toast() {}, dreamError: s => s, loadAgentMemory: async () => {},
    api: async (_url, options) => {
      calls.push(JSON.parse(options.body));
      if (calls.length === 1) throw Error("response lost");
      return {created: 0, updated: 0, deleted: 1};
    },
  });
  env.dreamPreview = {request_id: "preview-id",
    changes: [{op: "delete", id: "b", expected: {value: "original"}, before: "display"}],
    guards: [{id: "a", expected: {value: "survivor"}}]};
  vm.runInContext(script("async function applyDreaming(", "async function loadAgentMemory("), env);
  await vm.runInContext("applyDreaming()", env);
  assert.ok(env.dreamPreview);
  await vm.runInContext("applyDreaming()", env);
  assert.equal(env.dreamPreview, null);
  assert.equal(calls.length, 2);
  assert.deepEqual(calls[0], calls[1]);
  assert.equal(calls[0].request_id, "preview-id");
  assert.deepEqual(calls[0].changes, [{op: "delete", id: "b", expected: {value: "original"}}]);
  assert.deepEqual(calls[0].guards, [{id: "a", expected: {value: "survivor"}}]);
});
