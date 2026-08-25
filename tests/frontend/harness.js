// Evaluate the UI's inline <script> under a permissive DOM shim, then answer
// questions about its pure logic. Keeps front-end behaviour testable from the
// normal pytest run without adding an npm toolchain.
const fs = require('fs');
const vm = require('node:vm');

const html = fs.readFileSync(process.argv[2], 'utf8');
const js = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]).join('\n');

function el() {
  const e = {
    style: {}, dataset: {}, children: [], value: '', checked: false, disabled: false,
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    appendChild(c) { this.children.push(c); return c; },
    prepend() {}, remove() {}, insertBefore() {}, setAttribute() {}, removeAttribute() {},
    getAttribute() { return null; }, addEventListener() {}, removeEventListener() {},
    scrollIntoView() {}, focus() {}, click() {}, closest() { return null; },
    querySelector() { return null; }, querySelectorAll() { return []; },
    getBoundingClientRect() { return { top: 0, left: 0, width: 0, height: 0 }; },
    get textContent() { return this._t || ''; }, set textContent(v) { this._t = v; },
    get innerHTML() { return this._h || ''; }, set innerHTML(v) { this._h = v; },
  };
  return e;
}

const sandbox = {
  console,
  document: {
    getElementById: () => el(), querySelector: () => null, querySelectorAll: () => [],
    createElement: () => el(), addEventListener() {}, body: el(), documentElement: el(),
  },
  window: { scrollTo() {}, scrollY: 0, innerWidth: 1440, innerHeight: 900,
            addEventListener() {}, location: { reload() {} }, matchMedia: () => ({ matches: false }) },
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  EventSource: class { constructor() {} },
  fetch: () => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ jobs: [] }) }),
  setInterval: () => 0, clearInterval() {}, setTimeout: () => 0, clearTimeout() {},
  requestAnimationFrame: (f) => f(), getComputedStyle: () => ({}),
  alert() {}, confirm: () => true, Intl,
};
sandbox.globalThis = sandbox;
const ctx = vm.createContext(sandbox);
vm.runInContext(js, ctx, { filename: 'index.html' });

// The question to answer comes in as JSON on argv[3].
const q = JSON.parse(process.argv[3]);
const out = vm.runInContext(q.expr, ctx, { filename: 'query' });
process.stdout.write(JSON.stringify(out));
