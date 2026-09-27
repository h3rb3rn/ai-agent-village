// Exercises the real renderMemoryRatio()/renderAuditor() functions from
// observatory.js with a minimal DOM stub that records innerHTML per element id
// (only what those two functions touch - no click/render machinery involved).
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const vm = require('vm');

const elements = {};
function makeElement(id) {
  return elements[id] = elements[id] || { id, value: '', innerHTML: '', textContent: '', hidden: false, dataset: {},
           addEventListener() {}, setAttribute() {}, classList: { toggle() {}, add() {}, remove() {} },
           closest() { return null; }, append() {}, querySelector() { return null; }, querySelectorAll() { return []; } };
}
const document = {
  getElementById: (id) => makeElement(id),
  querySelector: () => null, querySelectorAll: () => [], addEventListener: () => {},
  createElement: () => makeElement('_created'),
  documentElement: { dataset: {}, removeAttribute() {}, setAttribute() {}, style: {} },
  body: { classList: { toggle() {}, add() {}, remove() {} } },
};
const location = { pathname: '/dashboard', hash: '' };
const window = { scrollTo: () => {}, addEventListener: () => {}, matchMedia: () => ({ matches: false, addEventListener: () => {} }) };
const localStorage = { store: {}, getItem(k) { return this.store[k] ?? null; }, setItem(k, v) { this.store[k] = v; } };

const ctx = vm.createContext({ document, location, window, localStorage, console,
  history: {}, setInterval: () => {}, clearInterval: () => {}, setTimeout: () => {}, clearTimeout: () => {},
  fetch: () => Promise.resolve({ ok: false }),
  MutationObserver: class { observe() {} disconnect() {} },
  AbortSignal: { timeout: () => ({}) } });
vm.runInContext(src, ctx, { filename: 'observatory.js' });

const payload = JSON.parse(process.argv[3]);
vm.runInContext(`data = ${JSON.stringify({ current: { auditor: payload.auditor || {} } })};`, ctx);
vm.runInContext('renderMemoryRatio', ctx)(payload.stats || {});
vm.runInContext('renderAuditor', ctx)();

console.log(JSON.stringify({
  memoryRatio: elements['memory-ratio'] ? elements['memory-ratio'].innerHTML : null,
  auditorSummary: elements['auditor-summary'] ? elements['auditor-summary'].innerHTML : null,
  auditorCategories: elements['auditor-categories'] ? elements['auditor-categories'].innerHTML : null,
}));
