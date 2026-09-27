// Exercises the real markdownText()/humanPost() functions from observatory.js
// with the minimal DOM stub they need (only esc() is used, which is pure -
// no click/render machinery involved, so this stub is much smaller than
// board_navigation_harness.js).
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const vm = require('vm');

function makeElement() {
  return { value: '', hidden: false, innerHTML: '', textContent: '', dataset: {},
           addEventListener() {}, setAttribute() {}, classList: { toggle() {}, add() {}, remove() {} },
           closest() { return null; }, append() {}, querySelector() { return null; }, querySelectorAll() { return []; } };
}
const document = {
  getElementById: () => makeElement(),
  querySelector: () => null, querySelectorAll: () => [], addEventListener: () => {},
  createElement: () => makeElement(),
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

const sample = process.argv[3];
const html = vm.runInContext('humanPost', ctx)(sample);
console.log(html);
