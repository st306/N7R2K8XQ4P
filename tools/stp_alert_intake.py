#!/usr/bin/env python3
# STP alert intake parser v1 (25 Oct 2026). Rules: REUSE_LIST_INTAKE_25-10-2026 (numbers in brackets).
# Parse:   python3 -I intake.py --cursor START --sheet FILE --live FILE [--manual FILE] --out FILE THREADFILES...
# Prepare: python3 -I intake.py --prepare --out FILE --keys FILE      (drop Keys written since the parse, set chunks, record first row)
# Reader:  python3 -I intake.py --reader-build --out FILE PAGEFILES...   |   --reader-manual --reader FILE --cursor START --manual FILE --out X
# Check:   python3 -I intake.py --check --out FILE --readback FILE --first ROW [--pmi FILE] [--start START]
import json, re, html, sys, argparse, datetime, os

SENDERS = {'property@alert.rightmove.co.uk': 'rightmove', 'propertyalerts@comms-2.zoopla.co.uk': 'zoopla',
           'property-alerts@mail.onthemarket.com': 'onthemarket', 'no-reply@onthemarket-mail.com': 'onthemarket'}
SHORT = {'rightmove': 'RM', 'zoopla': 'ZO', 'onthemarket': 'OTM'}
LINK = {'rightmove': 'https://www.rightmove.co.uk/properties/{}',
        'zoopla': 'https://www.zoopla.co.uk/for-sale/details/{}/',
        'onthemarket': 'https://www.onthemarket.com/details/{}/'}
EXCL_ANY = re.compile(r'\b(caravan|park home|mobile home|static home|holiday lodge|lodge|chalet|holiday home)\b', re.I)
EXCL = re.compile(r'\b(land|plot|plots|site|garage|parking space|commercial|retail|office|shop|caravan|park home|mobile home|'
                  r'static home|holiday lodge|lodge|chalet|holiday home)\b', re.I)  # (20)(21) type words only
ROADWORDS = {'street', 'st', 'road', 'rd', 'avenue', 'ave', 'drive', 'dr', 'crescent', 'cres', 'place', 'pl', 'court', 'ct',
             'gardens', 'gdns', 'terrace', 'lane', 'way', 'view', 'grove', 'park', 'square', 'close', 'wynd', 'brae', 'row',
             'quadrant', 'circle', 'walk', 'loan', 'gate', 'north', 'south', 'east', 'west', 'the', 'flat', 'and', 'of',
             'upper', 'lower', 'new', 'old', 'scotland', 'glasgow', 'lanarkshire', 'ayrshire', 'renfrewshire', 'dunbartonshire',
             'lothian', 'fife', 'falkirk', 'stirling', 'edinburgh', 'paisley', 'motherwell', 'hamilton'}


def utc(s):
    if not s:
        return None
    s = s.strip().replace('Z', '+00:00')
    d = datetime.datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=datetime.timezone.utc)


def to_text(f):
    f = re.sub(r'(?is)<style.*?</style>', '', f)
    f = re.sub(r'(?is)<!--.*?-->', '', f)
    t = re.sub(r'<[^>]+>', ' | ', f)
    t = html.unescape(t)
    t = re.sub('[\u200c\u00a0\u034f\u200b]', ' ', t)
    t = re.sub(r'(\s*\|\s*)+', ' | ', t)
    return re.sub(r'\s+', ' ', t).strip()


def fields(seg):
    o = {}
    m = re.search(r'£\s?([\d,]{4,})', seg)
    o['price'] = int(m.group(1).replace(',', '')) if m else None
    q = re.search(r'(Offers over|Offers in excess of|Fixed price|Offers around|Guide price|Offers in the region of)', seg, re.I)
    o['qual'] = q.group(1) if q else ''
    b = re.search(r'(\d+)\s*(?:bed|bedroom)', seg, re.I)
    o['beds'] = int(b.group(1)) if b else None
    ty = re.search(r'\d+\s*(?:bed|bedroom)s?\s+([a-z\- ]+?)\s+for sale', seg, re.I)
    o['type'] = ty.group(1).strip().lower() if ty else ''
    parts = [x.strip() for x in seg.split(' | ')]
    addr = None
    for i, p in enumerate(parts):
        if re.search(r'for sale$', p, re.I) and i + 1 < len(parts):
            addr = parts[i + 1]; break
    if addr is None:
        for i, p in enumerate(parts):
            if re.match(r'\d+ beds?\b', p) and i + 1 < len(parts):
                addr = parts[i + 1]; break
    if addr and (addr.startswith('<') or 'http' in addr or '£' in addr):
        addr = None
    o['address'] = addr or ''
    o['title'] = ' '.join(parts[:12])[:400]
    o['reduced'] = bool(re.search(r'\bReduced\b', seg[:300]))
    c = re.search(r'Reduced by\s*£\s?([\d,]{3,})', seg[:300], re.I)
    o['cut'] = int(c.group(1).replace(',', '')) if c else None
    a = re.search(r'Added on (\d{2}/\d{2}/\d{4})', seg)
    o['added_on'] = a.group(1) if a else ''
    return o


