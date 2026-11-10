const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(path.join(__dirname, 'inventory_browser_scan.py'), 'utf8');
const functions = source.slice(source.indexOf('function buildStockText('), source.indexOf('window.shareStockToLine ='));
const ctx = vm.createContext({window: {}, navigator: {}});
vm.runInContext(functions, ctx);

const items = [
  {jan: '4901', title: 'フィギュアA', count: 2, location: '棚1'},
  {jan: '4902', title: 'フィギュアB', count: 1, location: ''},
  {jan: '4903', title: 'フィギュアC', count: 3, location: '棚1'},
];
const text = ctx.buildStockText(items, '【 見出し 】');
assert.equal(text, [
  '【 見出し 】', '', '■ 棚1', '- フィギュアA : 2個 [JAN: 4901]', '- フィギュアC : 3個 [JAN: 4903]',
  '', '■ 場所未設定', '- フィギュアB : 1個 [JAN: 4902]', '', '合計: 3種 / 6点',
].join('\n'));
assert.match(ctx.buildStockText(items), /^【 保管在庫リスト 】/);

const url = ctx.lineShareUrl(text);
assert.ok(url.startsWith('https://line.me/R/share?text='));
assert.equal(decodeURIComponent(url.split('text=')[1]), text);

const many = Array.from({length: 300}, (_, i) => ({jan: String(i), title: 'とても長い商品名のフィギュア', count: 1, location: '棚'}));
assert.equal(ctx.lineShareUrl(ctx.buildStockText(many)), null);
console.log('Stock LINE share: text grouping, share URL round-trip, too-long fallback: PASS');
