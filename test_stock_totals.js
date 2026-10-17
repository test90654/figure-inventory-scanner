const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(path.join(__dirname, 'inventory_browser_scan.py'), 'utf8');
const functions = source.slice(source.indexOf('function stockPriceTotals('), source.indexOf('window.refreshLashinbang ='));
const ctx = vm.createContext({escapeHtml: s => String(s)});
vm.runInContext(functions, ctx);
const quote = (price_yen, extra = {}) => ({status: 'ok', price_yen, ...extra});
const items = [
  {jan: '1', location: 'A', count: 3, trader_quote: quote(100), lashinbang_quote: quote(600)},
  {jan: '1', location: 'B', count: 2, trader_quote: quote(100)},
  {count: 2},
  {count: 1, trader_quote: {status: 'multiple', price_min: 100, price_max: 200}},
  {count: 2, trader_quote: quote(0)},
  {count: 1, trader_quote: quote(50, {stale: true, refresh_error: true})}
];
const plain = value => JSON.parse(JSON.stringify(value));
assert.deepEqual(plain(ctx.stockPriceTotals(items, 'trader_quote')), {amount: 550, counted: 8, excluded: 3, stale: 1});
assert.deepEqual(plain(ctx.stockPriceTotals(items, 'lashinbang_quote')), {amount: 1800, counted: 3, excluded: 8, stale: 0});
assert.equal(ctx.stockPriceTotals(items.slice(0, 1), 'trader_quote').amount, 300);
assert.match(ctx.stockTotalsHtml([{count: 1}], '在庫全体'), /未集計/);
assert.match(ctx.stockTotalsHtml([{count: 2, trader_quote: quote(0)}], '在庫全体'), /¥0/);
assert.match(ctx.stockPriceHtml(quote(600), 'らしんばん', 2), /2点 小計 ¥1,200/);
console.log('Stock totals: quantity, locations, independent stores, filtered subset, missing/ambiguous/zero/stale prices, row subtotal: PASS');
