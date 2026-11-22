#!/usr/bin/env python3
"""Сверка доступных товаров магазина с размещёнными позициями Onlíner (v11).

Запуск:
  python compare_products_v8.py shop.xlsx onliner.xlsx -o result.xlsx
  python compare_products_v8.py shop.xlsx onliner.xlsx --aliases spravochnik_sootvetstviy.csv

Магазин: только строки входной выгрузки с указанной ценой.
Товары без цены, под заказ и дополнительный список товаров НЕ учитываются.
Сопоставление не смешивает модели разных поколений или комплектации Pro / Pro+.
"""
import argparse
from pathlib import Path
from collections import defaultdict, Counter
import csv, re, unicodedata, difflib
import xml.etree.ElementTree as ET
from zipfile import ZipFile

NS={'m':'http://schemas.openxmlformats.org/spreadsheetml/2006/main','r':'http://schemas.openxmlformats.org/officeDocument/2006/relationships'}

def read_xlsx(path, sheet_name):
    with ZipFile(path) as z:
        root=ET.fromstring(z.read('xl/workbook.xml')); rel=ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))
        rm={x.get('Id'):x.get('Target') for x in rel}
        s=next((x for x in root.findall('m:sheets/m:sheet',NS) if x.get('name')==sheet_name),None)
        if s is None: raise ValueError(f'В {path} нет листа {sheet_name!r}')
        dest=rm[s.get('{%s}id'%NS['r'])].lstrip('/')
        dest=dest if dest.startswith('xl/') else 'xl/'+dest
        ss=[]
        if 'xl/sharedStrings.xml' in z.namelist():
            shared=ET.fromstring(z.read('xl/sharedStrings.xml'))
            ss=[''.join(t.text or '' for t in e.findall('.//m:t',NS)) for e in shared.findall('m:si',NS)]
        sheet=ET.fromstring(z.read(dest)); out=[]
        for row in sheet.findall('.//m:sheetData/m:row',NS):
            cells={}
            for c in row.findall('m:c',NS):
                letters=re.match('[A-Z]+',c.get('r',''))
                if not letters: continue
                idx=0
                for ch in letters.group():idx=idx*26+ord(ch)-64
                t=c.get('t');v=c.find('m:v',NS)
                if t=='inlineStr':value=''.join(n.text or '' for n in c.findall('.//m:t',NS))
                elif v is None or v.text is None:value=''
                elif t=='s':value=ss[int(v.text)]
                else:
                    value=v.text
                    if t not in ('str','e','b'):
                        try:
                            num=float(value);value=int(num) if num.is_integer() else num
                        except (ValueError,TypeError):pass
                cells[idx-1]=value
            out.append([cells.get(i,'') for i in range(max(cells,default=-1)+1)])
        return out

def nrm(value):
    s=unicodedata.normalize('NFKC',str(value or '')).lower().replace('ё','е')
    s=s.replace('–','-').replace('—','-').replace('‑','-')
    s=re.sub(r'(?<=\w)\+(?!\w)',' plus ',s)
    s=re.sub(r'\bгб\b','gb',s);s=re.sub(r'\bтб\b','tb',s)
    s=re.sub(r'\be[ -]?sim\b','esim',s)
    s=re.sub(r'\s*/\s*','/',s)
    s=re.sub(r'[^\w/]+',' ',s)
    return ' '.join(s.split())

def get_aliases(path):
    out={'model':{},'color':{}}
    if not path: return out
    with open(path,encoding='utf-8-sig',newline='') as f:
        probe=f.read(2048);f.seek(0)
        delimiter=';' if probe.count(';')>=probe.count(',') else ','
        for r in csv.DictReader(f,delimiter=delimiter):
            kind=str(r.get('тип',r.get('type',''))).lower().strip()
            if kind not in out:continue
            src=nrm(r.get('исходное',r.get('original','')))
            dst=nrm(r.get('каноническое',r.get('canonical','')))
            if src and dst:out[kind][src]=dst
    return out

REGIONS=[('china',r'\b(?:китайская|китайский|china|chinese|cn)\s*(?:версия|version)?\b'),
         ('india',r'\b(?:индийская|индийский|india|indian)\s*(?:версия|version)?\b'),
         ('global',r'\b(?:международная|международный|глобальная|global|international|европейская|европейский)\s*(?:версия|version)?\b')]

def spec(name,aliases):
    s=nrm(name);reg='global'
    for key,pat in REGIONS:
        if re.search(pat,s):reg=key;s=re.sub(pat,' ',s);break
    s=re.sub(r'\bверсия\b',' ',s)
    # Код производителя удаляется только по известным структурным шаблонам.
    s=re.sub(r'\b(?:sm[ -]?[a-z]\d{3,5}[a-z0-9]*|xq[ -]?[a-z]{2}\d{2}|rmx\d{4,5}|cph\d{4,5})\b',' ',s)
    mem=re.search(r'(?<!\w)(\d+)\s*gb\s*/\s*(\d+)\s*(gb|tb)\b',s)
    if not mem:mem=re.search(r'(?<!\w)(\d+)\s*/\s*(\d+)\s*(gb|tb)\b',s)
    ram=None;storage=None
    if mem:
        ram=int(mem.group(1));storage=int(mem.group(2))*(1024 if mem.group(3)=='tb' else 1)
        s=s[:mem.start()]+' '+s[mem.end():]
    else:
        mem=re.search(r'(?<!\w)(\d+)\s*(gb|tb)\b',s)
        if mem:
            storage=int(mem.group(1))*(1024 if mem.group(2)=='tb' else 1)
            s=s[:mem.start()]+' '+s[mem.end():]
    sim='esim' if re.search(r'\b(?:dual\s+)?esim\b',s) else ('nano' if re.search(r'\bnano\s+sim\b',s) else None)
    s=re.sub(r'\b(?:dual\s+)?esim\b|\bnano\s+sim\b|\bdual\s+sim\b',' ',s)
    radio='5g' if re.search(r'\b5g\b',s) else ('4g' if re.search(r'\b4g\b',s) else None)
    s=re.sub(r'\b[45]g\b',' ',s)
    base=' '.join(s.split())
    base=aliases['model'].get(base,base)
    return {'base':base,'ram':ram,'storage':storage,'sim':sim,'radio':radio,'region':reg}

def color_key(c,aliases):
    s=nrm(re.sub(r'\s*\(\s*#[0-9a-fA-F]{3,8}\s*\)', '', str(c or '')))
    if not s:return ''
    tokens=s.split('/')
    tokens=sorted(tokens) if len(tokens)>1 else tokens
    s='/'.join(tokens)
    defaults={'золотой':'золотистый','графит':'графитовый','лаванда':'лавандовый',
              'темно серый':'темно серый'}
    s=defaults.get(s,s)
    return aliases['color'].get(s,s)

def parse_catalog(brand, name):
    """Отделяет цвет от характеристик внутри последних скобок.

    Например AirPods Max (с разъёмом USB Type-C, фиолетовый)
    -> модель с USB-C + цвет фиолетовый.
    """
    label=str(name or '').strip();color=''
    m=re.search(r'\s*\(([^()]*)\)\s*$',label)
    if m:
        inside=m.group(1).strip()
        label=label[:m.start()].strip()
        parts=[p.strip() for p in re.split(r'[,;]',inside) if p.strip()]
        kept=[]
        for part in parts:
            rawver=re.search(r'(?:китайская|индийская|международная|глобальная|европейская)\s+версия|(?:china|india|global|international)\s+version',part,re.I)
            if rawver:
                label+=' '+rawver.group(0)
                part=(part[:rawver.start()]+' '+part[rawver.end():]).strip(' ,;')
                if not part: continue
            if re.search(r'шумоподавлен|noise cancel|\banc\b|\bgps\b|\blte\b|\bcellular\b|wi[ -]?fi|\d+\s*шт|комплект|bundle|type[ -]?c|usb[ -]?c|magsafe',part,re.I):
                kept.append(part)
            else: color=part
        if kept:label+=' '+' '.join(kept)
    if brand and not nrm(label).startswith(nrm(brand)+' '):label=str(brand).strip()+' '+label
    return label.strip(),color