def excluded(l, portal):  # (20)(21) type only; a bed count means a dwelling
    t = l.get('type') or ''
    if re.match(r'\s*["\u201c]', l.get('address') or ''):  # developer house type, e.g. "The Ardbeg" at ...: new-build plot
        return True
    if l.get('newbuild') or (portal == 'rightmove' and len(str(l['id'])) >= 13) or EXCL_ANY.search(t):
        return True
    if l.get('beds') is None and (EXCL.search(t) or (l.get('price') is not None and l['price'] < 25000)):
        return True
    return False


def outcode(addr):
    m = re.search(r'\b([A-Z]{1,2}\d{1,2}[A-Z]?)(?:\s*\d[A-Z]{2})?\s*$', (addr or '').strip().rstrip(','))
    return m.group(1) if m else None


def photo_for(portal, pid, h, seg_html):  # (27) property image only, never a logo or icon
    if portal == 'rightmove':
        m = re.search(r'(https://media\.rightmove\.co\.uk/[^"\'\s<>]*?/%s/[^"\'\s<>]+?\.(?:jpe?g|png))' % pid, h)
    elif portal == 'zoopla':
        m = re.search(r'(https://lid\.zoocdn\.com/[^"\'\s<>]+?\.jpe?g)', seg_html)
    else:
        m = re.search(r'(https?://media\.onthemarket\.com/properties/%s/[^"\'\s<>]+?\.jpe?g)' % pid, h)
    return m.group(1).replace('http://', 'https://') if m else ''


def html_listings(portal, h):
    if portal == 'rightmove':
        pat = re.compile(r'(?:www\.rightmove\.co\.uk/properties/|property-photo/[^/"]+/)(\d{6,})')
        cut = len(h)
    else:
        pat = re.compile(r'(for-sale|new-homes)/details/(\d+)')
        i = h.find('Similar properties')
        cut = i if i >= 0 else len(h)  # (4)
    order, pos, nb = [], {}, set()
    for m in pat.finditer(h):
        pid = m.group(m.lastindex)
        if m.lastindex == 2 and m.group(1) == 'new-homes':
            nb.add(pid)
        if pid not in pos:
            pos[pid] = m.start(); order.append(pid)
    res = []
    for k, pid in enumerate(order):
        st = pos[pid]
        en = pos[order[k + 1]] if k + 1 < len(order) else min(len(h), st + 40000)
        f = fields(to_text(h[st:en]))
        f.update(id=pid, before_cut=st < cut, newbuild=pid in nb, photo=photo_for(portal, pid, h, h[st:en]))
        res.append(f)
    return res


OTM_RE = re.compile(r'£([\d,]+)[ \t]*([A-Za-z ]*?)[ \t]*\n(?:(\d+) bedroom )?(.+?) for sale - (.+?)\nhttps://www\.onthemarket\.com/details/(\d+)/')


def otm_listings(plain):
    res, seen = [], set()
    for m in OTM_RE.finditer(plain):
        pid = m.group(6)
        if pid in seen:
            continue
        seen.add(pid)
        res.append(dict(id=pid, price=int(m.group(1).replace(',', '')), qual=m.group(2).strip(),
                        beds=int(m.group(3)) if m.group(3) else None, type=m.group(4).strip().lower(),
                        address=m.group(5).strip(), title=m.group(4), before_cut=True, newbuild=False, reduced=False, added_on=''))
    for pid in dict.fromkeys(re.findall(r'onthemarket\.com/details/(\d+)/', plain)):
        if pid not in seen:
            seen.add(pid)
            res.append(dict(id=pid, price=None, qual='', beds=None, type='', address='(alert not read, open link)', title='',
                            before_cut=True, newbuild=False, reduced=False, added_on='', linkonly=True))
    return res, len(seen)


