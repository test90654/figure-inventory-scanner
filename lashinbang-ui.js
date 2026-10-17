/* Confirm name-search candidates before linking them to an inventory code. */
(() => {
  'use strict';
  const overlay = document.createElement('div');
  overlay.style.cssText = 'display:none;position:fixed;inset:0;z-index:3000;background:#000b;align-items:center;justify-content:center;padding:12px';
  const dialog = document.createElement('section');
  dialog.setAttribute('role', 'dialog');
  dialog.setAttribute('aria-modal', 'true');
  dialog.setAttribute('aria-label', 'らしんばんの商品名検索');
  dialog.style.cssText = 'box-sizing:border-box;width:100%;max-width:560px;max-height:85vh;overflow-y:auto;background:#202c38;color:#eee;padding:16px;border-radius:10px;text-align:left';
  overlay.appendChild(dialog);
  document.body.appendChild(overlay);
  let generation = 0, previousFocus;
  const el = (tag, text, parent = dialog) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    parent.appendChild(node);
    return node;
  };
  const button = (text, action, parent = dialog) => {
    const node = el('button', text, parent);
    node.type = 'button';
    node.style.cssText = 'padding:7px 10px;margin:5px 5px 5px 0;background:#284b6c;color:white;border:1px solid #78b9ff;border-radius:5px;cursor:pointer';
    node.addEventListener('click', action);
    return node;
  };
  function close() {
    generation++;
    overlay.style.display = 'none';
    previousFocus?.focus();
  }
  overlay.addEventListener('click', event => { if (event.target === overlay) close(); });
  dialog.addEventListener('keydown', event => {
    if (event.key === 'Escape') close();
    if (event.key === 'Tab') {
      const nodes = [...dialog.querySelectorAll('button:not(:disabled),input,a[href]')];
      if (event.shiftKey && document.activeElement === nodes[0]) { event.preventDefault(); nodes.at(-1)?.focus(); }
      else if (!event.shiftKey && document.activeElement === nodes.at(-1)) { event.preventDefault(); nodes[0]?.focus(); }
    }
  });
  async function api(path, body) {
    const response = await fetch('/api/lashinbang/' + path, {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
    });
    const result = await response.json();
    if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : '処理に失敗しました');
    return result;
  }
  window.openLashinbangForJan = (jan, title) => {
    previousFocus = document.activeElement;
    let own = ++generation;
    overlay.style.display = 'flex';
    dialog.replaceChildren();
    const header = el('div');
    header.style.cssText = 'display:flex;align-items:center;justify-content:space-between;gap:8px';
    el('strong', 'らしんばん 商品名検索', header);
    button('閉じる', close, header);
    el('p', title).style.cssText = 'font-size:12px;overflow-wrap:anywhere';
    el('small', 'キャラクター・衣装・限定版を確認して選んでください。なっとく買取の参考価格です。');
    const form = el('form');
    const input = el('input', undefined, form);
    input.setAttribute('aria-label', 'らしんばん検索語');
    input.maxLength = 100;
    input.value = title.slice(0, 100);
    input.style.cssText = 'box-sizing:border-box;width:100%;padding:9px;margin-top:10px;background:white;color:#222';
    const submit = button('検索', () => {}, form);
    submit.type = 'submit';
    const status = el('div');
    status.setAttribute('role', 'status');
    const results = el('div');
    async function search() {
      own = ++generation;
      const requestGeneration = own;
      status.textContent = '候補を検索中…';
      results.replaceChildren();
      try {
        const data = await api('search', {jan, query: input.value});
        if (requestGeneration !== generation) return;
        input.value = data.query;
        status.textContent = data.candidates.length ? '同じ商品を選んで紐づけてください。' : '候補がありません。作品名やキャラクター名など、短い語で再検索してください。';
        let saving = false;
        function show(product) {
          const card = el('article', undefined, results);
          card.style.cssText = 'border-top:1px solid #526373;padding:10px 0;overflow-wrap:anywhere';
          if (product.image_url) {
            const image = el('img', undefined, card);
            image.src = product.image_url;
            image.alt = product.title;
            image.loading = 'lazy';
            image.style.cssText = 'width:64px;height:64px;object-fit:contain;background:white;float:left;margin:0 10px 8px 0';
          }
          el('div', product.title, card).style.fontSize = '13px';
          el('strong', product.price_yen == null ? '価格は要確認' : `¥${Number(product.price_yen).toLocaleString('ja-JP')}`, card);
          el('small', ` ／ 商品ID ${product.master_id}`, card);
          const select = button('この商品に紐づける', async () => {
            if (saving) return;
            saving = true;
            select.disabled = true;
            try {
              await api('select', {jan, token: data.token, master_id: product.master_id});
              if (window.loadStock) window.loadStock();
              if (requestGeneration !== generation) return;
              results.replaceChildren();
              status.textContent = data.storage === 'google_sheets' ? 'Google Sheetsに紐づけを保存しました。次回から選んだ商品の価格を更新します。' : '紐づけをローカルに保存しました。無料Renderでは再起動で消えるためGoogle Sheets保存を有効にしてください。';
              dialog.querySelector('button').focus();
            } catch (error) { if (requestGeneration === generation) { status.textContent = error.message; select.disabled = false; saving = false; } }
          }, card);
          el('div', undefined, card).style.clear = 'both';
        }
        data.candidates.slice(0, 3).forEach(show);
        if (data.candidates.length > 3) {
          const more = button(`残り${data.candidates.length - 3}件を表示`, () => {
            more.remove();
            data.candidates.slice(3).forEach(show);
          }, results);
        }
        if (data.has_more) el('p', '候補が多いため先頭30件を表示しています。検索語を追加して絞り込めます。', results);
      } catch (error) { if (requestGeneration === generation) status.textContent = error.message; }
    }
    form.addEventListener('submit', event => { event.preventDefault(); search(); });
    input.focus();
    search();
  };
})();
