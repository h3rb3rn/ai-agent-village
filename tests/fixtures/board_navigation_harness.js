// Minimal DOM-free harness to execute the real observatory.js and drive the
// board thread click handler exactly as a browser would, without jsdom.
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');

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

const document = {
  getElementById: get,
  querySelector: () => null,
  querySelectorAll: () => [],
  addEventListener: () => {},
  createElement: () => makeElement('created'),
};
const location = { pathname: '/dashboard', hash: '' };
const history = {};
const window = { scrollTo: () => {}, addEventListener: () => {}, matchMedia: () => ({ matches: false, addEventListener: () => {} }) };
const localStorage = { store: {}, getItem(k) { return this.store[k] ?? null; }, setItem(k, v) { this.store[k] = v; } };
document.documentElement = { dataset: {}, removeAttribute() {}, setAttribute() {}, style: {} };
document.body = { classList: { toggle() {}, add() {}, remove() {} } };

const vm = require('vm');
const ctx = vm.createContext({ document, location, history, window, localStorage, console,
  setInterval: () => {}, clearInterval: () => {}, setTimeout: () => {}, clearTimeout: () => {},
  fetch: () => Promise.resolve({ ok: false }),
  MutationObserver: class { observe() {} disconnect() {} },
  AbortSignal: { timeout: () => ({}) } });
vm.runInContext(src, ctx, { filename: 'observatory.js' });

// Synthetic board_message events: three topics with a title order (by recency)
// that differs from insertion/event order, which is exactly the condition that
// exposed the bug (position-in-sorted-list != position-in-insertion-order-map).
vm.runInContext(`
  data = { events: [
    { event: 'board_message', agent: '01-king',      timestamp: '2026-01-01T10:00:00Z', detail: 'message=Alpha topic first line.' },
    { event: 'board_message', agent: '02-explorer',  timestamp: '2026-01-01T11:00:00Z', detail: 'message=Beta topic first line.' },
    { event: 'board_message', agent: '03-librarian', timestamp: '2026-01-01T12:00:00Z', detail: 'message=Gamma topic first line.' },
  ], current: { timestamp: '2026-01-01T12:00:01Z' } };
  renderBoard();
`, ctx);

const threadsEl = get('board-threads');
const clickHandler = threadsEl.listeners['click'];
if (!clickHandler) { console.log(JSON.stringify({ error: 'no click handler registered' })); process.exit(1); }

// renderBoard() sorts threads most-recent-first, so the FIRST rendered row is
// "Gamma" even though it was the THIRD (last) event inserted - the exact
// mismatch between render order and insertion order that exposed the bug.
// Click that first rendered row and check the right thread gets selected.
const rows = threadsEl.innerHTML.match(/data-thread="([^"]*)"/g).map(m => m.match(/"([^"]*)"/)[1]);
const firstRowKey = rows[0];
clickHandler({ target: { closest: () => ({ dataset: { thread: firstRowKey } }) } });

const result = vm.runInContext('boardThread && boardThread.title', ctx);
console.log(JSON.stringify({ clickedKey: firstRowKey, selectedTitle: result, renderedOrderKeys: rows }));
