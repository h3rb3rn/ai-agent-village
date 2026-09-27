// Renders board-info for a synthetic event list, reusing the same minimal DOM
// stub as board_navigation_harness.js.
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync(process.argv[2], 'utf8');
const events = JSON.parse(process.argv[3]);

function makeElement(id) {
  return { id, value: '', hidden: false, innerHTML: '', textContent: '',
           dataset: {}, listeners: {},
           addEventListener(type, fn) { this.listeners[type] = fn; },
           setAttribute() {}, classList: { toggle() {}, add() {}, remove() {} },
           closest() { return null; }, disabled: false,
           append() {}, querySelector() { return null; }, querySelectorAll() { return []; } };
}
const registry = new Map();
function get(id) { if (!registry.has(id)) registry.set(id, makeElement(id)); return registry.get(id); }
const document = { getElementById: get, querySelector: () => null, querySelectorAll: () => [], addEventListener: () => {}, createElement: () => makeElement('created') };
document.documentElement = { dataset: {}, removeAttribute() {}, setAttribute() {}, style: {} };
document.body = { classList: { toggle() {}, add() {}, remove() {} } };
const location = { pathname: '/dashboard', hash: '' };
const window = { scrollTo: () => {}, addEventListener: () => {}, matchMedia: () => ({ matches: false, addEventListener: () => {} }) };
const localStorage = { store: {}, getItem(k) { return this.store[k] ?? null; }, setItem(k, v) { this.store[k] = v; } };

const ctx = vm.createContext({ document, location, history: {}, window, localStorage, console,
  setInterval: () => {}, clearInterval: () => {}, setTimeout: () => {}, clearTimeout: () => {},
  fetch: () => Promise.resolve({ ok: false }),
  MutationObserver: class { observe() {} disconnect() {} },
  AbortSignal: { timeout: () => ({}) } });
vm.runInContext(src, ctx, { filename: 'observatory.js' });
ctx.data = undefined;
vm.runInContext('data = ' + JSON.stringify({ events, current: { timestamp: '2026-01-01T10:07:00Z' } }) + '; renderBoard();', ctx);
console.log(get('board-info').textContent);