def headline(portal, subj, txt):
    if portal == 'rightmove':
        m = re.search(r'found you (\d+) propert', subj)
        return int(m.group(1)) if m else None
    if portal == 'zoopla':
        m = re.search(r'(\d+) (?:new )?propert', subj) or re.search(r'We have (\d+) new propert', txt)
        return int(m.group(1)) if m else None
    return None


def search_name(portal, subj, txt, plain):
    if portal == 'rightmove':
        m = re.search(r'(?:from|up to) £[\d,]+(?: to £[\d,]+)?', txt)
        return 'RM ' + (m.group(0).rstrip(', ') if m else '?')
    if portal == 'zoopla':
        m = re.search(r'for sale in (.*)$', subj) or re.search(r'here for (.*?)!', subj)
        return 'ZO ' + (m.group(1) if m else subj)[:50]
    m = re.search(r'\n\n(1\+ bedroom.*?)\n', plain or '')
    return 'OTM ' + (m.group(1) if m else subj)[:50]


def street_word(addr):
    a = re.sub(r'\b[A-Z]{1,2}\d{1,2}[A-Z]?(?:\s*\d[A-Z]{2})?\b', ' ', addr or '')
    words = [w.lower() for w in re.findall(r'[A-Za-z]{3,}', a)]
    words = [w for w in words if w not in ROADWORDS]
    return words[0] if words else None


def fullpc(addr):
    m = re.search(r'\b([A-Z]{1,2}\d{1,2}[A-Z]?\s*\d[A-Z]{2})\s*$', (addr or '').strip().rstrip(','))
    return m.group(1).replace(' ', '') if m else None


def house_no(addr):
    m = re.match(r'\s*(?:flat\s+\S+,?\s*)?(?:[A-Za-z][A-Za-z ]+?,\s*)?(\d+[a-z]?)\b', addr or '', re.I)
    return m.group(1).lower() if m else None


def twin(a, b):  # (16)(17)
    if a['portal'] == b['portal']:
        return 'no'
    if not a['price'] or not b['price'] or abs(a['price'] - b['price']) > 5000:
        return 'no'
    if a['beds'] is not None and b['beds'] is not None and a['beds'] != b['beds']:
        return 'no'
    sa, sb = street_word(a['address']), street_word(b['address'])
    if (not sa or not sb) and 'reader' in (a.get('src'), b.get('src')):  # v1.4: flag only, never merge
        oa, ob = outcode(a['address']), outcode(b['address'])
        if oa and ob and oa != ob:
            return 'no'
        if not (a['address'] or '').strip() or not (b['address'] or '').strip():
            return 'maybe' if a['price'] == b['price'] else 'no'
        return 'maybe'
    if not sa or sa != sb:
        return 'no'
    ha, hb = house_no(a['address']), house_no(b['address'])
    if ha != hb:
        return 'no'
    pa, pb = fullpc(a['address']), fullpc(b['address'])
    if pa and pb and pa != pb:
        return 'no'
    if ha is None:
        return 'maybe'  # unnumbered lookalikes: flag, never merge
    oa, ob = outcode(a['address']), outcode(b['address'])
    if oa and ob:
        return 'yes' if oa == ob else 'no'
    return 'maybe'  # (18) keep and flag


def sheet_values(path):
    if not path:
        return []
    raw = open(path).read().strip()
    if not raw:
        return []
    d = json.loads(raw)
    if isinstance(d, list):
        return d
    def find(o):
        if isinstance(o, dict):
            if 'values' in o and isinstance(o['values'], list):
                return o['values']
            for v in o.values():
                r = find(v)
                if r is not None:
                    return r
        if isinstance(o, list):
            for v in o:
                r = find(v)
                if r is not None:
                    return r
        return None
    return find(d) or []


def to_int(v):
    try:
        return int(round(float(re.sub(r'[^\d.]', '', str(v)))))
    except Exception:
        return None


def pmi_listings(path):
    raw = open(path).read()
    try:
        d = json.loads(raw)
        if isinstance(d, dict) and 'result' in d:
            raw = d['result']
        elif isinstance(d, dict) and 'listings' in d:
            return d['listings']
    except Exception:
        pass
    m = re.search(r'\{.*\}', raw, re.S)
    return json.loads(m.group(0)).get('listings', []) if m else []


def norm(v):
    if v is None:
        return ''
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


