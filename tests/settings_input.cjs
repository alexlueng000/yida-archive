// 在隔离 DOM 中执行实际页面脚本，模拟上传后的状态刷新与异步旧响应。
const fs = require('fs');
const vm = require('vm');
const assert = require('assert/strict');
const page = fs.readFileSync(process.argv[2], 'utf8');
const source = [...page.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)].map(x => x[1]).join('\n');
const nodes = {};
function node(id) {
  return nodes[id] ||= {value: '', innerHTML: '', textContent: '', disabled: false, style: {},
    classList: {add() {}, remove() {}}, listeners: {},
    addEventListener(name, callback) { this.listeners[name] = callback; },
    querySelectorAll() { return []; }};
}
const server = {project_no: 'OLD', purchaser: '旧单位', unmatched_policy: 'skip', batches: [], result: null};
let lastBody;
let release;
const context = vm.createContext({
  document: {getElementById: id => id === 'split' ? null : node(id), addEventListener() {}},
  window: {}, console, setTimeout: () => 1, clearTimeout() {},
  fetch: async (url, options) => {
    if (url === '/api/state') return {json: async () => server};
    lastBody = JSON.parse(options.body);
    if (url === '/api/analyze') return new Promise(resolve => {
      release = () => resolve({ok: true, json: async () => ({ok: true})});
    });
    return {ok: true, json: async () => ({ok: true})};
  }
});
(async () => {
  vm.runInContext(source, context);
  await context.refresh();
  assert.equal(node('project_no').value, 'OLD');
  node('project_no').value = 'NEW-PROJECT';
  node('project_no').listeners.input();
  node('purchaser').value = '新单位';
  node('purchaser').listeners.input();
  await context.refresh(); // 上传完成、添加批次等操作都走这个刷新
  assert.equal(node('project_no').value, 'NEW-PROJECT');
  assert.equal(node('purchaser').value, '新单位');
  assert.equal(context.state.result, null);
  assert.equal(node('saveBtn').disabled, true);
  assert.equal(context.currentSettings().project_no, 'NEW-PROJECT');
  const pending = context.api('/api/analyze', context.currentSettings());
  assert.equal(lastBody.project_no, 'NEW-PROJECT');
  node('project_no').value = 'NEXT';
  node('project_no').listeners.input();
  release();
  await assert.rejects(pending, /设置已变更/);
  node('project_no').value = '';
  node('project_no').listeners.input();
  await context.refresh();
  assert.equal(node('project_no').value, '');
  await assert.rejects(context.api('/api/save', context.currentSettings()), /请输入项目号/);
  console.log('项目号编辑、上传后保留、旧响应隔离、空值保护：通过');
})().catch(error => { console.error(error); process.exitCode = 1; });
