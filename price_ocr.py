"""Portable price OCR: Tesseract with conservative preprocessing agreement."""
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from pathlib import Path
from PIL import Image, ImageOps

def normalize(value):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value))).casefold()

def price_crop(image):
    """Find red glyphs; reject large red panels before joining one text line."""
    image = image.convert('RGB')
    mask = Image.new('L', image.size)
    mask.putdata([255 if r > 160 and g < 130 and 30 < b < 150 and r > g * 1.5 and r > b * 1.4 else 0
                  for r, g, b in image.getdata()])
    width, height = mask.size
    pix = mask.load()
    rows = [y for y in range(height)
            if 2 <= sum(pix[x, y] > 0 for x in range(width)) <= width * .4]
    bands = []
    for y in rows:
        if not bands or y - bands[-1][-1] > 3:
            bands.append([y])
        else:
            bands[-1].append(y)
    candidates = []
    for band in bands:
        if band[-1] - band[0] < 8:
            continue
        box = mask.crop((0, band[0], width, band[-1] + 1)).getbbox()
        if box is None:
            continue
        x0, _, x1, _ = box
        h = band[-1] - band[0] + 1
        w = x1 - x0
        if 1.5 <= w / h <= 18:
            candidates.append((x0, band[0], x1, band[-1] + 1))
    if len(candidates) != 1:
        raise ValueError(f'Red price region is ambiguous ({len(candidates)} regions); use a cropped price image.')
    x0, y0, x1, y1 = candidates[0]
    box = (max(0, x0-8), max(0, y0-8), min(width, x1+8), min(height, y1+8))
    crop = image.crop(box)
    mono = ImageOps.invert(mask.crop(box)).convert('RGB')
    return crop, mono, box

def read_price(image_path):
    executable = shutil.which('tesseract') or os.environ.get('TESSERACT_CMD')
    if not executable:
        fallback=Path(os.environ.get('ProgramFiles','C:/Program Files'))/'Tesseract-OCR/tesseract.exe'
        executable=str(fallback) if fallback.is_file() else None
    if not executable:
        raise RuntimeError('Tesseract OCR is not installed')
    with Image.open(image_path) as image:
        color, mono, box=price_crop(image)
    ink=ImageOps.invert(mono.convert('L'))
    columns=[x for x in range(ink.width) if ink.crop((x,0,x+1,ink.height)).getbbox()]
    groups=[]
    for x in columns:
        if not groups or x-groups[-1][-1]>3: groups.append([x])
        else: groups[-1].append(x)
    if len(groups)<2:
        raise ValueError('Could not separate the currency glyph')
    end=groups[-1][0]-2
    # 数字は1文字ずつ離れて描かれているので、塊の数（最後の「円」を除く）が桁数になる
    digit_count=len(groups)-1
    # (前処理, 拡大率) 最初の2回で決まらない時だけ残りも試す
    passes=((0,1),(1,4),(0,3),(1,2))
    results=[]
    with tempfile.TemporaryDirectory(prefix='trader-') as temp:
        for number,(index,scale) in enumerate(passes):
            picture=(color,mono)[index].crop((0,0,end,mono.height))
            picture=ImageOps.expand(picture.resize((picture.width*scale,picture.height*scale)),24,fill='white')
            path=Path(temp)/f'{number}.png';picture.save(path)
            result=subprocess.run([executable,str(path),'stdout','--psm','7','-l','eng',
                '-c','tessedit_char_whitelist=0123456789'],capture_output=True,text=True,timeout=30)
            text=result.stdout.strip()
            if result.returncode: raise RuntimeError(result.stderr.strip())
            results.append({'engine':'tesseract','preprocessing':('color','mask')[index],'scale':scale,
                'text':text,'price':int(text) if re.fullmatch(r'[0-9]+',text) else None})
            if len(results)>=2:
                price=agreed_price(results,digit_count)
                if price is not None: break
    price=agreed_price(results,digit_count)
    return {'candidate_price_yen':price,'ocr_agreement':price is not None,
            'ocr_reads':results,'crop_box':box,'digit_count':digit_count}

def agreed_price(results,digit_count):
    """桁数が画像と合う読み取りが2つ以上あり、すべて同じ値の時だけ確定する（食い違いは要確認のまま）"""
    valid=[r['price'] for r in results if r['price'] is not None and len(r['text'])==digit_count]
    if len(valid)>=2 and len(set(valid))==1:
        return valid[0]
    return None