def prepare(a):
    # just before appending: drop Keys that reached the sheet since the parse (rule 15), chunk by 50, record the first row
    res = json.load(open(a.out))
    now = [str(r[0]).strip() for r in sheet_values(a.keys) if r and r[0]]
    have = set(now)
    rows = [r for r in res['rows'] if r[0] not in have]
    res['dropped_late'] = len(res['rows']) - len(rows)
    res['rows'] = rows
    res['counts']['new'] = len(rows)
    by = {}
    for r in rows:
        p = SHORT[r[0].split(':')[0]]; by[p] = by.get(p, 0) + 1
    res['by_portal'] = by
    res['chunks'] = [rows[i:i + 50] for i in range(0, len(rows), 50)]
    json.dump(res, open(a.out, 'w'), ensure_ascii=False)
    print(json.dumps(dict(rows=len(rows), dropped_late=res['dropped_late'], chunks=len(res['chunks']))))


def check(a):
    # after the appends: read-back 'Buy list'!A2:K (UNFORMATTED_VALUE). Every appended cell must equal out.json, by position.
    res = json.load(open(a.out))
    back = sheet_values(a.readback)
    first = int(a.first)  # sheet row of the first appended row, from the first append's updates.updatedRange
    got = back[first - 2:]
    want = res['rows']
    fixes, junk = [], []
    cols = 'ABCDEFGHIJK'
    for n, w in enumerate(want):
        rown = first + n
        g = got[n] if n < len(got) else None
        if g is None:
            fixes.append(dict(row=rown, range=f"'Buy list'!A{rown}:K{rown}", values=[w], why='missing'))
            continue
        bad = [c for c in range(11) if norm(w[c]) != norm(g[c] if c < len(g) else '')]
        if bad:
            fixes.append(dict(row=rown, range=f"'Buy list'!A{rown}:K{rown}", values=[w], why='cells ' + ''.join(cols[c] for c in bad)))
    for n in range(len(want), len(got)):
        if got[n] and got[n][0]:
            junk.append(f"row {first + n}: {got[n][0]}")
    keys = {}
    for i, r in enumerate(back):
        if r and r[0]:
            keys.setdefault(str(r[0]).strip(), []).append(i + 2)
    dups = {k: v for k, v in keys.items() if len(v) > 1}
    prices = []
    for u in res.get('price_updates', []):
        rr = keys.get(u['key'])
        if rr and len(rr) == 1:
            prices.append(dict(range=f"'Buy list'!D{rr[0]}", values=[[u['price']]]))
            prices.append(dict(range=f"'Buy list'!G{rr[0]}", values=[[f"was £{u['was']:,}"]]))
    refuse = len(fixes) > 3
    cap = dict(checked=0, found=0, missed=[], run=bool(a.pmi))
    live_keys = {str(r[0]).strip() for r in sheet_values(a.live) if r and r[0]} if a.live else set()
    if a.pmi:
      try:
          start = utc(a.start) if a.start else None
          edge = utc(res['cursor_new']) - datetime.timedelta(hours=2)
          rows = [dict(portal='any', address=str(r[2]) if len(r) > 2 else '', price=to_int(r[3]) if len(r) > 3 else None)
                  for r in back if r and r[0]]
          n = 0
          for l in pmi_listings(a.pmi):
              pc = (l.get('postcode') or '').upper()
              if not re.match(r'(G|PA|ML|KA|FK|KY|DD|PH|DG|TD|EH)\d', pc) or re.match(r'EH([1-9]|1[0-7])\s', pc):
                  continue
              if not l.get('created_at'):
                  continue
              c0 = utc(l['created_at'])
              if c0 > edge or (start and c0 < start) or l.get('status') != 'Available':
                  continue
              if not l.get('bedrooms'):
                  continue
              n += 1
              k = f"rightmove:{l['id']}"
              sw = street_word(l.get('address', ''))
              ok = k in keys or any(sw and street_word(o['address']) == sw and o['price'] and l.get('price') and
                                    abs(o['price'] - l['price']) <= 5000 for o in rows)
              cap['found'] += ok
              if not ok:
                  cap['missed'].append(f"{l.get('address')}, {pc}, £{l.get('price')}: {l.get('url')}")
                  if k not in keys and k not in live_keys and (l.get('price') or 0) <= 260000:  # (27 Oct 2026, Stu "1"): put it on the Buy list for Stu
                      ad = str(l.get('address', '')); ad = ad if pc and pc.split()[0] in ad.upper() else f"{ad}, {pc}".strip(', ')
                      fl = ['RM | missed by alerts (PMI check)']
                      tg = ' '.join(map(str, l.get('tags') or []))
                      if re.search(r'over_60s|retire|shared|new', tg, re.I):
                          fl.append('outside alert filters?')
                      if re.match(r'EH([1-9]|1[0-7])\s', pc):
                          fl.append('Edinburgh, parked')
                      cap.setdefault('rows', []).append([k, datetime.date.today().isoformat(), ad[:90], l.get('price') or '', l.get('bedrooms') or '',
                                                         '', '', LINK['rightmove'].format(l['id']), '', ' | '.join(fl), l.get('cover_image') or ''])
              if n == 10:
                  break
          cap['checked'] = n
      except Exception as e:
        cap = dict(checked=0, found=0, missed=[], run=False, error=str(e)[:200])
    out = dict(fixes=[] if refuse else fixes, refused_fixes=len(fixes) if refuse else 0, junk_rows=junk, duplicate_keys=dups,
               capture=cap, price_data=prices, miss_rows=cap.get('rows', []))
    json.dump(out, open(a.out.replace('.json', '_check.json'), 'w'), ensure_ascii=False)
    print(json.dumps(dict(fixes=0 if refuse else len(fixes), refused_fixes=len(fixes) if refuse else 0, junk=len(junk),
                          duplicates=len(dups), price_cells=len(prices),
                          capture=(f"{cap['found']} of {cap['checked']}" if cap['run'] else 'not run'),
                          capture_missed=cap['missed'][:5], miss_rows=len(cap.get('rows', [])))))

