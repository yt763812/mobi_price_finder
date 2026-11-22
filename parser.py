#!/usr/bin/env python3
"""MobiStore.by v9: one Excel file, one row per detected color.
Install: python -m pip install playwright beautifulsoup4 lxml
         playwright install chromium
Run:     python mobistore_parser_v9.py

Only a price explicitly displayed after choosing a configuration is accepted.
An option with no separately verifiable price is excluded, not extrapolated.
"""
from __future__ import annotations
import asyncio, csv, json, re, sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

BASE='https://mobistore.by'
OUT=Path('mobistore_catalog_prices_with_colors_v9.xlsx')
MAX_CONCURRENCY=5  # Moderate concurrency; temporary errors are retried
DELAY_MS=70
CLICK_SETTLE_MS=350
HOVER_SETTLE_MS=110
# Read all products from each parent category, not from overlapping children.
# Keep children when no parent category is listed.
PARENT_CHILDREN = {
    'umnye-chasy-i-braslety': {'umnye-chasy', 'fitnes-braslety'},
    'aksessuary': {'chehly', 'zaschitnye-plenki', 'chehly-dlya-planshetov',
                  'apple-pencil', 'zaryadnye-ustrojstva', 'vneshnie-akkumulyatory',
                  'setevoe-zaryadnoe', 'airtag', 'kabeli'},
    'drugaya-tehnika': {'feny-dyson-supersonic', 'stajlery-dyson-airwrap',
                       'vypryamiteli-dyson-corrale'},
}


PRICE_RE=re.compile(r'(?<!\d)(\d[\d\s]*[.,]\d{2})\s*BYN', re.I)


def clean(s): return re.sub(r'\s+', ' ', s or '').strip()
def site_url(url):
    u=urlparse(urljoin(BASE,url))
    return u.scheme in ('http','https') and u.netloc in ('mobistore.by','www.mobistore.by')

def parse_price(text):
    s=clean(text)
    if any(x in s.lower() for x in ('нет в наличии','заказать','уточняйте цену')) and not PRICE_RE.search(s): return None
    ms=PRICE_RE.findall(s)
    if not ms: return None
    try:
        return float(ms[-1].replace(' ', '').replace(',','.'))
    except ValueError: return None

async def get_page_html(page,url):
    # Retry transient navigation failures (ERR_ABORTED, connection resets, timeouts).
    # A single failed request must not silently erase an entire color-variant product.
    last_error = None
    for attempt in range(3):
        try:
            await page.goto(url,wait_until='domcontentloaded',timeout=35000)
            await page.wait_for_timeout(DELAY_MS)
            return await page.content()
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                try:
                    await page.goto('about:blank',wait_until='domcontentloaded',timeout=5000)
                except Exception:
                    pass
                await asyncio.sleep(0.6 * (attempt+1))
    raise last_error

async def catalog_urls(page):
    """Discover primary categories from site's own HTML sitemap."""
    html=await get_page_html(page,BASE+'/sitemap')
    soup=BeautifulSoup(html,'lxml')
    h=next((x for x in soup.find_all(re.compile('^h[1-6]$')) if 'Категории товаров' in x.get_text(' ',strip=True)),None)
    stops={'Товарные страницы','Страницы сайта'}
    urls=[]
    if h:
        for tag in h.next_elements:
            if getattr(tag,'name',None) in ('h1','h2') and tag is not h and any(s in tag.get_text(' ',strip=True) for s in stops):break
            if getattr(tag,'name',None)=='a':
                u=urljoin(BASE,tag.get('href',''))
                if site_url(u) and u not in urls: urls.append(u)
    # Independently fall back to visible navigation categories if sitemap changes
    if not urls:
        for a in soup.select('a[href]'):
            u=urljoin(BASE,a.get('href'))
            if site_url(u) and any(z in u for z in ('mobilnye-telefony','planshety','naushniki','noutbuki','igrovye-konsoli','dyson')):
                if u not in urls:urls.append(u)
    paths={urlparse(u).path.rstrip('/').split('/')[-1] for u in urls}
    skip=set()
    for parent,children in PARENT_CHILDREN.items():
        if parent in paths:
            skip.update(children)
    filtered=[u for u in urls if urlparse(u).path.rstrip('/').split('/')[-1] not in skip]
    print(f'Primary categories: {len(filtered)}; skipped {len(urls)-len(filtered)} overlapping subcategories', flush=True)
    return filtered