def get_model_colors(path):
    """Модельно-зависимые эквиваленты цветов: один цвет магазина -> один цвет Onliner."""
    mapping={}
    if not path or not Path(path).exists():return mapping
    with open(path,encoding='utf-8-sig',newline='') as f:
        for r in csv.DictReader(f,delimiter=';'):
            base=nrm(r.get('модель',''))
            src=nrm(r.get('цвет магазина',''))
            dest=nrm(r.get('цвет onliner',''))
            if base and src and dest:mapping[(base,src)]=dest
    return mapping

def comparable_color(item,model_colors):
    base=item['attrs']['base']; ck=item['ck']
    return model_colors.get((base,ck),ck)

def build(shop,online,aliases):
    hs=[str(x).strip() for x in shop[0]]; ho=[str(x).strip() for x in online[0]]
    for h in ('Категория','Модель (карточка)','Цвет','Цена, BYN'):
        if h not in hs:raise ValueError('Нет поля магазина: '+h)
    for h in ('Раздел','Бренд','Модель','productId'):
        if h not in ho:raise ValueError('Нет поля Onliner: '+h)
    a={x:hs.index(x) for x in hs};b={x:ho.index(x) for x in ho}
    def col(row,i):return row[i] if i<len(row) else ''
    stores=[];catalog=[];excluded=0
    for line,row in enumerate(shop[1:],2):
        name=str(col(row,a['Модель (карточка)']) or '').strip();color=str(col(row,a['Цвет']) or '').strip()
        price=col(row,a['Цена, BYN'])
        if not name:continue
        if price=='' or price is None:excluded+=1;continue
        itemname=name
        if color and nrm(name).endswith(' '+nrm(color)):itemname=name[:-len(color)].strip(' ,;()-')
        stores.append({'row':line,'name':name,'color':color,'ck':color_key(color,aliases),'price':price,'cat':str(col(row,a['Категория'])),'attrs':spec(itemname,aliases)})
    for line,row in enumerate(online[1:],2):
        name=str(col(row,b['Модель']) or '').strip()
        if not name:continue
        fullname,color=parse_catalog(col(row,b['Бренд']),name)
        catalog.append({'row':line,'name':name,'color':color,'ck':color_key(color,aliases),'id':str(col(row,b['productId'])),'cat':str(col(row,b['Раздел'])),'attrs':spec(fullname,aliases)})
    return stores,catalog,excluded

CATEGORY_MAP={
 'mobilnye-telefony':'smartphone','мобильные телефоны':'smartphone',
 'planshety':'tablet','планшеты':'tablet',
 'umnye-chasy-i-braslety':'watch','умные часы и браслеты':'watch',
 'naushniki':'headphones','наушники и гарнитуры':'headphones',
 'noutbuki':'laptop','ноутбуки':'laptop',
 'portativnye-kolonki':'speaker','портативные колонки':'speaker',
 'ekshen-kamery':'camera','экшен-камеры':'camera',
 'аксессуары':'accessory','aksessuary':'accessory',
 'стилусы и сменные наконечники':'accessory',
 'drugaya-tehnika':'other', 'другая техника':'other',
 'ochki-virtualnoj-realnosti':'vr', 'igrovye-konsoli':'console',
}

def category(item):
    return CATEGORY_MAP.get(str(item['cat']).lower().strip(),str(item['cat']).lower().strip())

def compatible(a,b):
    if a['base']!=b['base'] or a['region']!=b['region']:return False
    for k in ('ram','storage','radio','sim'):
        if a[k] is not None and b[k] is not None and a[k]!=b[k]:return False
    return True

def missing_specs(a,b):
    return [k for k in ('ram','storage','radio','sim') if (a[k] is None)!=(b[k] is None)]

def fixed_color_when_missing(item):
    """Только устройство с известной единственной заводской расцветкой."""
    base=item['attrs']['base']
    return (base in ('apple pencil pro','apple pencil usb c',
                     'apple airpods 4 без активного шумоподавления',
                     'apple airpods 4 с активным шумоподавлением',
                     'apple airpods pro 3') and item['ck']=='белый')

def model_family(base):
    """Сохраняем реальную линейку: Note != Redmi; Ear != Phone; Buds != Pad."""
    x=base.split()
    if len(x)<2:return base
    if x[0]=='xiaomi' and 'redmi' in x:
        return 'xiaomi redmi note' if 'note' in x else 'xiaomi redmi'
    if x[0]=='nothing':return 'nothing '+x[1]
    if x[0]=='oneplus':return 'oneplus '+('nord buds' if 'nord' in x and 'buds' in x else x[1])
    if x[0]=='sony' and 'xperia' in x:return 'sony xperia'
    if x[0]=='apple' and 'airpods' in x:return 'apple airpods'
    if x[0]=='apple' and 'iphone' in x:return 'apple iphone'
    if x[0]=='samsung' and 'galaxy' in x:return 'samsung galaxy '+(x[2] if len(x)>2 else '')
    return ' '.join(x[:2])

def likely_same_model(a,b):
    if a==b or model_family(a)!=model_family(b):return False
    aa=a.split();bb=b.split()
    nums=lambda toks:[t for t in toks if re.search(r'\d',t) or re.fullmatch(r'(?:vi|vii|viii|ix|iv|x|xi|xii)',t)]
    if nums(aa)!=nums(bb):return False
    tier={'pro','plus','ultra','max','lite','fe','mini','air','edge','neo','note','buds','phone','ear','pad'}
    if {x for x in aa if x in tier}!={x for x in bb if x in tier}:return False
    return difflib.SequenceMatcher(None,a,b).ratio()>=0.87

def color_family(col):
    words=col.split()
    groups={
     'синий':{'синий','голубой','лазурь','бирюзовый','небо','индиго'},
     'серый':{'серый','графитовый','графит','стальной','титановый'},
     'красный':{'красный','алый','бордовый','бургунди','винный'},
     'фиолетовый':{'фиолетовый','сиреневый','лавандовый','лаванда'},
     'золотистый':{'золотистый','золотой','золото','шампань'},
     'белый':{'белый','серебристый','жемчужный','снежный'},
     'зеленый':{'зеленый','мятный','изумрудный','оливковый'},
     'черный':{'черный','черное','графитовый','ночное','темный'},
    }
    hits=set()
    for group,vocab in groups.items():
        if set(words)&vocab:hits.add(group)
    return hits

def ambiguous_colors(a,b):
    if not a or not b:return True
    if a==b:return False
    x=color_family(a);y=color_family(b)
    if x and y and x&y:return True
    # Фирменные необычные названия требуют проверки, если цвет не выводится из слов.
    if not x or not y:return True
    return False

def choose_candidates(cands,limit=4):
    # Сначала наиболее близкие цвета и совпавшие объёмы памяти.
    result=[];seen=set()
    for c in cands:
        key=(c['id'],c['name'])
        if key not in seen:seen.add(key);result.append(c)
    return result[:limit]

