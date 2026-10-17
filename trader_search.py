"""Search Trader's figure form, archive detail/price images, OCR, and record each product."""
import argparse
import base64
import datetime as dt
import http.cookiejar
import io
import json
from pathlib import Path
import re
import time
import urllib.parse
import urllib.request
from bs4 import BeautifulSoup
from PIL import Image
from price_ocr import normalize, read_price

URL = 'https://www.e-trader.jp/kensaku_figure/'
CATEGORIES = {'prize': '1fgp', 'figure': '1fg', 'gunpla': '1pmg', 'plastic': '1pme'}

def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')

def form_payload(form):
    data = {x['name']: x.get('value', '') for x in form.select('input[name]')
            if x.get('type', '').lower() not in ('radio', 'checkbox') or x.has_attr('checked')}
    button = form.select_one('button[name]')
    if button:
        data[button['name']] = button.get('value', button.get_text(strip=True))
    return data

def parse_results(html):
    soup = BeautifulSoup(html, 'html.parser')
    found = []
    for form in soup.select('form:has(input[name="frm_master"])'):
        data = form_payload(form)
        master = data['frm_master']
        if not re.fullmatch(r'\d+', master):
            raise ValueError('Unexpected product ID')
        row = form.find_parent('tr')
        cells = row.find_all('td', recursive=False) if row else []
        if len(cells) < 4:
            raise ValueError('Search result row structure changed')
        found.append({'master_id': master, 'name': cells[2].get_text(' ', strip=True), 'payload': data})
    pages = []
    for form in soup.select('form[id^="fpagina"]'):
        button = form.select_one('button')
        if button and button.get_text(strip=True).isdigit():
            pages.append((int(button.get_text(strip=True)), form_payload(form)))
    match = re.search(r'検索に一致した商品は.*?([0-9,]+).*?点', soup.get_text(' ', strip=True))
    if not match:
        raise ValueError('Search result count was not found; site layout or response changed')
    return found, pages, int(match.group(1).replace(',', ''))

def parse_detail(html, candidate):
    soup = BeautifulSoup(html, 'html.parser')
    names = soup.select('#post-231 p.has-huge-font-size')
    if len(names) != 1:
        raise ValueError('Expected one product title')
    name = names[0].get_text(' ', strip=True)
    if normalize(name) != normalize(candidate['name']):
        raise ValueError('Search result and detail product names differ')
    container = names[0].find_parent(class_='vk_outer_container')
    if container is None:
        raise ValueError('Product detail container was not found')
    fields = {}
    for label in container.find_all('p'):
        key = label.get_text(strip=True)
        if key in ('型番', 'メーカー'):
            value = label.parent.find_next_sibling('div')
            if value:
                fields[key] = value.get_text(' ', strip=True)
    if not fields.get('型番') or not fields.get('メーカー'):
        raise ValueError('Product model/manufacturer missing')
    images = container.select('img[src]')
    if len(images) != 1:
        raise ValueError('Expected one price image in product detail')
    return {'master_id': candidate['master_id'], 'model': fields['型番'],
            'manufacturer': fields['メーカー'], 'name': name}, images[0]['src']