# Avoid matching tracking IDs or merging unrelated figures with a price.
STRICT_MONEY = re.compile(r'(?<![\d.,])(?P<amount>\d{1,5}(?:[ \u00a0]\d{3})*(?:[.,]\d{2}))\s*BYN\b', re.I)

def explicit_money(text):
    text=clean(text)
    found=list(STRICT_MONEY.finditer(text))
    if not found:return None
    for m in found:
        n=float(m.group('amount').replace(' ','').replace('\u00a0','').replace(',','.'))
        if 1<=n<=50000:return n
    return None

def catalog_card_price(code_string):
    """Read ONLY a dedicated price node in the card; no parent-wide text matching."""
    node=code_string.parent
    if not node:return None,False
    for ancestor in list(node.parents)[:9]:
        if not getattr(ancestor,'name',None):continue
        if len(ancestor.find_all(string=re.compile(r'Код товара\s*\d+')))!=1:continue
        # Prefer explicit price elements and do not read old/credit prices.
        for e in ancestor.select('[itemprop="price"], [class*="price"], [class*="Price"]'):
            classes=' '.join(e.get('class',[])).lower()
            if any(x in classes for x in ('old','credit','month','installment','economy','discount','saving')):continue
            if e.find_parent(['del','s']):continue
            if e.name in ('del','s'):continue
            n=explicit_money(e.get_text(' ',strip=True))
            if n is not None:
                return n, bool(re.search(r'\bот\s*\d',clean(e.get_text(' ',strip=True)),re.I))
        # A price may not have a class: use only individual (short) leaf text nodes.
        for textnode in ancestor.find_all(string=re.compile(r'BYN',re.I)):
            par=textnode.parent
            if par and (par.find_parent(['del','s']) or par.name in ('del','s')):continue
            t=clean(str(textnode))
            if len(t)>80 or re.search(r'BYN\s*/\s*мес',t,re.I):continue
            n=explicit_money(t)
            if n is not None:return n,bool(re.search(r'\bот\s*\d',t,re.I))
    return None,False