RM_IMG = 'https://media.rightmove.co.uk/dir/crop/10:9-16:9/property-photo/'
ZO_IMG = 'https://lid.zoocdn.com/'


def body_check(lines):  # same as the page script: sum of character codes of the body lines, mod 1e9
    return sum(ord(ch) for l in lines for ch in l) % 1000000000


def reader_build(a):
    # 05:00 reader: page-text files (STPREADER header + id|price|beds|type|address|photo|reduced lines) -> rows for the Reader tab.
    rows, bad, seen, pages = [], [], set(), {'RM': 0, 'ZO': 0}
    if not a.files:
        bad.append('no page files')
    for f in a.files:
        try:
            lines = [l.rstrip() for l in open(f).read().splitlines() if l.strip()]
            head = next((l for l in lines if l.startswith('STPREADER ')), None)
            if not head:
                bad.append(f'{f}: no STPREADER header'); continue
            portal = 'rightmove' if head.startswith('STPREADER RM') else 'zoopla'
            km, cm = re.search(r'kept=(\d+)', head), re.search(r'check=(\d+)', head)
            if not (km and cm):
                bad.append(f'{f}: header has no kept or check'); continue
            body = [l for l in lines[lines.index(head) + 1:] if l.count('|') == 6]
            if len(body) != int(km.group(1)) or body_check(body) != int(cm.group(1)):
                bad.append(f'{f}: kept {km.group(1)} vs {len(body)} lines, or check differs (copy error)'); continue
            pages['RM' if portal == 'rightmove' else 'ZO'] += 1
            for l in body:
                pid, price, beds, typ, addr, img, red = l.split('|')
                if not pid.isdigit() or (portal, pid) in seen:
                    continue
                seen.add((portal, pid))
                rows.append(dict(portal=portal, id=pid, price=to_int(price) if price else None, beds=to_int(beds) if beds else None,
                                 type=typ.lower(), address=addr, search='RM reader' if portal == 'rightmove' else 'ZO reader',
                                 reduced=red == 'R', src='reader',
                                 photo=((RM_IMG if portal == 'rightmove' else ZO_IMG) + img) if img and not img.startswith('http') else img))
        except Exception as e:
            bad.append(f'{f}: {type(e).__name__} {str(e)[:80]}')
    idsum = sum(int(r['id']) for r in rows) % 1000000000
    json.dump(dict(rows=rows, bad=bad, pages=pages, idsum=idsum), open(a.out, 'w'), ensure_ascii=False)
    print(json.dumps(dict(rows=len(rows), rm=sum(r['portal'] == 'rightmove' for r in rows), zo=sum(r['portal'] == 'zoopla' for r in rows),
                          pages=pages, idsum=idsum, bad=bad)))