class Client:
    def __init__(self, delay=1.0):
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.delay = max(1.0, delay)
        self.last = 0

    def fetch(self, url=URL, payload=None):
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != 'https' or parts.hostname not in ('www.e-trader.jp', 'e-trader.jp'):
            raise ValueError('Only HTTPS URLs on e-trader.jp are allowed')
        time.sleep(max(0, self.delay - (time.monotonic() - self.last)))
        data = urllib.parse.urlencode(payload).encode('utf-8') if payload is not None else None
        request = urllib.request.Request(url, data=data, headers={
            'User-Agent': 'TraderPriceRecorder/1.0 (personal buyback price check)', 'Referer': URL})
        try:
            with self.opener.open(request, timeout=30) as response:
                if urllib.parse.urlsplit(response.url).hostname not in ('www.e-trader.jp', 'e-trader.jp'):
                    raise ValueError('Unexpected redirect')
                content = response.read(8 * 1024 * 1024 + 1)
                if len(content) > 8 * 1024 * 1024:
                    raise ValueError('Response exceeds 8MB')
                return content
        finally:
            self.last = time.monotonic()

    def image(self, src):
        if src.startswith('data:image/'):
            header, encoded = src.split(',', 1)
            if ';base64' not in header or len(encoded) > 8 * 1024 * 1024:
                raise ValueError('Unsupported inline image')
            raw = base64.b64decode(encoded, validate=True)
        else:
            url = urllib.parse.urljoin(URL, src)
            if url.startswith('http://'):
                url = 'https://' + url[7:]
            raw = self.fetch(url)
        with Image.open(io.BytesIO(raw)) as image:
            image.verify()
        return raw

def target_matches(observed, target):
    return (normalize(observed['model']) == normalize(target.get('model', '')) and
            normalize(observed['name']) == normalize(target.get('name', '')) and
            (not target.get('master_id') or observed['master_id'] == target['master_id']))