def decide(item,index,keys,model_colors):
    s=item['attrs'];same=[x for x in index.get(s['base'],[]) if category(x)==category(item)]
    possible=[x for x in same if compatible(s,x['attrs'])]
    color=comparable_color(item,model_colors)
    exact=[x for x in possible if color and color==comparable_color(x,model_colors)]
    safe_missing_color=[x for x in possible if not x['ck'] and fixed_color_when_missing(item)]
    eq=exact or safe_missing_color
    if eq:
        # Не подтверждать карточку без RAM/накопителя, если другой источник содержит
        # несколько конфигураций одного базового устройства с такой же памятью.
        safe=[];ambiguous=[]
        for x in eq:
            miss=missing_specs(s,x['attrs'])
            needs_review=any(k in ('ram','storage') for k in miss) and category(item) in ('smartphone','tablet','laptop')
            if needs_review:ambiguous.append(x)
            else:safe.append(x)
        if safe:
            return 'Уже на Onliner','Совпали модель, значимые характеристики и цвет'+(' (цвет товара фиксированный)' if safe_missing_color and not exact else ''),choose_candidates(safe)
        return 'Проверить','Цвет совпал, но в одной выгрузке не указана память/RAM',choose_candidates(ambiguous)
    if possible:
        # Если ровно этот цвет уже существует у другой памяти, это доказательство,
        # что цвет отдельный, а не синоним других оттенков нужной конфигурации.
        if color and any(comparable_color(x,model_colors)==color and x['attrs']['region']==s['region'] and x not in possible for x in same):
            if all(x['ck'] for x in possible):
                return 'Можно добавить','Этот цвет уже есть у другого объёма памяти; нужная конфигурация отсутствует',choose_candidates(possible+[x for x in same if comparable_color(x,model_colors)==color])
        if not color:return 'Проверить','Нет цвета в магазине: нельзя подтвердить модификацию',choose_candidates(possible)
        if any(not x['ck'] for x in possible):
            return 'Проверить','В Onliner не указан цвет: возможна та же модификация',choose_candidates(possible)
        if any(ambiguous_colors(color,comparable_color(x,model_colors)) for x in possible):
            return 'Проверить','Одинаковая модель и память; возможный синоним фирменного цвета',choose_candidates(possible)
        return 'Можно добавить','Новый цвет при совпадающих модели, памяти и регионе',choose_candidates(possible)
    if same:
        # Разный объём памяти/RAM или регион — отдельная модификация, не совпадение.
        # Если цвет есть у другой памяти, это по-прежнему отдельный товар.
        regions={x['attrs']['region'] for x in same}
        if s['region'] not in regions:reason='Другая региональная версия (отдельная модификация)'
        elif any(s['storage']!=x['attrs']['storage'] or s['ram']!=x['attrs']['ram'] for x in same):reason='Отдельная конфигурация RAM/встроенной памяти'
        else:reason='Другая модификация связи, комплектации или характеристик'
        return 'Можно добавить',reason,choose_candidates(same)
    # Подсказки допускаются только в той же категории И той же линейке.
    suggestions=[]
    for base in keys:
        if likely_same_model(s['base'],base):
            suggestions.extend(x for x in index[base] if category(x)==category(item))
    if suggestions:return 'Проверить','Похожее написание модели в той же товарной линейке; нужен контроль',choose_candidates(suggestions)
    return 'Можно добавить','Модель отсутствует в выгрузке Onliner',[]

def get_confirmations(path):
    """Ручные подтверждения: точная запись магазина + цвет -> productId.

    Пары не применяются, когда явные регион, память, RAM и прочие
    характеристики противоречат друг другу.
    """
    out={}
    if not path or not Path(path).exists():return out
    with open(path,encoding='utf-8-sig',newline='') as f:
        for row in csv.DictReader(f,delimiter=';'):
            title=nrm(row.get('модель магазина',''))
            color=nrm(row.get('цвет магазина',''))
            pid=str(row.get('productId Onliner','')).strip()
            if title and pid:out[(title,color)]=pid
    return out

def find_confirmation(item,catalog_by_id,confirmations):
    key=(nrm(item['name']),nrm(item['color']))
    pid=confirmations.get(key)
    if not pid:return None
    target=catalog_by_id.get(pid)
    if not target:return None
    if category(item)!=category(target):return None
    a=item['attrs'];b=target['attrs']
    if a['region']!=b['region']:return None
    if a['base'].split()[0]!=b['base'].split()[0]:return None
    for specname in ('ram','storage','sim','radio'):
        if a[specname] is not None and b[specname] is not None and a[specname]!=b[specname]:return None
    return target

def make_report(stores,catalog,excluded,model_colors,confirmations=None):
    index=defaultdict(list)
    for x in catalog:index[x['attrs']['base']].append(x)
    catalog_by_id={x['id']:x for x in catalog if x['id']}
    confirmations=confirmations or {}
    labels=['Категория магазина','Модель магазина','Цвет магазина','Цена, BYN','Причина','Варианты Onliner','productId Onliner','Строка магазина']
    add=[labels];review=[labels];matched=[labels]
    stats=Counter()
    for s in stores:
        status,reason,candidates=decide(s,index,index.keys(),model_colors)
        confirmed=find_confirmation(s,catalog_by_id,confirmations)
        if confirmed:
            status,reason,candidates='Уже на Onliner','Ручное подтверждение по productId (характеристики проверены)',[confirmed]
        stats[status]+=1
        names='; '.join(dict.fromkeys(c['name'] for c in candidates))[:1200]
        ids='; '.join(dict.fromkeys(c['id'] for c in candidates))[:500]
        row=[s['cat'],s['name'],s['color'],s['price'],reason,names,ids,s['row']]
        (matched if status=='Уже на Onliner' else review if status=='Проверить' else add).append(row)
    # Диагностический лист — не влияет на товары, рекомендованные к добавлению.
    byshop=defaultdict(list)
    for s in stores:byshop[s['attrs']['base']].append(s)
    diag=[['Раздел Onliner','Модель Onliner','Цвет','productId','Статус относительно выгрузки магазина','Возможная модель магазина','Примечание']]
    diag_counts=Counter()
    for c in catalog:
        same=byshop.get(c['attrs']['base'],[])
        eq=[x for x in same if compatible(c['attrs'],x['attrs']) and comparable_color(x,model_colors) and (comparable_color(x,model_colors)==comparable_color(c,model_colors) or (not c['ck'] and fixed_color_when_missing(x)))]
        if eq:st='Совпало';cand=eq
        elif same:st='Проверить модель/цвет/модификацию';cand=same
        else:st='Нет в ценовой выгрузке магазина';cand=[]
        diag_counts[st]+=1
        diag.append([c['cat'],c['name'],c['color'],c['id'],st,'; '.join(x['name'] for x in cand[:3]),'Не означает отсутствие на сайте' if not same else ''])
    summary=[['ПОКАЗАТЕЛЬ','ЗНАЧЕНИЕ'],
       ['ПОЗИЦИИ МАГАЗИНА С ЦЕНОЙ',len(stores)],['ПОЗИЦИИ ONLINER',len(catalog)],
       ['МОЖНО ДОБАВИТЬ',stats['Можно добавить']],['ПРОВЕРИТЬ ПЕРЕД ДОБАВЛЕНИЕМ',stats['Проверить']],
       ['УЖЕ НА ONLINER',stats['Уже на Onliner']],['Строк без цены в исходной выгрузке',excluded],
       ['',''],['КАК ЧИТАТЬ ОТЧЁТ',''],
       ['Можно добавить','Нет подтверждённого совпадения по модели, модификации и цвету. Отличие памяти/региона допускает отдельную карточку.'],
       ['Проверить','Не размещать автоматически: возможно то же устройство под другим названием/цветом.'],
       ['Уже на Onliner','Совпадают модель, совместимые характеристики и цвет.'],
       ['Приоритет','Перед массовым размещением проверьте лист «Проверить» и выборку из «Можно добавить».'],
       ['Регион','Если не указан — международный. Китайская/индийская версии — отдельные.'],
       ['eSIM','eSIM и Dual eSIM приравниваются. Явные nano-SIM и eSIM считаются разными.'],
       ['Поколение','A17 ≠ A18; Pro ≠ Pro+; память/радиомодуль проверяются отдельно.'],
       ['Особые цвета','Фирменные синонимы — только в справочнике цветов конкретной модели; сомнения в проверку.'],
       ['Разная память','Другой RAM / встроенная память — новая модификация; не приравнивается к существующей.'],
       ['Без цены','Не участвуют в списках добавления и проверки.'],
       ['Контроль Onliner','Диагностический лист не влияет на основной список.'],
       ['Подтверждения','Файл podtverzhdennye_pary.csv: модель магазина;цвет магазина;productId Onliner. Несовместимая память/регион не подтверждаются.']]
    color_audit=[['Категория','Модель (нормализованная)','Цвет магазина','Цвет Onliner (авто)','Результат определения','Цветовая палитра Onliner']]
    for key,(color_map,evidences) in sorted(palettes.items()):
        site_colors=sorted({x['ck'] for x in byshop.get(key,[]) if x['ck']})
        online_colors=sorted({x['ck'] for x in index.get(key[1],[]) if category(x)==key[0] and x['ck']})
        for original in site_colors:
            dest=color_map.get(original,'')
            color_audit.append([key[0],key[1],original,dest,evidences.get(original,'Не найдено однозначное соответствие' if online_colors else 'Модель отсутствует на Onliner'),'; '.join(online_colors)[:1000]])
    return [('Сводка',summary),('Можно добавить',add),('Проверить',review),('Уже на Onliner',matched),('Контроль Onliner',diag),('Автоцвета',color_audit)],stats,diag_counts