def reader_body(a):
    # Reader tab write bodies, one per call: --part clear | 0,1,2... (50 rows each) | stamp
    d = json.load(open(a.out)); rows = d['rows'][:500]
    now = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    if a.part == 'clear':
        data = [dict(range="'Reader'!A1:A501", values=[['Reader writing ' + now]] + [['']] * 500)]
    elif a.part == 'stamp':
        st = ', '.join(f"{k.lower()} {v} pages" for k, v in d['pages'].items())
        fl = list(a.faults or [])
        if len(d['rows']) > 500:
            fl.append(f"reader rows {len(d['rows'])} over the 500-row tab, {len(d['rows']) - 500} dropped (zoopla first)")
        flt = '; FAULT ' + ' / '.join(fl) if fl else ''
        data = [dict(range="'Reader'!A1", values=[[f"Reader ran {now}, rows {len(rows)}, idsum {sum(int(r['id']) for r in rows) % 1000000000}, {st}{flt}"]])]
    else:
        i = int(a.part); chunk = rows[i * 50:(i + 1) * 50]
        if not chunk:
            print('NONE'); return
        data = [dict(range=f"'Reader'!A{2 + i * 50}:A{1 + i * 50 + len(chunk)}", values=[[json.dumps(x, ensure_ascii=False)] for x in chunk])]
    print(json.dumps(dict(valueInputOption='RAW', data=data), ensure_ascii=False))