def run(args):
    started = dt.datetime.now(dt.timezone(dt.timedelta(hours=9)))
    folder = args.output_dir / started.strftime('%Y%m%d-%H%M%S-%f')
    folder.mkdir(parents=True)
    client = Client()
    first = client.fetch().decode('utf-8-sig')
    initial = BeautifulSoup(first, 'html.parser')
    search_form = initial.select_one('form#fkensaku')
    radio = initial.find('input', id=CATEGORIES[args.category])
    if search_form is None or radio is None:
        raise ValueError('Search form was not found')
    payload = form_payload(search_form)
    payload.update(frm_keyw=args.keyword, frm_kisword=radio['value'])
    queue, visited, candidates = [payload], set(), {}
    total = None
    while queue:
        data = queue.pop(0)
        key = json.dumps(data, sort_keys=True, ensure_ascii=False)
        if key in visited:
            continue
        if len(visited) >= args.max_pages:
            raise ValueError('Page limit reached; narrow the keyword or increase --max-pages')
        visited.add(key)
        html = client.fetch(payload=data).decode('utf-8-sig')
        (folder / f'search-{len(visited)}.html').write_text(html, encoding='utf-8')
        items, pages, count = parse_results(html)
        total = count if total is None else total
        if count != total:
            raise ValueError('Search count changed during pagination; rerun')
        for item in items:
            candidates[item['master_id']] = item
        if len(candidates) == total:
            break
        for page, page_data in pages:
            if json.dumps(page_data, sort_keys=True, ensure_ascii=False) not in visited:
                queue.append(page_data)
    if len(candidates) != total:
        raise ValueError(f'Incomplete results: {len(candidates)} of {total}')
    if total > args.max_items:
        raise ValueError(f'{total} products exceed --max-items {args.max_items}; narrow the keyword')
    dump(folder / 'candidates.json', list(candidates.values()))
    targets = json.loads(args.targets.read_text(encoding='utf-8-sig')) if args.targets else None
    if targets is not None and (not isinstance(targets, list) or any(not t.get('model') or not t.get('name') for t in targets)):
        raise ValueError('Targets must be an array of objects with model and name')
    records = []
    for index, candidate in enumerate(candidates.values(), 1):
        record = {'checked_at': started.isoformat(timespec='seconds'), 'source_url': URL,
                  'master_id': candidate['master_id'], 'search_name': candidate['name'],
                  'price_kind': 'reference_buyback', 'status': 'needs_review', 'price_yen': None}
        try:
            html = client.fetch(payload=candidate['payload']).decode('utf-8-sig')
            stem = candidate['master_id']
            detail_path = folder / f'{stem}.html'
            detail_path.write_text(html, encoding='utf-8')
            observed, src = parse_detail(html, candidate)
            record['observed'] = observed
            record['detail_file'] = str(detail_path.resolve())
            selected = [t for t in (targets or []) if target_matches(observed, t)]
            if targets is not None and not selected:
                record['status'] = 'not_target'
            else:
                raw = client.image(src)
                image_path = folder / f'{stem}.png'
                # Store a real PNG regardless of the data URI's sometimes incorrect MIME type.
                with Image.open(io.BytesIO(raw)) as image:
                    image.convert('RGB').save(image_path)
                record['image'] = str(image_path.resolve())
                record['image_source'] = src if not src.startswith('data:') else 'inline data image in archived detail'
                record.update(read_price(image_path))
                if record['ocr_agreement']:
                    record['status'] = 'matched' if targets is not None else 'catalogued'
                    record['price_yen'] = record['candidate_price_yen']
                if targets is not None:
                    record['expected'] = selected
                    record['identity_match'] = True
        except Exception as exc:
            record['error'] = f'{type(exc).__name__}: {exc}'
        records.append(record)
        dump(folder / 'records.json', records)
        print(f"[{index}/{total}] {record['master_id']} {record['status']} {record['price_yen']}", flush=True)
    target_results = []
    for target in targets or []:
        matches = [r for r in records if r.get('observed') and target_matches(r['observed'], target)]
        status = matches[0]['status'] if len(matches) == 1 else ('not_found' if not matches else 'ambiguous')
        target_results.append({'expected': target, 'status': status, 'match_count': len(matches)})
        if len(matches) > 1:
            for record in matches:
                record.update(status='needs_review', price_yen=None, error='Multiple products match the same target')
    summary = {'keyword': args.keyword, 'checked_at': started.isoformat(timespec='seconds'),
               'total': total, 'records': len(records),
               'accepted': sum(r['status'] in ('matched', 'catalogued') for r in records),
               'needs_review': sum(r['status'] == 'needs_review' for r in records),
               'targets': target_results}
    dump(folder / 'records.json', records)
    dump(folder / 'summary.json', summary)
    dump(folder / 'watchlist-candidates.json', [r['observed'] for r in records if r.get('observed')])
    lines = [f'# 買取価格記録：{args.keyword}', '', f"確認日時：{summary['checked_at']}", '',
             '|商品名|型番|参考買取価格（円）|状態|', '|---|---|---:|---|']
    for record in records:
        observed = record.get('observed', {})
        name = observed.get('name', record['search_name']).replace('|', '／')
        lines.append(f"|{name}|{observed.get('model', '')}|{record['price_yen'] if record['price_yen'] is not None else '要確認'}|{record['status']}|")
    (folder / 'report.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    with (args.output_dir / 'history.jsonl').open('a', encoding='utf-8') as history:
        for record in records:
            history.write(json.dumps(record, ensure_ascii=False) + '\n')
    print('Result: ' + str(folder.resolve()))
    return 2 if summary['needs_review'] or any(t['status'] != 'matched' for t in target_results) else 0

def main():
    parser = argparse.ArgumentParser(description='トレーダー検索→型番取得→価格画像保存→OCR→記録')
    parser.add_argument('keyword')
    parser.add_argument('--category', choices=CATEGORIES, default='prize')
    parser.add_argument('--targets', type=Path, help='追跡する型番・正式名のJSON配列')
    parser.add_argument('--output-dir', type=Path, default=Path(__file__).resolve().parent / 'runs')
    parser.add_argument('--max-items', type=int, default=100)
    parser.add_argument('--max-pages', type=int, default=30)
    args = parser.parse_args()
    if not args.keyword.strip() or args.max_items < 1 or args.max_pages < 1:
        parser.error('Specify a nonempty keyword and positive limits')
    try:
        return run(args)
    except Exception as exc:
        parser.exit(1, f'{type(exc).__name__}: {exc}\n')

if __name__ == '__main__':
    raise SystemExit(main())