def export(sheets, path):
    """Экспорт в XLSX через openpyxl, без зависимости от artifact_tool."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.dimensions import ColumnDimension

    workbook = Workbook()
    workbook.remove(workbook.active)
    for title, rows in sheets:
        ws = workbook.create_sheet(title)
        for row in rows:
            ws.append([value if value is not None else '' for value in row])
        if not rows:
            continue
        ws.freeze_panes = 'A2'
        ws.auto_filter.ref = ws.dimensions
        ws.row_dimensions[1].height = 31
        for cell in ws[1]:
            cell.fill = PatternFill('solid', fgColor='17365D')
            cell.font = Font(bold=True, color='FFFFFF')
            cell.alignment = Alignment(wrap_text=True, vertical='center')
        for idx in range(1, len(rows[0]) + 1):
            width = 42 if idx - 1 in (1, 5) else 26 if idx - 1 in (0, 2, 4) else 18
            ws.column_dimensions[get_column_letter(idx)].width = width
        if title == 'Сводка':
            for row in ws.iter_rows(min_row=1, max_row=min(7, ws.max_row), min_col=1, max_col=1):
                row[0].font = Font(bold=True, color='FFFFFF' if row[0].row == 1 else '000000')
            for range_name, color in (('A4:B4', 'DCFCE7'), ('A5:B5', 'FEF3C7')):
                for row in ws[range_name]:
                    for cell in row:
                        cell.fill = PatternFill('solid', fgColor=color)
    workbook.save(str(path))

def main():
    parser=argparse.ArgumentParser(description='Сверка доступных товаров магазина с Onliner (v9)')
    parser.add_argument('shop',help='XLSX магазина — лист «Товары с ценой»')
    parser.add_argument('onliner',help='XLSX Onliner — лист «Все товары»')
    parser.add_argument('-o','--output',default='rezultat_sverki_v9.xlsx')
    parser.add_argument('--aliases',help='CSV с колонками тип;исходное;каноническое')
    parser.add_argument('--model-colors',help='CSV: модель;цвет магазина;цвет onliner')
    parser.add_argument('--confirmed',help='CSV ручных подтверждений: модель магазина;цвет магазина;productId Onliner')
    a=parser.parse_args()
    default=Path(__file__).with_name('spravochnik_sootvetstviy.csv')
    aliases=get_aliases(a.aliases if a.aliases else default if default.exists() else None)
    default_colors=Path(__file__).with_name('цвета_по_моделям.csv')
    model_colors=get_model_colors(a.model_colors if a.model_colors else default_colors)
    stores,catalog,excluded=build(read_xlsx(a.shop,'Товары с ценой'),read_xlsx(a.onliner,'Все товары'),aliases)
    default_confirmed=Path(__file__).with_name('podtverzhdennye_pary.csv')
    confirmations=get_confirmations(a.confirmed if a.confirmed else default_confirmed)
    sheets,counts,diag=make_report(stores,catalog,excluded,model_colors,confirmations)
    export(sheets,a.output)
    print('Магазин:',len(stores),'Onliner:',len(catalog),'Без цены пропущено:',excluded)
    print('Статусы магазина:',dict(counts),'Контроль Onliner:',dict(diag))
    print('Готовый отчёт:',a.output)

# === v9: automatic palette-aware colors and explicit variant matching ===
VERSION = 'v10'
_old_spec = spec
_old_parse_catalog = parse_catalog
_old_build = build
_old_decide = decide
_old_category = category

# Keep radio, physical SIM and special editions as distinct variants when explicit.
def spec(name, aliases):
    s = nrm(name)
    a = _old_spec(name, aliases)
    a['region_explicit'] = any(re.search(pattern, s) for _,pattern in REGIONS)
    # The original implementation lost part of the device name when memory notation used 1 TB separated by spaces.
    a['connectivity'] = ('cellular' if re.search(r'\b(?:cellular|lte|4g|5g)\b',s) and re.search(r'\b(?:ipad|watch|tab|pad|планшет|часы)\b',s)
                          else 'wifi' if re.search(r'\bwi\s*fi\b',s) else None)
    a['anc'] = 'without' if re.search(r'\b(?:без|without)\b.{0,35}\b(?:шумоподавлен|anc|noise)\b',s) else ('with' if re.search(r'\b(?:с|with|active)\b.{0,35}\b(?:шумоподавлен|anc|noise)\b',s) else None)
    a['sim'] = 'esim' if re.search(r'\b(?:dual\s*)?esim\b',s) else ('physical' if re.search(r'\b(?:nano\s*sim|physical\s*sim|sim\s*card)\b',s) else a['sim'])
    # Remove recognized modifiers from the model name, to support asymmetric naming.
    base=a['base']
    base=re.sub(r'\b(?:wi\s*fi|cellular|lte)\b',' ',base)
    base=re.sub(r'\b(?:с|без)\s+(?:активным\s+)?шумоподавлени[ея]м?\b',' ',base)
    a['base']=' '.join(base.split())
    a['base']=aliases['model'].get(a['base'],a['base'])
    return a

def parse_catalog(brand, name):
    label=str(name or '').strip()
    suffix=re.search(r'\s*\(([^()]*)\)\s*$',label)
    if not suffix: return _old_parse_catalog(brand,name)
    inner=suffix.group(1)
    # A quantity, interface, or model number in brackets is not a color.
    if re.search(r'\b\d+\s*шт\b|\b(?:type\s*c|usb\s*c|magsafe|комплект|bundle|gps|lte|cellular|wi\s*fi|anc|noise|шумоподавлен)\b',inner,re.I):
        parts=[p.strip() for p in re.split('[,;]',inner) if p.strip()]
        attrs=[];colors=[]
        for p in parts:
            if re.search(r'\b\d+\s*шт\b|\b(?:type\s*c|usb\s*c|magsafe|комплект|bundle|gps|lte|cellular|wi\s*fi|anc|noise|шумоподавлен)\b',p,re.I):attrs.append(p)
            else:colors.append(p)
        if attrs and not colors:
            model=label[:suffix.start()]+' '+' '.join(attrs)
            if brand and not nrm(model).startswith(nrm(brand)+' '):model=str(brand).strip()+' '+model
            return model.strip(),''
    return _old_parse_catalog(brand,name)

# Color descriptors with safely controlled synonym families. No generic dark/light collapse.
COLOR_ROOTS = {
 'голубой':('голубой','голубая','лазурь','лазурный','небесный','sky','ice blue','ледяной голубой'),
 'синий':('синий','синяя','blue','индиго','navy','ультрамарин'),
 'белый':('белый','белая','white','снежный','snow'),
 'серебристый':('серебристый','серебряный','silver'),
 'серый':('серый','серая','grey','gray','графитовый','графит'),
 'черный':('черный','черная','чёрный','black','обсидиан','obsidian'),
 'фиолетовый':('фиолетовый','фиолетовая','purple','сиреневый','лавандовый','лаванда'),
 'розовый':('розовый','розовая','pink'),
 'красный':('красный','красная','red','бордовый','бургунди'),
 'оранжевый':('оранжевый','оранжевая','orange'),
 'золотистый':('золотистый','золотой','золото','gold','golden'),
 'зеленый':('зеленый','зеленая','green','мятный','оливковый','изумрудный'),
 'бежевый':('бежевый','бежевая','beige','песочный'),
}
COLOR_ALIAS={}
for root,variants in COLOR_ROOTS.items():
    for synonym in variants:COLOR_ALIAS[nrm(synonym)]=root

COLOR_ADJECTIVES={'ледяной','ледяная','космический','космическая','небесный','небесная','глубокий','глубокая','титановый','титановая','титан','натуральный','натуральное','яркий','яркая','звездный','звездная','звездное','звездно','матовый','матовая','глянцевый'}

def root_color(s):
    s=nrm(s)
    if not s:return ''
    if s in COLOR_ALIAS:return COLOR_ALIAS[s]
    tokens=s.split()
    found=[]
    for token in tokens:
        if token in COLOR_ALIAS:found.append(COLOR_ALIAS[token])
    return found[0] if len(set(found))==1 else ''

def shade_signature(s):
    s=nrm(s)
    # These qualifiers can indicate a genuinely different factory color.
    tone=('dark' if any(t in s.split() for t in ('темно','темный','темная','deep','dark','ночной','полуночный')) else
          'light' if any(t in s.split() for t in ('светло','светлый','светлая','light')) else '')
    return tone

def color_score(shop_color, online_color):
    a=nrm(shop_color);b=nrm(online_color)
    if not a or not b:return (0,'нет цвета')
    if a==b:return (100,'точное совпадение')
    if a.replace('ё','е')==b.replace('ё','е'):return (100,'нормализация')
    ra,rb=root_color(a),root_color(b)
    if not ra or not rb or ra!=rb:return (0,'разные или неизвестные цвета')
    if shade_signature(a)!=shade_signature(b):return (50,'неоднозначная яркость/тон')
    ta=set(a.split());tb=set(b.split())
    if ta<=tb or tb<=ta:return (94,'основной цвет и уточняющее название')
    if a in COLOR_ALIAS and b in COLOR_ALIAS:return (84,'известный языковой синоним')
    return (76,'похожий цвет, но разные уточнения')

def same_variant(a,b):
    if a['base']!=b['base']:return False
    if a['region']!=b['region'] and a.get('region_explicit') and b.get('region_explicit'):return False
    for k in ('ram','storage','radio','sim','connectivity','anc'):
        if a.get(k) is not None and b.get(k) is not None and a[k]!=b[k]:return False
    return True

def variant_uncertainties(a,b,category_name):
    problems=[]
    if a['region']!=b['region'] and not (a.get('region_explicit') and b.get('region_explicit')):problems.append('регион указан только в одной выгрузке')
    for k in ('ram','storage','sim','radio','connectivity','anc'):
        if (a.get(k) is None)!=(b.get(k) is None):
            if k in ('ram','storage') and category_name not in ('smartphone','tablet','laptop'):continue
            if k=='ram' and a.get('storage')==b.get('storage') and a.get('storage') is not None:
                # RAM omissions remain reviewable in phones/tablets/laptops.
                pass
            problems.append({'ram':'RAM','storage':'объём памяти','sim':'тип SIM','radio':'4G/5G','connectivity':'Wi-Fi/Cellular','anc':'шумоподавление'}[k]+' отсутствует в одном источнике')
    return problems

def variant_conflict(a,b):
    for k in ('ram','storage','sim','radio','connectivity','anc'):
        if a.get(k) is not None and b.get(k) is not None and a[k]!=b[k]:return True
    return a['region']!=b['region'] and a.get('region_explicit') and b.get('region_explicit')

def palette_resolution(items,catalog):
    # Determine ONE unique online color for a site color, at model/category level.
    # A second visually similar color in either palette is a hard ambiguity guard.
    shop_colors={x['ck'] for x in items if x['ck']}
    online_colors={x['ck'] for x in catalog if x['ck']}
    mapping={}; evidence={}
    for sc in shop_colors:
        if sc in online_colors:
            mapping[sc]=sc;evidence[sc]='точное совпадение цвета'
            continue
        ranked=sorted([(color_score(sc,oc)[0],oc,color_score(sc,oc)[1]) for oc in online_colors],reverse=True)
        if not ranked or ranked[0][0]<84:continue
        score,oc,how=ranked[0]
        competitors=[x for x in ranked if x[0]>=score-12 and x[1]!=oc]
        claimed=[other for other in shop_colors if other!=sc and (other==oc or color_score(other,oc)[0]>=score-12)]
        if competitors or claimed:continue
        # A semantic color that is a different factory shade is not auto-matched.
        if score<94 and (sc not in COLOR_ALIAS or oc not in COLOR_ALIAS):continue
        mapping[sc]=oc;evidence[sc]=f'автоматическое соответствие палитры: {sc} → {oc} ({how})'
    return mapping,evidence

def build(shop,online,aliases):
    stores,catalog,excluded=_old_build(shop,online,aliases)
    # Apply corrected variant parser; do not strip shop color if already separate.
    for it in stores:
        name=it['name'];c=it['color']
        if c and nrm(name).endswith(' '+nrm(c)):name=name[:-len(c)].strip(' ,;()-')
        it['attrs']=spec(name,aliases)
    for it in catalog:
        brand=it['attrs']['base'].split()[0] if it['attrs']['base'] else ''
        full,c=parse_catalog(brand,it['name'])
        it['attrs']=spec(full,aliases)
        it['color']=c;it['ck']=color_key(c,aliases)
    return stores,catalog,excluded

def build_palette(stores,catalog):
    shop_groups=defaultdict(list);online_groups=defaultdict(list)
    for s in stores:shop_groups[(category(s),s['attrs']['base'])].append(s)
    for x in catalog:online_groups[(category(x),x['attrs']['base'])].append(x)
    palettes={}
    for key,ss in shop_groups.items():
        palettes[key]=palette_resolution(ss,online_groups.get(key,[]))
    return palettes

def decide_v9(item,index,keys,model_colors,palettes):
    attrs=item['attrs'];key=(category(item),attrs['base'])
    same=[x for x in index.get(attrs['base'],[]) if category(x)==category(item)]
    possible=[x for x in same if same_variant(attrs,x['attrs'])]
    manual=model_colors.get((attrs['base'],item['ck']))
    mapping,evidence=palettes.get(key,({},{}))
    color=manual or mapping.get(item['ck'],item['ck'])
    exact=[x for x in possible if color and color==x['ck']]
    fixed=[x for x in possible if not x['ck'] and fixed_color_when_missing(item)]
    equivalents=exact or fixed
    if equivalents:
        safe=[];need=[]
        for c in equivalents:
            problems=variant_uncertainties(attrs,c['attrs'],category(item))
            if problems:need.append((c,problems))
            else:safe.append(c)
        col_reason=evidence.get(item['ck'],'точное совпадение цвета') if not manual else 'подтверждённое соответствие цвета из CSV'
        if safe:return ('Уже на Onliner',f'Совпали модель и модификация; {col_reason}',choose_candidates(safe),color,'автоматически' if not manual else 'справочник')
        if need:return ('Проверить','Совпали модель и цвет, но '+', '.join(need[0][1]),choose_candidates([v[0] for v in need]),color,'неполные характеристики')
    if possible:
        if not item['ck']:return ('Проверить','Не указан цвет сайта',choose_candidates(possible),color,'нет цвета')
        if any(not x['ck'] for x in possible):return ('Проверить','Onliner не указывает цвет',choose_candidates(possible),color,'нет цвета')
        if any(color_score(item['ck'],x['ck'])[0]>=50 for x in possible):
            return ('Проверить','Возможный синоним цвета или близкий оттенок; автоматическое соответствие не доказано',choose_candidates(possible),color,'неоднозначный цвет')
        return ('Можно добавить','Для этой модификации отсутствует указанный цвет',choose_candidates(possible),color,'новый цвет')
    if same:
        compatibles=[x for x in same if not variant_conflict(attrs,x['attrs'])]
        if compatibles:return ('Проверить','Модификация описана неполно или регион неявный',choose_candidates(compatibles),color,'неполная модификация')
        return ('Можно добавить','Другая явно различимая конфигурация памяти/SIM/связи/региона',choose_candidates(same),color,'новая модификация')
    close=[]
    for base in keys:
        if likely_same_model(attrs['base'],base):close.extend(x for x in index[base] if category(x)==category(item))
    if close:return ('Проверить','Возможно альтернативное название модели',choose_candidates(close),color,'похожая модель')
    return ('Можно добавить','Модель отсутствует в выгрузке Onliner',[],color,'новая модель')

def find_confirmation(item,catalog_by_id,confirmations):
    key=(nrm(item['name']),nrm(item['color']))
    pid=confirmations.get(key)
    target=catalog_by_id.get(pid) if pid else None
    if not target or category(item)!=category(target):return None
    a=item['attrs'];b=target['attrs']
    if a['base']!=b['base'] or variant_conflict(a,b):return None
    return target if not variant_uncertainties(a,b,category(item)) else None

def make_report(stores,catalog,excluded,model_colors,confirmations=None):
    index=defaultdict(list)
    for x in catalog:index[x['attrs']['base']].append(x)
    palettes=build_palette(stores,catalog)
    catalog_by_id={x['id']:x for x in catalog if x['id']}
    labels=['Категория магазина','Модель магазина','Цвет магазина','Цена, BYN','Причина','Варианты Onliner','productId Onliner','Строка магазина','Определённый цвет Onliner','Метод сопоставления','RAM сайта','Память сайта, GB','SIM сайта','Цвета кандидатов Onliner']
    add=[labels];review=[labels];matched=[labels];stats=Counter()
    for s in stores:
        status,reason,candidates,resolved,method=decide_v9(s,index,index.keys(),model_colors,palettes)
        confirmed=find_confirmation(s,catalog_by_id,confirmations or {})
        if confirmed:
            status,reason,candidates,resolved,method='Уже на Onliner','Ручное подтверждение по productId, модификация проверена',[confirmed],confirmed['ck'],'подтверждение'
        stats[status]+=1
        names='; '.join(dict.fromkeys(x['name'] for x in candidates))[:1200]
        ids='; '.join(dict.fromkeys(x['id'] for x in candidates))[:500]
        colors='; '.join(dict.fromkeys(x['color'] or '(не указан)' for x in candidates))[:500]
        row=[s['cat'],s['name'],s['color'],s['price'],reason,names,ids,s['row'],resolved,method,s['attrs']['ram'] or '',s['attrs']['storage'] or '',s['attrs']['sim'] or '',colors]
        (matched if status=='Уже на Onliner' else review if status=='Проверить' else add).append(row)
    diag=[['Раздел Onliner','Модель Onliner','Цвет','productId','Статус относительно выгрузки магазина','Модель магазина','Примечание']]
    byshop=defaultdict(list)
    for s in stores:byshop[(category(s),s['attrs']['base'])].append(s)
    diag_counts=Counter()
    for c in catalog:
        key=(category(c),c['attrs']['base']);mapping,_=palettes.get(key,({},{}))
        same=byshop.get(key,[])
        matches=[s for s in same if same_variant(s['attrs'],c['attrs']) and not variant_uncertainties(s['attrs'],c['attrs'],category(s)) and ((mapping.get(s['ck'],s['ck'])==c['ck'] and c['ck']) or (not c['ck'] and fixed_color_when_missing(s)))]
        st='Совпало' if matches else ('Проверить модель/цвет/модификацию' if same else 'Нет в ценовой выгрузке магазина')
        diag_counts[st]+=1
        diag.append([c['cat'],c['name'],c['color'],c['id'],st,'; '.join(s['name'] for s in (matches or same)[:3]),'Диагностика, не доказательство отсутствия на сайте'])
    summary=[['ПОКАЗАТЕЛЬ','ЗНАЧЕНИЕ'],['ПОЗИЦИИ МАГАЗИНА С ЦЕНОЙ',len(stores)],['ПОЗИЦИИ ONLINER',len(catalog)],['МОЖНО ДОБАВИТЬ',stats['Можно добавить']],['ПРОВЕРИТЬ ПЕРЕД ДОБАВЛЕНИЕМ',stats['Проверить']],['УЖЕ НА ONLINER',stats['Уже на Onliner']],['Строк без цены',excluded],['',''],['ПРИНЦИПЫ V9',''],['Цвет','Автоматически сопоставляется по цветовой палитре модели; при нескольких близких оттенках отправляется на проверку'],['SIM/связь','Явные различия SIM, Wi-Fi/Cellular и 4G/5G не объединяются'],['Память','RAM и накопитель сравниваются отдельно; при пропуске значимой характеристики требуется проверка'],['Неоднозначность','Сомнительные цвета, регионы и модификации не считаются подтверждёнными совпадениями'],['Ограничение','Без внешнего каталога производителя невозможно гарантированно распознать произвольные маркетинговые названия цветов'],['Исходные данные','Сверка сделана по выбранным XLSX, справочники не обязательны']]
    color_audit=[['Категория','Модель (нормализованная)','Цвет магазина','Цвет Onliner (авто)','Результат определения','Цветовая палитра Onliner']]
    for key,(color_map,evidences) in sorted(palettes.items()):
        site_colors=sorted({x['ck'] for x in byshop.get(key,[]) if x['ck']})
        online_colors=sorted({x['ck'] for x in index.get(key[1],[]) if category(x)==key[0] and x['ck']})
        for original in site_colors:
            dest=color_map.get(original,'')
            color_audit.append([key[0],key[1],original,dest,evidences.get(original,'Не найдено однозначное соответствие' if online_colors else 'Модель отсутствует на Onliner'),'; '.join(online_colors)[:1000]])
    return [('Сводка',summary),('Можно добавить',add),('Проверить',review),('Уже на Onliner',matched),('Контроль Onliner',diag),('Автоцвета',color_audit)],stats,diag_counts



# === v10: source-aware iPhone SIM defaults; guarded color refinements ===
_v9_build = build
_v9_build_palette = build_palette
_v9_make_report = make_report


def normalize_iphone_onliner_sim(item):
    """Only for Onliner iPhones: omitted SIM type means one physical SIM.

    It must NOT apply to other phone brands or a missing SIM type on the shop side.
    A stated Dual eSIM / eSIM is already parsed as 'esim' by spec().
    """
    attrs = item['attrs']
    attrs['sim_inferred'] = False
    if attrs['base'].startswith('apple iphone ') and attrs.get('sim') is None:
        attrs['sim'] = 'physical'
        attrs['sim_inferred'] = True
    return item


def build(shop, online, aliases):
    stores, catalog, excluded = _v9_build(shop, online, aliases)
    for entry in catalog:
        normalize_iphone_onliner_sim(entry)
    return stores, catalog, excluded


def build_palette(stores, catalog):
    palettes = _v9_build_palette(stores, catalog)
    # The A57 has two separately offered blue shades on the store side.
    # This rule preserves light-blue (голубой) and maps dark-blue (темно-синий)
    # exclusively to Onliner's blue, if there isn't another competing shop blue.
    key = ('smartphone', 'samsung galaxy a57')
    if key in palettes:
        ss = [x for x in stores if (category(x), x['attrs']['base']) == key]
        cc = [x for x in catalog if (category(x), x['attrs']['base']) == key]
        shop_colors = {x['ck'] for x in ss}
        online_colors = {x['ck'] for x in cc}
        mapping, evidence = palettes[key]
        if ('темно синий' in shop_colors and 'синий' in online_colors
                and 'синий' not in shop_colors
                and not any(v == 'синий' and k != 'темно синий' for k,v in mapping.items())):
            mapping['темно синий'] = 'синий'
            evidence['темно синий'] = ('контекст модели Galaxy A57: '
                                        'темно-синий → синий, голубой отдельно')
    return palettes


def make_report(stores, catalog, excluded, model_colors, confirmations=None):
    sheets, stats, diagnostics = _v9_make_report(stores,catalog,excluded,model_colors,confirmations)
    by_id = {x['id']:x for x in catalog if x['id']}
    # Make the assumed physical SIM visible for auditing, not silently inferred.
    for title, rows in sheets:
        if title in ('Можно добавить','Проверить','Уже на Onliner'):
            rows[0].extend(['SIM кандидатов Onliner','Правило SIM Onliner'])
            for r in rows[1:]:
                matched = [by_id[pid.strip()] for pid in str(r[6]).split(';') if pid.strip() in by_id]
                sims = '; '.join(dict.fromkeys(x['attrs'].get('sim') or 'не указана' for x in matched))
                rules = ('iPhone без SIM в Onliner → 1 физическая SIM'
                         if any(x['attrs'].get('sim_inferred') for x in matched) else '')
                r.extend([sims, rules])
        if title=='Контроль Onliner':
            rows[0].extend(['SIM Onliner','Тип SIM определён автоматически'])
            for r in rows[1:]:
                x=by_id.get(str(r[3]))
                r.extend([x['attrs'].get('sim') or '' if x else '',
                          'Да: iPhone без обозначения SIM' if x and x['attrs'].get('sim_inferred') else ''])
        if title=='Сводка':
            rows.append(['ПРАВИЛА V10',''])
            rows.append(['iPhone в Onliner без SIM','Означает одну физическую SIM; nano-SIM в магазине совпадает; eSIM и Dual eSIM совпадают между собой, но не с физической SIM'])
            rows.append(['Samsung Galaxy A57','Тёмно-синий магазина соответствует синему Onliner, но голубой остаётся самостоятельным цветом'])
            rows.append(['Границы автоправил','Необозначенный тип SIM других брендов и необозначенный тип SIM в магазине не подменяются физической SIM'])
    return sheets,stats,diagnostics



# === v11: source-aware SIM, connectivity, regions, and omitted-color semantics ===
VERSION = 'v11'
_v10_spec = spec
_v10_build = build
_v10_palette = build_palette
_v10_report = make_report
_v10_sim_normalize = normalize_iphone_onliner_sim
_v10_likely_model = likely_same_model
_v10_decide = decide_v9
_v10_variant_conflict = variant_conflict

FIXED_RAM_FAMILIES = {
    'google pixel 10', 'google pixel 10 pro', 'google pixel 10 pro xl',
    'samsung galaxy s25 fe',
}


def spec(name, aliases):
    attrs = _v10_spec(name, aliases)
    s = nrm(name)
    # China remains a separate product. All other region labels, including India,
    # are normalized to international for the purposes of this shop's matching policy.
    if attrs['region'] != 'china':
        attrs['region'] = 'global'
    attrs['region_explicit'] = attrs['region'] == 'china'
    # A separate 4G/5G radio token is not a distinction for Galaxy A17 listings.
    if attrs['base'] == 'samsung galaxy a17':
        attrs['radio'] = None
    return attrs


def normalize_v11_source(item, source):
    attrs = item['attrs']
    base = attrs['base']
    attrs['sim_inferred'] = False
    attrs['connectivity_inferred'] = False
    if base == 'apple iphone air':
        # This device family is eSIM-only regardless of whether the label says so.
        if attrs.get('sim') is None:
            attrs['sim'] = 'esim'
            attrs['sim_inferred'] = True
    elif base.startswith('apple iphone ') and attrs.get('sim') is None:
        # Source-specific catalog convention provided by the store owner.
        attrs['sim'] = 'physical'
        attrs['sim_inferred'] = True
    if category(item) == 'tablet' and attrs.get('connectivity') is None:
        # An unspecified tablet connection is the Wi-Fi-only catalog variant.
        attrs['connectivity'] = 'wifi'
        attrs['connectivity_inferred'] = True
    return item


def build(shop, online, aliases):
    stores, catalog, excluded = _v10_build(shop, online, aliases)
    # Reparse from original names to apply version 11 rules to all source records;
    # the v10 parser had already set iPhone catalog defaults.
    for it in stores:
        name = it['name']; c=it['color']
        if c and nrm(name).endswith(' '+nrm(c)):
            name=name[:-len(c)].strip(' ,;()-')
        it['attrs'] = spec(name, aliases)
        normalize_v11_source(it, 'shop')
    for it in catalog:
        # Previous-stage 'base' has already been built with the correct brand prefix.
        brand=it['attrs']['base'].split()[0] if it['attrs']['base'] else ''
        fullname, color=parse_catalog(brand, it['name'])
        it['attrs']=spec(fullname, aliases)
        it['color']=color
        it['ck']=color_key(color,aliases)
        normalize_v11_source(it, 'onliner')
    # Distinguish unspecified RAM in an unambiguous fixed-RAM family only when
    # every catalog entry for that model gives exactly one RAM figure.
    by_base=defaultdict(set)
    for it in catalog:
        if it['attrs']['ram'] is not None:
            by_base[it['attrs']['base']].add(it['attrs']['ram'])
    for it in stores:
        attrs=it['attrs']
        base=attrs['base']
        if (base in FIXED_RAM_FAMILIES and attrs['ram'] is None
                and len(by_base[base])==1):
            attrs['ram']=next(iter(by_base[base]))
            attrs['ram_inferred']=True
    return stores,catalog,excluded


def likely_same_model(a,b):
    # Distinct marketed products, not an alternative spacing/brand spelling.
    if {a,b} == {'realme gt7','realme gt7 t'}:
        return False
    return _v10_likely_model(a,b)


def same_variant(a,b):
    if a['base']!=b['base'] or a['region']!=b['region']:
        return False
    for k in ('ram','storage','sim','connectivity','anc'):
        if a.get(k) is not None and b.get(k) is not None and a[k]!=b[k]:
            return False
    # Radio is informative for smartphones other than Galaxy A17; for tablets,
    # connectivity is the meaningful discriminator instead.
    if a['base']!='samsung galaxy a17' and not ('ipad' in a['base'] or 'ipad' in b['base']):
        if a.get('radio') and b.get('radio') and a['radio']!=b['radio']:
            return False
    return True


def variant_conflict(a,b):
    if a['region']!=b['region']:
        return True
    return not same_variant(a,b)


def variant_uncertainties(a,b,category_name):
    problems=[]
    for k in ('ram','storage','sim','connectivity','anc'):
        if (a.get(k) is None)!=(b.get(k) is None):
            if k in ('ram','storage') and category_name not in ('smartphone','tablet','laptop'):
                continue
            problems.append({'ram':'RAM','storage':'объём памяти','sim':'тип SIM',
                             'connectivity':'Wi-Fi/Cellular','anc':'шумоподавление'}[k]
                            +' отсутствует в одном источнике')
    # No radio warning for A17 or iPad; iPad connection is checked above.
    if a['base']!='samsung galaxy a17' and 'ipad' not in a['base']:
        if (a.get('radio') is None)!=(b.get('radio') is None):
            problems.append('4G/5G отсутствует в одном источнике')
    return problems


def build_palette(stores,catalog):
    palettes=_v10_palette(stores,catalog)
    key=('smartphone','honor magic8 pro')
    if key in palettes:
        sc={x['ck'] for x in stores if (category(x),x['attrs']['base'])==key}
        oc={x['ck'] for x in catalog if (category(x),x['attrs']['base'])==key}
        mapping,evidence=palettes[key]
        # Marketing colors used in this same product family; never apply globally.
        candidates={'золотой рассвет':'золотистый','небесная лазурь':'голубой',
                    'черный бархат':'черный'}
        for src,dst in candidates.items():
            if src in sc and dst in oc and not any(k!=src and v==dst for k,v in mapping.items()):
                mapping[src]=dst
                evidence[src]=f'цвет заводской палитры Magic8 Pro: {src} → {dst}'
    return palettes


def decide_v9(item,index,keys,model_colors,palettes):
    # Missing color in the Onliner catalog means that its product card does not
    # differentiate the color. Retain matching of all other variant attributes.
    attrs=item['attrs'];key=(category(item),attrs['base'])
    same=[x for x in index.get(attrs['base'],[]) if category(x)==category(item)]
    possible=[x for x in same if same_variant(attrs,x['attrs'])]
    no_color=[x for x in possible if not x['ck']]
    if no_color:
        definite=[x for x in no_color if not variant_uncertainties(attrs,x['attrs'],category(item))]
        if definite:
            return ('Уже на Onliner','Onliner не разделяет эту модификацию по цветам; модель и остальные характеристики совпали',
                    choose_candidates(definite),'(цвет на Onliner не задан)','цвет не учитывается')
        return ('Проверить','На Onliner цвет отсутствует, но другие характеристики описаны неполно',
                choose_candidates(no_color),'','неполная модификация')
    # Fall back to the established model-aware palette algorithm.
    return _v10_decide(item,index,keys,model_colors,palettes)


def make_report(stores,catalog,excluded,model_colors,confirmations=None):
    sheets,stats,diag=_v10_report(stores,catalog,excluded,model_colors,confirmations)
    by_id={x['id']:x for x in catalog if x['id']}
    by_row={x['row']:x for x in stores}
    for title,rows in sheets:
        if title in ('Можно добавить','Проверить','Уже на Onliner'):
            rows[0].extend(['Обоснование v11','Регион сайта','Связь сайта', 'Связь Onliner', 'RAM определена автоматически'])
            for r in rows[1:]:
                shop_item=by_row.get(r[7]); ids=[p.strip() for p in str(r[6]).split(';')]
                candidates=[by_id[p] for p in ids if p in by_id]
                notes=[]
                if candidates and any(x['attrs'].get('sim_inferred') for x in candidates):
                    if any(x['attrs']['base']=='apple iphone air' for x in candidates):
                        r[15]='iPhone Air без SIM в Onliner → eSIM'
                    else:
                        r[15]='iPhone без SIM в Onliner → физическая SIM'
                if shop_item:
                    a=shop_item['attrs']
                    if a.get('ram_inferred'): notes.append(f'RAM {a["ram"]}GB: одна конфигурация RAM для модели в каталоге')
                    if a.get('sim_inferred'): notes.append('SIM по правилу семейства iPhone')
                    if a.get('connectivity_inferred'): notes.append('Планшет без маркировки сети → Wi-Fi')
                    if a['base']=='samsung galaxy a17':notes.append('4G/5G для A17 игнорируется')
                if any(x['attrs'].get('sim_inferred') for x in candidates):notes.append('SIM Onliner определена по правилу iPhone')
                r.extend(['; '.join(notes), shop_item['attrs']['region'] if shop_item else '',
                          shop_item['attrs'].get('connectivity') or '' if shop_item else '',
                          '; '.join(dict.fromkeys(c['attrs'].get('connectivity') or 'не указана' for c in candidates)),
                          'Да' if shop_item and shop_item['attrs'].get('ram_inferred') else ''])
        if title=='Сводка':
            del rows[8:]  # Replace obsolete v9/v10 descriptions with authoritative v11 rules.
            rows.extend([
                ['ПРАВИЛА V11',''],
                ['Регионы','Китайская версия отдельно; индийская, европейская, глобальная и неуказанная считаются международными'],
                ['iPhone 17e','Если SIM не указана в любой выгрузке — физическая SIM; eSIM/Dual eSIM отдельно'],
                ['iPhone Air','SIM не указана → eSIM, поскольку семейство eSIM-only'],
                ['Honor Magic8 Pro','Золотой рассвет = золотистый; небесная лазурь = голубой; чёрный бархат = чёрный'],
                ['Samsung Galaxy A17','4G/5G не влияет на сопоставление данной модели'],
                ['Pixel 10 / S25 FE','Отсутствующая RAM заполняется только при единственном RAM в карточках Onliner той же модели'],
                ['iPad / планшеты','Без обозначения Cellular/5G в каталоге = Wi-Fi; 5G модели магазина не равны Wi-Fi Onliner'],
                ['Цвет не указан на Onliner','Игнорируется при сопоставлении, если модель и модификация совпадают'],
                ['Realme GT7','GT7 и GT7 T считаются разными моделями'],
                ['Ограничения','Утверждения о SIM, связи и цветах основаны на согласованных правилах каталога, а не проверке спецификаций производителей'],
            ])
    return sheets,stats,diag


# === v12: strictly model-scoped factory-color synonyms ===
# Do not turn these into global color aliases: different models can use
# the same marketing words for entirely different factory colors.
VERSION = 'v12'
_v11_build_palette = build_palette
_v11_make_report = make_report
MODEL_COLOR_SYNONYMS_V12 = {
    ('smartphone', 'apple iphone duo'): {
        'серебристый': 'звездный белый',
        'темно синий': 'ночное небо',
    },
    ('smartphone', 'honor 600'): {
        'белое золото': 'бежевый',
    },
}


def build_palette(stores, catalog):
    palettes = _v11_build_palette(stores, catalog)
    for key, synonyms in MODEL_COLOR_SYNONYMS_V12.items():
        if key not in palettes:
            continue
        shop_colors = {x['ck'] for x in stores if (category(x), x['attrs']['base']) == key}
        online_colors = {x['ck'] for x in catalog if (category(x), x['attrs']['base']) == key}
        mapping, evidence = palettes[key]
        for source, target in synonyms.items():
            # Require both colors to be present in the current model palette,
            # and preserve one-to-one correspondences within a model.
            if source not in shop_colors or target not in online_colors:
                continue
            if source in mapping and mapping[source] != target:
                continue
            if any(other != source and result == target for other, result in mapping.items()):
                continue
            if target in shop_colors and target != source:
                # Shop lists target as a separate factory color: do not collapse.
                continue
            mapping[source] = target
            evidence[source] = f'проверенный синоним цвета для модели {key[1]}: {source} → {target}'
    return palettes


def make_report(stores, catalog, excluded, model_colors, confirmations=None):
    sheets, stats, diag = _v11_make_report(stores, catalog, excluded, model_colors, confirmations)
    for title, rows in sheets:
        if title == 'Сводка':
            rows.extend([
                ['ПРАВИЛА V12', ''],
                ['iPhone Duo', 'Серебристый = звездный белый; темно-синий = ночное небо (только эта модель)'],
                ['HONOR 600', 'Белое золото = бежевый (только HONOR 600, не Pro и не Lite)'],
                ['Ограничение', 'Совпадения проверены по предоставленной XLSX-выгрузке Onliner; актуальная публичная витрина может отличаться'],
            ])
    # Compare against v11's original palette before listing true status changes.
    byrow = {entry["row"]: entry for entry in stores}
    baseline_palettes = _v11_build_palette(stores, catalog)
    index = defaultdict(list)
    for entry in catalog:
        index[entry['attrs']['base']].append(entry)
    changed = [['Модель сайта', 'Цвет сайта', 'Статус v11', 'Статус v12',
                'Цвет Onliner', 'productId Onliner', 'Причина', 'Строка магазина']]
    for sheetname, rows in sheets:
        if sheetname not in ('Уже на Onliner', 'Можно добавить', 'Проверить'):
            continue
        for r in rows[1:]:
            it = byrow.get(r[7])
            if not it or (category(it), it['attrs']['base']) not in MODEL_COLOR_SYNONYMS_V12:
                continue
            if it['ck'] not in MODEL_COLOR_SYNONYMS_V12[(category(it), it['attrs']['base'])]:
                continue
            old_status = decide_v9(it, index, index.keys(), model_colors, baseline_palettes)[0]
            if old_status == sheetname:
                continue
            changed.append([r[1], r[2], old_status, sheetname,
                            r[8], r[6], r[4], r[7]])
    sheets.append(('Контроль цветов v12', changed))
    return sheets, stats, diag


if __name__ == "__main__":
    main()