def reader_manual(a):
    # 06:10 step R1: Reader tab (A1 stamp + one JSON row per cell) -> manual.json entries dated START + 1 s. Never crashes.
    out = []
    if a.manual and os.path.exists(a.manual):
        try:
            out = json.load(open(a.manual))
        except Exception:
            out = []
    res = dict(reader_ran=False, reader_rows=0, reader_bad=0, stamp='')
    try:
        vals = sheet_values(a.reader)
    except Exception as e:
        vals = []; res['error'] = str(e)[:120]
    stamp = str(vals[0][0]) if vals and vals[0] else ''
    res['stamp'] = stamp[:500]
    res['faults'] = [f.strip() for f in stamp.split('; FAULT ', 1)[1].split(' / ')] if '; FAULT ' in stamp else []
    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    m = re.match(r'Reader ran (\d{4}-\d\d-\d\d)T\S+, rows (\d+), idsum (\d+)', stamp)
    if m and m.group(1) == today:
        d = (utc(a.cursor) + datetime.timedelta(seconds=1)).strftime('%Y-%m-%dT%H:%M:%SZ')
        got = []
        for r in vals[1:]:
            c = str(r[0]).strip() if r else ''
            if not c:
                continue
            try:
                x = json.loads(c)
                assert x.get('portal') in ('rightmove', 'zoopla') and str(x.get('id', '')).isdigit()
                x.update(date=d, src='reader', added=today)
                got.append(x)
            except Exception:
                res['reader_bad'] += 1
        if len(got) == int(m.group(2)) and sum(int(x['id']) for x in got) % 1000000000 == int(m.group(3)) and not res['reader_bad']:
            out.extend(got); res.update(reader_ran=True, reader_rows=len(got))
        else:
            res['error'] = f"Reader tab check failed: {len(got)} rows read, stamp says {m.group(2)}; bad cells {res['reader_bad']}"
    json.dump(out, open(a.manual, 'w'), ensure_ascii=False)
    print(json.dumps(res))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--reader-build', action='store_true')
    ap.add_argument('--reader-manual', action='store_true')
    ap.add_argument('--reader')
    ap.add_argument('--reader-body', action='store_true')
    ap.add_argument('--part')
    ap.add_argument('--faults', nargs='*')
    ap.add_argument('--cursor')
    ap.add_argument('--sheet')
    ap.add_argument('--live')
    ap.add_argument('--manual')
    ap.add_argument('--out', required=True)
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--readback')
    ap.add_argument('--pmi')
    ap.add_argument('--prepare', action='store_true')
    ap.add_argument('--keys')
    ap.add_argument('--start')
    ap.add_argument('--first')
    ap.add_argument('files', nargs='*')
    a = ap.parse_args()
    if a.reader_build:
        return reader_build(a)
    if a.reader_manual:
        return reader_manual(a)
    if a.reader_body:
        return reader_body(a)
    if a.check:
        return check(a)
    if a.prepare:
        return prepare(a)
    start = utc(a.cursor)
    cur = start - datetime.timedelta(hours=2)  # overlap; Key dedupe absorbs repeats

    sheet = sheet_values(a.sheet)  # rows from 'Buy list'!A2:K, sheet row = index + 2
    have = {}
    for i, r in enumerate(sheet):
        if r and r[0]:
            k0 = str(r[0]).strip()
            have[k0] = dict(row=i + 2, address=str(r[2]) if len(r) > 2 else '', price=to_int(r[3]) if len(r) > 3 else None,
                            beds=to_int(r[4]) if len(r) > 4 else None, portal=k0.split(':')[0])
    live = {str(r[0]).strip() for r in sheet_values(a.live) if r and r[0]}

    msgs = {}
    for f in a.files:
        d = json.load(open(f))
        for m in d.get('messages', []):
            msgs[m['id']] = m
    manual = json.load(open(a.manual)) if a.manual and os.path.exists(a.manual) else []

    C = dict(emails=0, listings=0, excluded=0, same_run=0, already=0, twins=0, price_changes=0, new=0, flagged_dup=0,
             edinburgh=0, over260=0, england=0, own_enquiry=0, old_messages=0)
    faults, capnotes, cands = [], [], []
    newest = start
    for mid, m in sorted(msgs.items(), key=lambda x: x[1]['date']):
        portal = next((v for k, v in SENDERS.items() if k in m.get('sender', '')), None)
        if not portal:
            continue
        dt = utc(m['date'])
        if dt <= cur:
            C['old_messages'] += 1
            continue
        subj = m.get('subject', '')
        if 'Sales enquiry from Stewart Thomson' in subj:  # (10)
            C['own_enquiry'] += 1
            continue
        C['emails'] += 1
        newest = max(newest, dt)
        h = m.get('htmlBody') or ''
        plain = m.get('plaintextBody') or ''
        txt = to_text(h) if h else ''
        if portal == 'onthemarket':
            ls, links = otm_listings(plain)
            for l in ls:
                l['photo'] = photo_for('onthemarket', l['id'], h, '') if h else ''
            hl = None
            if re.search(r'\nReduced\.\s*\n', plain):
                for l in ls:
                    l['reduced'] = True
            lo = sum(1 for l in ls if l.get('linkonly'))
            if lo and dt > start:
                faults.append(f'OTM parse: {links - lo} of {links} listings read, {lo} written as link-only rows, message {mid}')
        else:
            if not h:
                if dt > start:
                    faults.append(f'{SHORT[portal]} email with no HTML body, message {mid} ({subj[:60]})')  # (2)
                continue
            ls = html_listings(portal, h)
            hl = headline(portal, subj, txt)
        srch = search_name(portal, subj, txt, plain)
        weekly = bool(re.search(r'weekly', subj, re.I))
        keep = [l for l in ls if l['before_cut']]
        if portal == 'zoopla' and hl is not None and len(keep) != hl and len(ls) >= hl > 0:  # (5)
            keep = ls[:hl]
        fresh = dt > start  # overlap messages were reported last run
        if hl is not None and len(keep) != hl and fresh:  # (6)
            if portal == 'zoopla' and hl > len(keep) == 10:
                capnotes.append(dict(search=srch, headline=hl, shown=10, missing=hl - 10, date=m['date'],
                                     url=m.get('viewUrl', '')))
            elif portal == 'rightmove' and hl > len(keep) == 14:
                capnotes.append(dict(search=srch, headline=hl, shown=14, missing=hl - 14, date=m['date'],
                                     url=m.get('viewUrl', '')))
            elif not weekly:
                faults.append(f'{SHORT[portal]} count: headline {hl}, read {len(keep)}, message {mid} ({srch})')
        if not keep and not weekly and fresh and (hl or portal == 'onthemarket'):  # (7)
            faults.append(f'{SHORT[portal]} zero listings read, message {mid} ({srch})')
        for l in keep:
            C['listings'] += 1
            if excluded(l, portal):
                C['excluded'] += 1
                continue
            l.update(portal=portal, key=f"{portal}:{l['id']}", date=dt, search=srch,
                     reduced=l['reduced'] or bool(re.search(r'reduc', subj, re.I)))
            cands.append(l)
    for x in manual:
        dt = utc(x['date'])
        if dt <= cur:
            continue
        newest = max(newest, dt)
        C['listings'] += 1
        C['manual'] = C.get('manual', 0) + 1
        l = dict(id=str(x['id']), portal=x['portal'], key=f"{x['portal']}:{x['id']}", price=x.get('price'), beds=x.get('beds'),
                 address=x.get('address', ''), qual=x.get('qual', ''), search=x.get('search', SHORT[x['portal']]),
                 date=dt, reduced=bool(x.get('reduced')), added_on='', type=x.get('type', ''), newbuild=False, photo=x.get('photo', ''),
                 src=x.get('src', ''), added=x.get('added', ''))
        if excluded(l, x['portal']):
            C['excluded'] += 1
            continue
        cands.append(l)

    rows, upd, seen = [], [], {}
    for l in sorted(cands, key=lambda z: z['date']):
        k = l['key']
        if k in seen:  # (14) same run: newest price wins
            s0 = seen[k]
            if s0.get('src') == 'reader' and l.get('src') != 'reader':  # an email sighting keeps its own search, qualifier, dates and price
                for f in ('search', 'qual', 'added_on', 'reduced', 'cut', 'date'):
                    if l.get(f):
                        s0[f] = l[f]
                s0['src'] = ''
                if l['price']:
                    s0['price'] = l['price']
                C['same_run'] += 1
                continue
            if l.get('src') == 'reader':  # the reader never changes what an email said, but fills a missing photo
                if not s0.get('photo') and l.get('photo'):
                    s0['photo'] = l['photo']
                C['same_run'] += 1
                continue
            if l['price'] and seen[k]['price'] != l['price']:
                seen[k]['was'] = seen[k].get('was') or seen[k]['price']; seen[k]['price'] = l['price']
            if l.get('cut') and not seen[k].get('cut'):
                seen[k]['cut'] = l['cut']; seen[k]['reduced'] = True
            C['same_run'] += 1
            continue
        if k in have or k in live:  # (11)(15)(19)
            h0 = have.get(k)
            if h0 and l['price'] and h0['price'] and l['price'] != h0['price'] and l['date'] > start and l.get('src') != 'reader':  # only sightings after START change a price
                ex = [u for u in upd if u['key'] == k]
                if ex:
                    ex[0]['price'] = l['price']
                else:
                    upd.append(dict(row=h0['row'], price=l['price'], was=h0['price'], key=k))
                    C['price_changes'] += 1
            else:
                C['already'] += 1
            continue
        verdicts = [twin(l, o) for o in list(seen.values()) + list(have.values())]
        if 'yes' in verdicts:
            C['twins'] += 1
            continue
        l['dupflag'] = 'maybe' in verdicts
        seen[k] = l
    out_rows = []
    for l in sorted(seen.values(), key=lambda z: z['date'], reverse=True):
        addr = (l['address'] or '').strip()[:90]
        oc = outcode(addr)
        flags = [l['search']] + ([l['qual']] if l.get('qual') else [])  # (23)
        if (oc and re.match(r'EH([1-9]|1[0-7])$', oc)) or re.search(r'edinburgh', addr, re.I):
            flags.append('Edinburgh, parked'); C['edinburgh'] += 1  # (24)
        if re.search(r'berwick|northumberland|cornhill', addr, re.I) or (oc or '').startswith('TD15') or re.match(r'(CA|NE|DH)\d', oc or ''):
            flags.append('England'); C['england'] += 1
        if l['price'] and l['price'] > 260000:
            flags.append('Over £260k'); C['over260'] += 1  # (22)
        if l['price'] and l['price'] < 25000:
            flags.append('price under £25k, check')
        if l.get('dupflag'):
            flags.append('DUP CHECK INCOMPLETE'); C['flagged_dup'] += 1  # (18)
        days = ''
        if l.get('added_on'):
            d0 = datetime.datetime.strptime(l['added_on'], '%d/%m/%Y').date()
            days = (datetime.date.today() - d0).days  # (26)
        if l.get('was'):
            red = f"was £{l['was']:,}"
        elif l.get('cut') and l['price']:
            red = f"cut £{l['cut']:,}, was £{l['price'] + l['cut']:,} (per alert)"
        elif l.get('reduced'):
            red = 'reduced (alert gives no amount)'
        else:
            red = ''
        price = l['price'] if l['price'] is not None else ''
        out_rows.append([l['key'], l.get('added') or l['date'].astimezone(datetime.timezone.utc).strftime('%Y-%m-%d'), addr or ('(no address on card)' if l.get('src') == 'reader' else '(no address in alert)'),
                         price, l['beds'] if l['beds'] is not None else '', days, red, LINK[l['portal']].format(l['id']), '',
                         ' | '.join(flags), l.get('photo', '')])
    C['new'] = len(out_rows)
    by = {}
    for r in out_rows:
        p = r[0].split(':')[0]; by[SHORT[p]] = by.get(SHORT[p], 0) + 1
    res = dict(cursor_new=newest.strftime('%Y-%m-%dT%H:%M:%SZ'), counts=C, by_portal=by, faults=faults,
               rm_cap=capnotes, price_updates=upd, rows=out_rows,
               chunks=[])
    json.dump(res, open(a.out, 'w'), ensure_ascii=False)
    print(json.dumps(dict(cursor_new=res['cursor_new'], counts=C, by_portal=by, faults=len(faults), rm_cap=len(capnotes),
                          price_updates=len(upd), chunks=len(res['chunks']))))


if __name__ == '__main__':
    main()