async def discover_products(page,categories):
    """Walk category pagination and collect links associated with product cards."""
    discovered={}
    for cat in categories:
        print('CATEGORY',cat,flush=True)
        seen=set(); queue=[cat]; scanned=0
        while queue and scanned<120:
            url=queue.pop(0)
            if url in seen:continue
            seen.add(url);scanned+=1
            try:html=await get_page_html(page,url)
            except Exception as exc:
                print('  Failed:',url,exc,flush=True);continue
            soup=BeautifulSoup(html,'lxml')
            for code in soup.find_all(string=re.compile(r'Код товара\s*\d+')):
                parent=code.parent
                # Search ancestors for the nearest linked product name.
                for ancestor in list(parent.parents)[:7]:
                    links=[a for a in ancestor.select('a[href]') if a.get_text(' ',strip=True) and site_url(a.get('href'))]
                    links=[a for a in links if not re.search(r'подробнее|сравнить|купить|в корзину',a.get_text(' ',strip=True),re.I)]
                    if links:
                        a=links[0];u=urljoin(BASE,a['href']).split('#')[0]
                        if u not in discovered:
                            price, is_from = catalog_card_price(code)
                            discovered[u]={'category':cat,'price':price,'from':is_from,
                                           'name':re.sub(r'\s+\d{4,8}\s+\d{2,6}$','',clean(a.get_text(' ',strip=True)))}
                        elif discovered[u]['price'] is None:
                            price,is_from=catalog_card_price(code)
                            if price is not None:
                                discovered[u]['price']=price
                                discovered[u]['from']=is_from
                        break
            # Page links, avoid other filters/sorts to prevent infinite traversal
            for a in soup.select('a[href]'):
                href=a.get('href','');text=clean(a.get_text(' ',strip=True))
                candidate=urljoin(url,href)
                if site_url(candidate) and (re.search(r'(?:[?&]page=\d+|/page/\d+)',candidate) or (text.isdigit() and len(text)<4)):
                    if urlparse(candidate).path==urlparse(cat).path and candidate not in seen and candidate not in queue:queue.append(candidate)
            # Some catalog templates have a "Show more" button, but also use a standard page param.
            count_match=re.search(r'Найдено\s+(\d+)\s+товар',soup.get_text(' ',strip=True))
            if count_match and scanned==1:
                total=int(count_match.group(1))
                if sum(1 for v in discovered.values() if v.get('category')==cat)<total:
                    for page_num in range(2,min(1+(total//20)+3,120)):
                        u=cat+('&' if '?' in cat else '?')+f'page={page_num}'
                        if u not in queue and u not in seen:queue.append(u)
        priced=sum(1 for v in discovered.values() if v.get('price') is not None)
        print(f'  Product links so far: {len(discovered)}; catalog priced: {priced}', flush=True)
    return discovered

async def extract_options(page):
    """Detect both text-labeled colors and unlabeled CSS swatches.

    Never use a photograph/image pixel or an inferred manufacturer's color name.
    If exact label is missing, save a basic color family plus measured CSS HEX.
    """
    return await page.evaluate(r'''() => {
      const norm = s => String(s||'').replace(/\s+/g,' ').trim();
      const colorWords=/^(белый|черный|чёрный|серый|серебристый|золотистый|розовый|красный|синий|голубой|зелёный|зеленый|фиолетовый|жёлтый|желтый|оранжевый|бежевый|коричневый|black|white|silver|gold|blue|green|pink|grey|gray|purple|red|yellow|orange|graphite|midnight|starlight|natural|titanium)/i;
      const noName=/^(цвет|color|colour|выберите|выбрать|вариант|варианты|[0-9]+|#[a-f0-9]{3,8}|true|false|on|off|undefined|null|radio|checkbox)$/i;
      const isName = x => {x=norm(x);return x.length>1 && x.length<=65 && !noName.test(x) && !/^rgb|^var\(|^url\(|^data:/i.test(x) && !/^(выбрать|добавить|в корзину)/i.test(x)};
      const toRGB=x=>{
        x=norm(x);if(!x || x==='transparent' || x==='none')return null;
        let m=x.match(/^#([0-9a-f]{3,8})$/i);
        if(m){let v=m[1];if(v.length===3||v.length===4)v=[...v.slice(0,3)].map(z=>z+z).join('');if(v.length>=6)return [0,2,4].map(i=>parseInt(v.slice(i,i+2),16));}
        m=x.match(/rgba?\(\s*(\d+)[, ]+\s*(\d+)[, ]+\s*(\d+)(?:\s*[,/]\s*([\d.]+))?/i);
        if(m){if(m[4]!==undefined && Number(m[4])===0)return null;return [Number(m[1]),Number(m[2]),Number(m[3])];}
        return null;
      };
      const hex=rgb=>'#'+rgb.map(n=>Math.max(0,Math.min(255,n)).toString(16).padStart(2,'0')).join('').toUpperCase();
      const family=rgb=>{
        const [r,g,b]=rgb,max=Math.max(...rgb),min=Math.min(...rgb),delta=max-min;
        if(max<52)return 'Чёрный';
        if(min>228 && delta<24)return 'Белый';
        if(delta<24)return max>183?'Серый светлый':max>100?'Серый':'Тёмно-серый';
        // Hue and chroma determine the broad, reproducible color family.
        let hue=0;
        if(delta){if(max===r)hue=((g-b)/delta)%6;else if(max===g)hue=(b-r)/delta+2;else hue=(r-g)/delta+4;hue=(hue*60+360)%360;}
        const sat=max?delta/max:0;
        if(sat<.20 && max>170)return 'Серебристый';
        if(hue<15||hue>=345)return 'Красный';
        if(hue<46)return max<175?'Коричневый':'Оранжевый';
        if(hue<72)return 'Жёлтый';
        if(hue<170)return 'Зелёный';
        if(hue<205)return 'Бирюзовый';
        if(hue<252)return 'Синий';
        if(hue<293)return 'Фиолетовый';
        return 'Розовый';
      };
      const rgbFor=el=>{
        if(!el)return null;
        const tryEls=[el, ...el.querySelectorAll('span,i,b,em,svg,circle,rect')].slice(0,12);
        for(const t of tryEls){
          const st=getComputedStyle(t);
          const attrs=[t.getAttribute('data-color-code'),t.getAttribute('data-hex'),t.getAttribute('fill'),t.style.backgroundColor,t.style.background,st.backgroundColor,t.getAttribute('value')];
          for(const c of attrs){const rgb=toRGB(c);if(rgb)return rgb;}
        }
        return null;
      };
      const root=document.querySelector('main')||document.querySelector('#content')||document.body;
      const labels=[...root.querySelectorAll('label,legend,span,div,p,strong,b,h3,h4')].filter(e=>e.children.length<=2 && /^(цвет|цвет корпуса|color|colour)\s*:?$/i.test(norm(e.textContent)));
      const candidateSel='label[for],input[type="radio"],[role="radio"],button,[data-color],.color-item,.color-option,.colors-item,.swatch,[class*="color"],[class*="Color"],li';
      let group=null;
      for(const label of labels){
        // Prefer a sibling option block, as in the screenshot: heading "ЦВЕТ" then circles.
        for(let el=label.nextElementSibling,k=0;el&&k<3;el=el.nextElementSibling,k++){
          const n=el.querySelectorAll(candidateSel).length;
          if(n>=2 && n<70){group=el;break;}
          if(el.children.length>=2 && el.children.length<30 && [...el.children].some(x=>rgbFor(x))){group=el;break;}
        }
        if(group)break;
        for(let el=label.parentElement,k=0;el&&k<5;el=el.parentElement,k++){
          if(!el)break;
          const n=el.querySelectorAll(candidateSel).length;
          if(n>=2 && n<60 && el.querySelectorAll('h1').length===0){group=el;break;}
        }
        if(group)break;
      }
      if(!group)return {foundLabel:labels.length>0,options:[]};
      const seenIds=new Set(),raw=[];
      const add=el=>{if(!el||seenIds.has(el))return;seenIds.add(el);raw.push(el)};
      // Label elements with a text caption and native radio buttons.
      for(const el of group.querySelectorAll('label[for], [role="radio"], input[type="radio"], [data-color],.swatch,.color-item,.color-option,.colors-item')){
        if(el.matches('input') && el.labels?.length)add(el.labels[0]);else add(el);
      }
      // CSS-only swatches often have no input or label. Choose innermost clickable elements
      // or direct children of the swatch row, not decorative inner spans.
      for(const el of group.querySelectorAll('button, [style*="background"], [class*="color"], [class*="Color"], li')){
        if(el.closest('h1,h2,h3,h4'))continue;
        if(el.querySelectorAll('button,label,input[type="radio"],[role="radio"]').length>0)continue;
        if(!rgbFor(el) && !colorWords.test(norm(el.textContent)))continue;
        add(el);
      }
      if(raw.length<2){
        for(const el of group.children){if(rgbFor(el)||colorWords.test(norm(el.textContent)))add(el);}
      }
      const output=[],seen=new Set();
      const manageable=raw.filter(el=> {
        if(!el.isConnected || el.closest('script,style,nav,footer'))return false;
        const inp=el.matches('label[for]')?document.getElementById(el.getAttribute('for')):el;
        if(inp?.disabled || el.disabled || el.getAttribute('aria-disabled')==='true')return false;
        // Avoid nested duplicate swatches; prefer a named label, then the outer clickable element.
        return true;
      });
      for(const el of manageable){
        const inp=el.matches('label[for]')?document.getElementById(el.getAttribute('for')):el;
        const img=el.querySelector('img');
        const candidates=[el.getAttribute('data-color-name'),el.getAttribute('data-name'),el.getAttribute('data-label'),el.getAttribute('data-color'),el.getAttribute('aria-label'),el.getAttribute('title'),el.getAttribute('data-title'),el.getAttribute('data-original-title'),el.getAttribute('data-bs-original-title'),inp?.getAttribute('aria-label'),inp?.getAttribute('title'),img?.getAttribute('alt'),img?.getAttribute('title'),el.textContent];
        // Do not treat internal IDs, URL, numeric values, RGB codes as names.
        const name=candidates.map(norm).find(x=>isName(x)&&!/^\d+\s*(гб|gb|тб|tb)$/i.test(x)&&!/^\d[\d /.,-]*$/.test(x)&&!/^\w{20,}$/.test(x));
        const rgb=rgbFor(el);
        if(!name && !rgb)continue;
        const label=name || (family(rgb)+' ('+hex(rgb)+')');
        const ident=(name?'name:'+name.toLowerCase():'rgb:'+hex(rgb));
        if(seen.has(ident))continue;
        seen.add(ident);
        // Prefer outer associated label or button to click, but preserve exact marker.
        const target=el.matches('input') ? (el.labels?.[0]||el) : el;
        target.setAttribute('data-mobistore-color-option',String(output.length));
        output.push({label,index:output.length,source:name?'text':'css',hex:rgb?hex(rgb):null});
      }
      // If too many targets were found, avoid generating spurious products.
      if(output.length>24)return {foundLabel:true,options:[],problem:'too-many-color-controls'};
      return {foundLabel:true,options:output};
    }''')

async def exact_color_name_on_hover(page, index):
    """Read store-provided color name (attributes, CSS tooltip, hover tooltip).

    A CSS hex color is NOT converted into a guessed marketing name.
    """
    selector = f'[data-mobistore-color-option="{index}"]'
    loc = page.locator(selector).first
    try:
        await loc.hover(timeout=3500, force=True)
        await page.wait_for_timeout(HOVER_SETTLE_MS)
    except Exception:
        # DOM attributes can still carry a name even if hovering fails.
        pass
    return await page.evaluate(r'''(selector) => {
      const el=document.querySelector(selector);
      if(!el)return null;
      const norm=x=>String(x||'').replace(/\s+/g,' ').trim().replace(/^['"«]|['"»]$/g,'');
      const reject=/^(?:цвет|цвет корпуса|color|colour|выберите цвет|выбрать|нет|да|выбрано|selected|radio|checkbox|on|off|true|false|undefined|null|\d+|#[a-f0-9]{3,8})$/i;
      const valid=x=>{
        x=norm(x);
        if(x.length<3||x.length>65||reject.test(x))return false;
        if(/(?:\d+\s*(?:гб|gb|тб|tb)|BYN|\d+[.,]\d+|rgba?\(|\bhttps?:|background|#[0-9a-f]{3,8})/i.test(x))return false;
        if(/(?:в корзину|купить|быстрый заказ|рассрочк|характеристик|наличии|сравнить|следить за ценой)/i.test(x))return false;
        // A color name is short, not a sentence or the whole option group.
        if(x.split(/\s+/).length>4)return false;
        return /[a-zа-яё]/i.test(x);
      };
      const attrs=['data-color-name','data-name','data-label','data-tooltip','data-tippy-content','data-bs-title','data-bs-original-title','data-original-title','title','aria-label','data-title','data-content'];
      let candidates=[];
      const push=(value, priority)=>{value=norm(value);if(valid(value))candidates.push({value,priority});};
      // 1. The swatch and its descendants, including title on an SVG/image.
      for(const node of [el,...el.querySelectorAll('*')].slice(0,20)){
        for(const a of attrs)push(node.getAttribute?.(a),100);
        if(node.matches?.('input[type="radio"]'))push(node.value,85);
      }
      // 2. Ancestors often host the tooltip title and event handler.
      let cur=el.parentElement;
      for(let i=0;cur&&i<3;i++,cur=cur.parentElement){
        for(const a of attrs)push(cur.getAttribute(a),75-i*10);
      }
      // 3. Tooltip shown only on hover, often mounted outside product form.
      const tooltipSelectors='[role="tooltip"],.tooltip.show,.tooltip-inner,.tippy-box,.tippy-content,[data-state="delayed-open"][role="tooltip"],[data-state="instant-open"][role="tooltip"],.popover.show';
      for(const tip of document.querySelectorAll(tooltipSelectors)){
        const r=tip.getBoundingClientRect(),st=getComputedStyle(tip);
        if(r.width===0||r.height===0||st.visibility==='hidden'||st.display==='none'||Number(st.opacity)===0)continue;
        const text=norm(tip.innerText||tip.textContent);
        push(text,110);
      }
      // 4. CSS :before/:after tooltips can expose their text in content.
      for(const node of [el,el.parentElement]){
        if(!node)continue;
        for(const pseudo of ['::before','::after']){
          const content=getComputedStyle(node,pseudo).content;
          if(content&&content!=='none'&&content!=='normal')push(content.replace(/^['"]|['"]$/g,''),60);
        }
      }
      // Name on swatch itself, only when it is short.
      push(el.innerText||el.textContent,55);
      candidates.sort((a,b)=>b.priority-a.priority);
      return candidates.length ? candidates[0].value : null;
    }''', selector)

async def displayed_price(page):
    """Read a current product price from a narrow DOM node, never card-wide text."""
    return await page.evaluate(r'''() => {
      const selectors=['[itemprop="price"]','.product-price .price-new','.product-price .price','.price-current','.price-new','.product-info .price','.product-detail .price','.product__price','.price'];
      const rx=/(?<![\d.,])(\d{1,5}(?:[ \u00a0]\d{3})*[.,]\d{2})\s*BYN\b/i;
      const roots=[document.querySelector('main'),document.querySelector('#content'),document.body].filter(Boolean);
      for(const root of roots)for(const sel of selectors)for(const e of root.querySelectorAll(sel)){
        if(e.closest('del,s,.old-price,.price-old,.installment,.credit,.product-card,.product-item,.related-products'))continue;
        const classes=String(e.className||'').toLowerCase();
        if(/old|month|installment|credit|saving/.test(classes))continue;
        if(e.matches('[itemprop="price"]') && e.getAttribute('content')){
          const v=Number(e.getAttribute('content'));if(v>=1&&v<=50000)return v;
        }
        const t=(e.innerText||e.textContent||'').trim();
        if(t.length>150 || /BYN\s*\/\s*мес/i.test(t))continue;
        const m=t.match(rx);
        if(m){const n=Number(m[1].replace(/[\s\u00a0]/g,'').replace(',','.'));if(n>=1&&n<=50000)return n;}
      }
      return null;
    }''')

async def process_product(page,url,info):
    """Return one row for each independently priced, identifiable color option."""
    await get_page_html(page,url)
    soup=BeautifulSoup(await page.content(),'lxml')
    h=soup.find('h1')
    name=clean(h.get_text(' ',strip=True)) if h else clean(info.get('name',''))
    if not name:return [],'no-title'
    # Informational/promotional page mistakenly displayed as a priced catalog card.
    if clean(name).casefold() == 'trade-in в мобистор':
        return [],None
    category=info['category'].rsplit('/',1)[-1]
    when=datetime.now().strftime('%Y-%m-%d %H:%M')
    group=await extract_options(page)
    rows=[]
    if group['options']:
        for opt in group['options']:
            try:
                loc=page.locator(f'[data-mobistore-color-option="{opt["index"]}"]').first
                if await loc.count()==0:continue
                # Read the merchant's actual hover name BEFORE the click may rerender options.
                hover_name=(opt['label'] if opt['source']=='text' else await exact_color_name_on_hover(page,opt['index']))
                await loc.click(force=True,timeout=4000)
                await page.wait_for_timeout(CLICK_SETTLE_MS)
                price=await displayed_price(page)
                if price is None:continue
                # Store official label when available, otherwise explicit textual label,
                # otherwise CSS approximation with HEX so the provenance is visible.
                label=hover_name or opt['label']
                source='Название цвета с сайта' if hover_name else ('Название из кнопки' if opt['source']=='text' else 'Приблизительный цвет по CSS')
                rows.append([category,name,label,price,'Цена выбранного цвета; '+source,when])
            except Exception:
                continue
        if rows:return rows,None
        # Keep a base product with an explicitly listed catalog price rather than lose it.
        # Do not create fictitious color rows or call a minimum price a variant price.
        if info.get('price') is not None:
            return [[category,name,'',info['price'],'Цена каталога; цвет не определён',when]],'unverified-colors'
        return [],'unverified-colors-no-price'
    if group['foundLabel']:
        if info.get('price') is not None:
            return [[category,name,'',info['price'],'Цена каталога; цвет не определён',when]],'color-label-unreadable'
        return [],'color-label-unreadable-no-price'
    price=await displayed_price(page)
    if price is not None:
        return [[category,name,'',price,'Цена карточки',when]],None
    if info.get('price') is not None:
        return [[category,name,'',info['price'],'Цена каталога',when]],None
    return [],'no-price'

def write_xlsx(records, destination=OUT, headers=None):
    """Create an Excel XLSX file with the Python standard library."""
    from zipfile import ZipFile, ZIP_DEFLATED
    from xml.sax.saxutils import escape
    if headers is None:
        headers=['Категория','Модель (карточка)','Цвет','Цена, BYN','Тип цены','Дата выгрузки']
    rows=[headers]+records
    def c(col,row,value):
        addr=f'{chr(65+col)}{row}'
        style=' s="1"' if row==1 else (' s="2"' if col==3 else '')
        if isinstance(value,(int,float)) and not isinstance(value,bool):
            return f'<c r="{addr}"{style}><v>{value}</v></c>'
        return f'<c r="{addr}" t="inlineStr"{style}><is><t>{escape(str(value if value is not None else ""))}</t></is></c>'
    content=''.join(f'<row r="{i}">'+''.join(c(j,i,v) for j,v in enumerate(row))+'</row>' for i,row in enumerate(rows,1))
    decl='<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    base='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    relbase='http://schemas.openxmlformats.org/package/2006/relationships'
    odrel='http://schemas.openxmlformats.org/officeDocument/2006/relationships'
    ws=(f'{decl}<worksheet xmlns="{base}"><sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews><cols>'
        +''.join(f'<col min="{i}" max="{i}" width="{w}" customWidth="1"/>' for i,w in enumerate([24,55,23,20,38,22],1))
        +f'</cols><sheetData>{content}</sheetData><autoFilter ref="A1:{chr(64+len(headers))}{len(rows)}"/></worksheet>')
    styles=f'{decl}<styleSheet xmlns="{base}"><fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><color rgb="FFFFFFFF"/><sz val="11"/></font></fonts><fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF17365D"/></patternFill></fill></fills><borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="3"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/><xf numFmtId="2" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/></cellXfs></styleSheet>'
    book=f'{decl}<workbook xmlns="{base}" xmlns:r="{odrel}"><sheets><sheet name="Товары с ценой" sheetId="1" r:id="rId1"/></sheets></workbook>'
    rootrels=f'{decl}<Relationships xmlns="{relbase}"><Relationship Id="rId1" Type="{odrel}/officeDocument" Target="xl/workbook.xml"/></Relationships>'
    bookrels=f'{decl}<Relationships xmlns="{relbase}"><Relationship Id="rId1" Type="{odrel}/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="{odrel}/styles" Target="styles.xml"/></Relationships>'
    ct=f'{decl}<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>'
    with ZipFile(destination,'w',ZIP_DEFLATED) as z:
        for name,data in [('[Content_Types].xml',ct),('_rels/.rels',rootrels),('xl/workbook.xml',book),('xl/_rels/workbook.xml.rels',bookrels),('xl/worksheets/sheet1.xml',ws),('xl/styles.xml',styles)]:z.writestr(name,data)

async def collect_catalog(*, persist=False):
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True)
        context=await browser.new_context(locale='ru-RU',viewport={'width':1440,'height':900})
        # Image/font/video downloads are unnecessary to read prices or textual tooltip labels.
        # CSS and JavaScript are left enabled: they are needed for swatches and dynamic prices.
        async def block_heavy(route):
            if route.request.resource_type in ('image','media','font'):
                await route.abort()
            else:
                await route.continue_()
        await context.route('**/*',block_heavy)
        catalog_page=await context.new_page()
        categories=await catalog_urls(catalog_page)
        print('Categories:',len(categories),flush=True)
        products=await discover_products(catalog_page,categories)
        await catalog_page.close()
        priced=[(url,info) for url,info in products.items() if info.get('price') is not None]
        print(f'Unique cards: {len(products)}; with catalog price: {len(priced)}; workers: {MAX_CONCURRENCY}',flush=True)
        queue=asyncio.Queue()
        for item in priced:
            queue.put_nowait(item)
        rows=[]
        problems=0
        done=0
        counter_lock=asyncio.Lock()

        async def worker(worker_num):
            nonlocal done,problems
            page=await context.new_page()
            try:
                while True:
                    try:
                        url,info=queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    data=[];issue=None
                    try:
                        data,issue=await process_product(page,url,info)
                    except Exception as exc:
                        issue='exception'
                        print(f'  Retry product: {url}: {str(exc)[:120]}',flush=True)
                        # Retry in a fresh browser tab: aborted navigation can poison a tab.
                        try:
                            await page.close()
                            page=await context.new_page()
                            await asyncio.sleep(0.75)
                            data,issue=await process_product(page,url,info)
                            print(f'  Recovered product: {url}',flush=True)
                        except Exception as retry_exc:
                            issue='exception'
                            print(f'  Problem after retry: {url}: {str(retry_exc)[:150]}',flush=True)
                            data=[[info['category'].rsplit('/',1)[-1],info.get('name',''),'',info['price'],
                                   'Цена каталога; цвет не проверен',datetime.now().strftime('%Y-%m-%d %H:%M')]]
                    async with counter_lock:
                        rows.extend(data)
                        if issue:problems+=1
                        done+=1
                        if done%25==0 or done==len(priced):
                            unique={tuple(str(x).casefold() for x in row[:3]):row for row in rows}
                            if persist:
                                write_xlsx(list(unique.values()))
                            print(f'{done}/{len(priced)} processed; {len(unique)} rows in {OUT}; issues: {problems}',flush=True)
                    queue.task_done()
            finally:
                await page.close()

        await asyncio.gather(*(worker(i) for i in range(MAX_CONCURRENCY)))
        await browser.close()
    unique={tuple(str(x).casefold() for x in row[:3]):row for row in rows}
    if persist:
        write_xlsx(list(unique.values()))
    count_colors=sum(1 for row in unique.values() if row[2])
    print(f'DONE: {len(unique)} rows, {count_colors} color variants, {problems} cases require review',flush=True)
    return {'rows': list(unique.values()), 'products_discovered': len(products), 'products_priced': len(priced), 'products_processed': done, 'problems': problems}

if __name__=='__main__':asyncio.run(collect_catalog(persist=True))
