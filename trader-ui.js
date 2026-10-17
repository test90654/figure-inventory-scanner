/* Trader price panel. All remote product text is rendered through textContent. */
(() => {
  'use strict';
  const panel = document.createElement('section');
  panel.id = 'trader-panel';
  panel.style.cssText = 'display:none;max-width:420px;margin:12px auto;padding:14px;background:#202c38;border:1px solid #78b9ff;border-radius:10px;text-align:left;color:#eee';
  document.getElementById('result-banner').insertAdjacentElement('afterend', panel);
  let generation = 0, current = null;
  const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
  const el = (tag, text, parent=panel) => {
    const node=document.createElement(tag);
    if(text !== undefined) node.textContent=text;
    parent.appendChild(node); return node;
  };
  async function api(path, body) {
    const response=await fetch('/api/trader'+path, body===undefined ? {} : {
      method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)
    });
    const result=await response.json();
    if(!response.ok) throw new Error(typeof result.detail==='string'?result.detail:'取得できませんでした');
    return result;
  }
  function button(text, action, parent=panel) {
    const node=el('button', text, parent);node.type='button';
    node.style.cssText='margin:5px 5px 5px 0;padding:7px 10px;border-radius:5px;border:1px solid #78b9ff;background:#284b6c;color:white;cursor:pointer';
    node.addEventListener('click',action);return node;
  }
  function heading(state) {
    panel.replaceChildren();panel.style.display='block';
    el('strong','トレーダー参考買取価格');
    if(state.storage==='google_sheets') el('small',' ／ Google Sheets保存');
    else if(state.storage==='local') el('small',' ／ ローカル保存（無料Renderでは再起動で消えます）');
    el('div',state.title).style.cssText='font-size:12px;margin:8px 0';
    el('small','掲載額は参考価格です。実際の査定額とは異なる場合があります。');
    const form=el('form');form.style.marginTop='10px';
    const input=el('input',undefined,form);input.value=state.suggested_query || '';
    input.setAttribute('aria-label','トレーダー検索語');input.maxLength=100;
    input.style.cssText='box-sizing:border-box;width:100%;padding:9px;background:#fff;color:#222;border-radius:4px';
    const submit=button('候補を検索',()=>{},form);submit.type='submit';
    form.addEventListener('submit',event=>{event.preventDefault();run({query:input.value,search:true});});
    if(state.link) button('紐づけた商品の価格を更新',()=>run({refresh:true}));
    const status=el('div');status.id='trader-progress';status.setAttribute('role','status');
    const results=el('div');results.id='trader-results';
    return results;
  }
  function showProduct(product, parent, selectable) {
    const card=el('article',undefined,parent);
    card.style.cssText='border-top:1px solid #526373;padding:10px 0;margin-top:8px';
    el('div',product.observed?.name || '詳細を取得できない候補',card);
    el('small',`型番: ${product.observed?.model || '未取得'} ／ 商品ID: ${product.master_id}`,card);
    el('div',product.price_yen==null?'価格は要確認':Number(product.price_yen).toLocaleString()+'円',card)
      .style.cssText='font-size:21px;color:#ffcf79;font-weight:bold;margin:7px 0';
    if(product.checked_at) el('small','確認: '+product.checked_at,card);
    if(product.image_url) {
      const image=el('img',undefined,card); image.src=product.image_url;
      image.alt='サイトから取得した参考買取価格の元画像';
      image.style.cssText='display:block;max-width:100%;background:white;margin-top:8px';
    }
    if(product.error) el('div',product.error,card).style.fontSize='12px';
    if(selectable && product.observed?.model) button('この商品に紐づける',async()=>{
      const own=generation,jan=current.jan;
      try {
        const selected=await api('/select',{jan,master_id:product.master_id});
        if(own!==generation)return;
        current=selected;
        const results=heading(selected);
        el('div',selected.storage==='google_sheets'?'Google Sheetsに紐づけを保存しました。次回のスキャンからこの商品を参照します。':'JANとの紐づけを保存しました。次回のスキャンからこの商品を参照します。',results);
        showProduct(selected.product,results,false);
      } catch(error) { if(own===generation) document.getElementById('trader-progress').textContent=error.message; }
    },card);
  }
  async function run(options={}) {
    if(!current)return;
    const own=++generation, state={...current,suggested_query:options.query || current.suggested_query};
    current=state;
    const results=heading(state);
    const status=document.getElementById('trader-progress');
    status.textContent='検索を準備しています…';
    try {
      const {job_id}=await api('/start',{jan:state.jan,...options});
      for(let attempts=0;attempts<400;attempts++) {
        if(own!==generation)return;
        const job=await api('/jobs/'+job_id);
        if(own!==generation)return;
        status.textContent=job.progress;
        if(job.status==='error')throw new Error(job.error);
        if(job.status==='done') {
          results.replaceChildren();
          if(!job.products.length) el('div','候補がありません。検索語を短くして再検索してください。',results);
          else if(!job.linked) el('div','同じ商品か、キャラクター・衣装・PLUS等の版・型番を確認して選んでください。',results);
          job.products.forEach(product=>showProduct(product,results,!job.linked));
          return;
        }
        await wait(1500);
      }
      throw new Error('時間がかかっています。後で再検索してください。');
    } catch(error) { if(own===generation) status.textContent='取得できませんでした: '+error.message; }
  }
  window.openTraderForJan=async(jan)=>{
    const own=++generation;
    panel.style.display='block';panel.replaceChildren();el('div','トレーダーの商品情報を確認中…');
    try {
      const state=await api('/state/'+encodeURIComponent(jan));
      if(own!==generation)return;
      current=state;const results=heading(state);
      if(state.link && !state.link.title_changed && state.product) {
        el('div','保存したJANの紐づけを使用しています。',results);
        showProduct(state.product,results,false);
        if(state.stale) run();
      } else {
        if(state.link?.title_changed) el('div','登録商品名が変わったため、候補を選び直してください。',results);
        else run({search:true});
      }
    } catch(error) { if(own===generation) {panel.replaceChildren();el('div','取得できませんでした: '+error.message);} }
  };
  window.traderOnItem=data=>{ window.openTraderForJan(data.jan); };
})();
