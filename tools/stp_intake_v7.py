#!/usr/bin/env python3
"""STP Daily v7 runner (16 Nov 2026, Go Live). All data work for the STP Daily run lives here, so the prompt only
fetches things into files and writes what this script prints. The listing parser stays the pinned intake.py
(v1.1.1, never edited); this script calls it.

Commands (all paths are files; every command prints one JSON line):
  unwrap IN OUT                      tool-result file (Gmail thread JSON, or a Zapier [{"type","text"}] wrapper) -> plain JSON
  window --z1 Z1 [--now EPOCH]       read window and the four Gmail queries
  sheet --out SHEET BLOCK...         join 'Buy list'!A:E blocks of 2000 rows (padded) into one values file
  parse --z1 Z1 --read-from T --cutoff T --sheet SHEET --live LIVE --parser intake.py --out OUT
        [--tsv TSV] [--reader READER] THREAD...
  chunks --out OUT --keys KEYS --dir DIR       exact-row write bodies of up to 25 rows
  check --out OUT --readback RB --first N --dir DIR   compare written rows; fix and price bodies
  logrow --out OUT --state STATE --dest FILE   the STP Property Brain Log run row body

Rules kept: never writes Buy list column I (Decision) content (always ''), a missing figure is '' never 0,
no network, no Airtable.
"""
import json, re, sys, os, argparse, datetime, subprocess

SENDERS = {'rightmove': 'property@alert.rightmove.co.uk', 'zoopla': 'propertyalerts@comms-2.zoopla.co.uk',
           'onthemarket': 'property-alerts@mail.onthemarket.com', 'onthemarket2': 'no-reply@onthemarket-mail.com'}
PORTAL_OF = {v: ('onthemarket' if k.startswith('onthemarket') else k) for k, v in SENDERS.items()}
SHORT = {'rightmove': 'RM', 'zoopla': 'ZO', 'onthemarket': 'OTM'}
SCOT = {'AB', 'DD', 'DG', 'EH', 'FK', 'G', 'HS', 'IV', 'KA', 'KW', 'KY', 'ML', 'PA', 'PH', 'TD', 'ZE'}
WINDOW_H = 12
BLOCK = 2000


def utc(s):
    s = str(s).strip().replace('Z', '+00:00')
    d = datetime.datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=datetime.timezone.utc)


def iso(d):
    return d.astimezone(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def say(**k):
    print(json.dumps(k, ensure_ascii=False))


def load_any(path):
    raw = open(path, encoding='utf-8').read().strip()
    d = json.loads(raw)
    # Zapier / tool wrappers: [{"type":"text","text":"..."}] or {"results":[{"body":...}]}
    if isinstance(d, list) and d and isinstance(d[0], dict) and 'text' in d[0] and 'type' in d[0]:
        # each text item is its own document; Zapier adds a plain "Action run ID" note after the JSON
        texts = [x.get('text', '') for x in d]
        for t in texts:
            try:
                return json.loads(t)
            except ValueError:
                continue
        return json.loads(''.join(texts))  # one JSON document split across items
    if isinstance(d, list) and d and all(isinstance(x, str) for x in d):
        return {'values': [[x] for x in d]}  # a flat key list
    return d


def find_values(d):
    if isinstance(d, dict):
        if 'values' in d and isinstance(d['values'], list):
            return d['values']
        if 'range' in d and 'majorDimension' in d:
            return []  # a range with no values key is an empty range
        for v in d.values():
            r = find_values(v)
            if r is not None:
                return r
    if isinstance(d, list):
        for v in d:
            r = find_values(v)
            if r is not None:
                return r
    return None


def find_thread(d):
    if isinstance(d, dict):
        if 'messages' in d and isinstance(d['messages'], list):
            return d
        for v in d.values():
            r = find_thread(v)
            if r is not None:
                return r
    if isinstance(d, list):
        for v in d:
            r = find_thread(v)
            if r is not None:
                return r
    return None


def cmd_unwrap(a):
    d = load_any(a.inp)
    t = find_thread(d)
    if t is not None:
        json.dump(t, open(a.outp, 'w'), ensure_ascii=False)
        return say(kind='thread', id=t.get('id', ''), messages=len(t['messages']))
    v = find_values(d)
    if v is not None:
        json.dump({'values': v}, open(a.outp, 'w'), ensure_ascii=False)
        return say(kind='values', rows=len(v))
    say(kind='unknown', error='no thread and no values found')
    sys.exit(2)


def cmd_window(a):
    z1 = utc(a.z1)
    now = (utc(a.now) if not a.now.isdigit() else datetime.datetime.fromtimestamp(int(a.now), datetime.timezone.utc)) if a.now else datetime.datetime.now(datetime.timezone.utc)
    read_from = z1 - datetime.timedelta(hours=2)
    cutoff = min(read_from + datetime.timedelta(hours=WINDOW_H), now)
    af, bf = int(read_from.timestamp()), int(cutoff.timestamp()) + 60
    q = {SHORT.get(PORTAL_OF[e], 'OTM') + ('2' if k == 'onthemarket2' else ''): f'from:{e} after:{af} before:{bf}'
         for k, e in SENDERS.items()}
    say(z1=iso(z1), read_from=iso(read_from), cutoff=iso(cutoff), backlog_hours=round((now - z1).total_seconds() / 3600, 1),
        queries=q)


def cmd_plan(a):
    """Block ranges for 'Buy list'!A:E from the key read. Every range is at least 2000 rows long, so every result is big
    enough to come back saved to a file (a small last block comes back inline and cannot be copied by hand)."""
    kv = find_values(load_any(a.keys)) or []
    last_row = len(kv) + 1  # sheet row of the last key (row 1 is the header)
    ranges, start = [], 2
    while start + BLOCK - 1 < last_row - BLOCK:
        ranges.append(start); start += BLOCK
    ranges.append(max(2, last_row - BLOCK + 1))  # the tail block overlaps the one before; overlap is merged by row
    out = [dict(start=r, range=f"'Buy list'!A{r}:E{r + BLOCK + 99}" if r == ranges[-1] else f"'Buy list'!A{r}:E{r + BLOCK - 1}")
           for r in ranges]
    say(last_row=last_row, blocks=out)


def cmd_sheet(a):
    """--block START:FILE ... ; rows placed by sheet row, so overlaps and short blocks cannot shift anything."""
    rows = {}
    for b in a.block:
        st, f = b.split(':', 1) if re.match(r'^\d+:', b) else ('', b)
        d = load_any(f)
        m = re.search(r"!A(\d+):", json.dumps(d)[:2000])  # the range Google echoes back is the truth
        if m and st and int(st) != int(m.group(1)):
            say(error=f'block start {st} does not match the range read ({m.group(1)}) in {f}'); sys.exit(2)
        if not (m or st):
            say(error=f'no start row for {f}'); sys.exit(2)
        st = int(m.group(1)) if m else int(st)
        v = find_values(d) or []
        for i, r in enumerate(v):
            rows[st + i] = r
    if not rows:
        say(error='no rows'); sys.exit(2)
    top = max(n for n, r in rows.items() if r and str(r[0]).strip())
    vals = [rows.get(n, []) for n in range(2, top + 1)]
    gaps = [n for n in range(2, top + 1) if n not in rows]
    json.dump({'values': vals}, open(a.out, 'w'), ensure_ascii=False)
    keys = [str(r[0]).strip() for r in vals if r and str(r[0]).strip()]
    if a.keys:
        kv = find_values(load_any(a.keys)) or []
        kk = [str(r[0]).strip() if r else '' for r in kv]
        mism = [i + 2 for i in range(min(len(kk), len(vals))) if kk[i] != (str(vals[i][0]).strip() if vals[i] else '')]
    else:
        kk, mism = [], []
    tail_missing = max(0, len([k for k in kk if k]) and (len(kk) + 1 - top))  # key rows below the last row read
    if tail_missing:
        mism = mism + list(range(top + 1, len(kk) + 2))
    say(rows=len(vals), tail_missing=tail_missing, keys=len(keys), duplicate_keys=len(keys) - len(set(keys)), unread_rows=gaps[:5],
        unread_count=len(gaps), key_mismatch_rows=mism[:5], key_mismatch_count=len(mism))


def pc_area(text):
    m = re.search(r'\b([A-Z]{1,2})\d[A-Z\d]?(?:\s*\d[A-Z]{2})?\b', (text or '').upper())
    return m.group(1) if m else None


def cmd_parse(a):
    z1, rf, co = utc(a.z1), utc(a.read_from), utc(a.cutoff)
    work = os.path.dirname(os.path.abspath(a.out))
    fdir = os.path.join(work, 'filtered')
    os.makedirs(fdir, exist_ok=True)
    stats = {'emails': {'RM': 0, 'ZO': 0, 'OTM': 0}, 'weekly_skipped': 0, 'after_cutoff': 0, 'threads': 0}
    newest = None
    files = []
    for i, f in enumerate(a.threads):
        t = find_thread(load_any(f))
        if t is None:
            say(error=f'not a thread file: {f}'); sys.exit(2)
        keep = []
        for m in t['messages']:
            snd = m.get('sender', '')
            portal = next((p for e, p in PORTAL_OF.items() if e in snd), None)
            if not portal:
                continue
            d = utc(m['date'])
            if d <= rf:
                continue
            if d > co:
                stats['after_cutoff'] += 1
                continue
            if portal == 'onthemarket' and re.search(r'weekly review', m.get('subject', ''), re.I):
                stats['weekly_skipped'] += 1  # weekly digests repeat that week's instant alerts (22 of 22 already listed, 16 Nov)
                continue
            keep.append(m)
            stats['emails'][SHORT[portal]] += 1
            newest = d if newest is None or d > newest else newest
        if keep:
            p = os.path.join(fdir, f't{i}.json')
            json.dump(dict(t, messages=keep), open(p, 'w'), ensure_ascii=False)
            files.append(p)
        stats['threads'] += 1
    manual, bad = [], []
    if a.tsv and os.path.exists(a.tsv):
        for n, line in enumerate(open(a.tsv, encoding='utf-8'), 1):
            if not line.strip():
                continue
            p = line.rstrip('\n').split('\t')
            if len(p) < 7 or p[0] not in SHORT or not p[1].isdigit():
                bad.append(n); continue
            d = utc(p[6])
            if d <= rf or d > co:
                continue
            manual.append(dict(portal=p[0], id=p[1], price=int(p[2]) if p[2].isdigit() else None,
                               beds=int(p[3]) if p[3].isdigit() else None, type=p[4].strip().lower(), address=p[5].strip(),
                               search=f'{SHORT[p[0]]} inline', date=iso(d), photo=p[7].strip() if len(p) > 7 else ''))
            stats['emails'][SHORT[p[0]]] += 0  # TSV lines are listings; their emails were counted by the prompt
            newest = d if newest is None or d > newest else newest
    reader_rows = 0
    if a.reader and os.path.exists(a.reader):
        v = find_values(load_any(a.reader)) or []
        stamp = str(v[0][0]) if v and v[0] else ''
        m = re.match(r'Reader ran (\S+), rows (\d+)', stamp)
        if m and 'FAULT' not in stamp and utc(m.group(1)) > rf:
            for r in v[1:]:
                try:
                    o = json.loads(r[0]) if r else None
                except Exception:
                    o = None
                if not o or str(o.get('portal', '')).lower() not in ('rightmove', 'zoopla', 'rm', 'zo') or not str(o.get('id', '')).isdigit():
                    continue
                pt = {'rm': 'rightmove', 'zo': 'zoopla'}.get(str(o['portal']).lower(), str(o['portal']).lower())
                manual.append(dict(portal=pt, id=str(o['id']), price=o.get('price') if isinstance(o.get('price'), int) else None,
                                   beds=o.get('beds') if isinstance(o.get('beds'), int) else None, type=str(o.get('type', '')),
                                   address=str(o.get('address', '')), search=f'{SHORT[pt]} reader', date=m.group(1),
                                   photo=str(o.get('photo', ''))))
                reader_rows += 1
    mpath = os.path.join(work, 'manual.json')
    json.dump(manual, open(mpath, 'w'), ensure_ascii=False)
    raw = os.path.join(work, 'parsed.json')
    cmd = [sys.executable, '-I', a.parser, '--cursor', a.z1, '--sheet', a.sheet, '--live', a.live, '--manual', mpath,
           '--out', raw] + files
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        say(error='parser failed', tail=r.stderr.strip().splitlines()[-5:]); sys.exit(3)
    res = json.load(open(raw))
    rows, dropped, nopc = [], 0, 0
    for row in res['rows']:
        area = pc_area(row[2])
        if area is None:
            row[9] = (row[9] + ' | NO POSTCODE').strip(' |'); nopc += 1
        elif area not in SCOT or re.search(r'\bTD15\b', (row[2] or '').upper()):
            dropped += 1; continue
        if 'England' in row[9]:
            dropped += 1; continue
        row[8] = ''  # column I Decision is Stu's: never written
        rows.append(row)
    out = dict(z1=a.z1, read_from=a.read_from, cutoff=a.cutoff, z1_new=a.cutoff, newest_message=iso(newest) if newest else '',
               rows=rows, price_updates=res['price_updates'], parser_counts=res['counts'], parser_faults=res['faults'],
               rm_cap=res['rm_cap'], stats=stats, england_or_outside_dropped=dropped, no_postcode=nopc,
               manual_listings=len(manual), reader_rows=reader_rows, tsv_bad_lines=bad)
    json.dump(out, open(a.out, 'w'), ensure_ascii=False)
    by = {}
    for row in rows:
        k = SHORT[row[0].split(':')[0]]; by[k] = by.get(k, 0) + 1
    say(new_rows=len(rows), by_portal=by, emails=stats['emails'], weekly_skipped=stats['weekly_skipped'],
        price_updates=len(res['price_updates']), dropped=dropped, no_postcode=nopc, parser_faults=len(res['faults']),
        tsv_bad_lines=bad, reader_rows=reader_rows, z1_new=a.cutoff)


def cmd_chunks(a):
    out = json.load(open(a.out))
    kv = find_values(load_any(a.keys)) or []
    have = {str(r[0]).strip() for r in kv if r and str(r[0]).strip()}
    rows = [r for r in out['rows'] if r[0] not in have]
    os.makedirs(a.dir, exist_ok=True)
    last = len(kv) + 1  # last used sheet row (row 1 is the header)
    first = last + 1
    bodies = []
    for i in range(0, len(rows), 25):
        part = rows[i:i + 25]
        rng = f"'Buy list'!A{last + 1}:K{last + len(part)}"
        body = {'valueInputOption': 'RAW', 'data': [{'range': rng, 'values': part}]}
        p = os.path.join(a.dir, f'chunk{i // 25}.json')
        json.dump(body, open(p, 'w'), ensure_ascii=False)
        bodies.append(dict(file=p, range=rng, rows=len(part)))
        last += len(part)
    out['written_rows'] = rows
    out['first'] = first
    out['last'] = last
    json.dump(out, open(a.out, 'w'), ensure_ascii=False)
    say(to_write=len(rows), skipped_already_on_sheet=len(out['rows']) - len(rows), first=first, last=last, chunks=bodies)


def norm(v):
    if v is None:
        return ''
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


def cmd_check(a):
    out = json.load(open(a.out))
    want = out.get('written_rows', [])
    d = load_any(a.readback)
    m = re.search(r"!A(\d+):", json.dumps(d)[:2000])
    if m and int(m.group(1)) != a.rb_start:
        say(error=f'--rb-start {a.rb_start} does not match the range read ({m.group(1)})'); sys.exit(2)
    got = find_values(d) or []
    got = got[a.first - a.rb_start:]  # the read-back starts up to 1500 rows above FIRST so it is big enough to be saved to a file
    os.makedirs(a.dir, exist_ok=True)
    fixes = []
    for n, w in enumerate(want):
        g = got[n] if n < len(got) else []
        if [norm(x) for x in w] != [norm(g[c]) if c < len(g) else '' for c in range(len(w))]:
            fixes.append(dict(range=f"'Buy list'!A{a.first + n}:K{a.first + n}", values=[w]))
    junk = [f'row {a.first + n}' for n in range(len(want), len(got)) if got[n] and norm(got[n][0])]
    res = dict(rows_checked=len(want), wrong=len(fixes), junk_below=junk)
    if fixes and len(fixes) <= 3:
        p = os.path.join(a.dir, 'fixes.json')
        json.dump({'valueInputOption': 'RAW', 'data': fixes}, open(p, 'w'), ensure_ascii=False)
        res['fix_body'] = p
    elif fixes:
        res['refused'] = 'more than 3 rows wrong: positions not trusted, no fixes written'
    pu = out.get('price_updates', [])
    if pu:
        data = []
        for u in pu:
            data.append({'range': f"'Buy list'!D{u['row']}", 'values': [[u['price']]]})
            data.append({'range': f"'Buy list'!G{u['row']}", 'values': [[f"was £{u['was']:,}"]]})
        p = os.path.join(a.dir, 'prices.json')
        json.dump({'valueInputOption': 'RAW', 'data': data}, open(p, 'w'), ensure_ascii=False)
        res['price_body'] = p
        res['price_changes'] = len(pu)
    say(**res)


def cmd_logrow(a):
    out = json.load(open(a.out)) if a.out and os.path.exists(a.out) else {}
    st = json.load(open(a.state))
    try:
        from zoneinfo import ZoneInfo
        now = datetime.datetime.now(ZoneInfo('Europe/London'))
    except Exception:
        now = datetime.datetime.now(datetime.timezone.utc)
    st.setdefault('time', now.strftime('%H:%M'))
    by = {}
    for row in out.get('written_rows', []):
        k = SHORT[row[0].split(':')[0]]; by[k] = by.get(k, 0) + 1
    em = out.get('stats', {}).get('emails', {})
    faults = st.get('faults', []) + [f'parser: {f}' for f in out.get('parser_faults', [])]
    e = (f"Z1 {st.get('z1_before', '')} to {st.get('z1_after', '')}; rows written RM {by.get('RM', 0)}, ZO {by.get('ZO', 0)}, "
         f"OTM {by.get('OTM', 0)}; emails RM {em.get('RM', 0)}, ZO {em.get('ZO', 0)}, OTM {em.get('OTM', 0)}; "
         f"OTM weekly reviews skipped {out.get('stats', {}).get('weekly_skipped', 0)}; price changes {len(out.get('price_updates', []))}; "
         f"dropped outside Scotland {out.get('england_or_outside_dropped', 0)}; calls {st.get('calls', '?')}; ")
    e += ' | '.join(faults) if faults else 'no faults'
    moved = st.get('z1_after') and st.get('z1_after') != st.get('z1_before')
    row = [st.get('date', now.strftime('%Y-%m-%d')), st.get('time', ''), 'Run', f"STP Daily v7 {st.get('time', '')}", e,
           str(len(out.get('written_rows', []))), 'Run', 'auto', 'OK' if moved and not faults else ('HELD' if not moved else 'OK with faults'),
           'STP Daily', '', 'n/a']
    row = [("'" + c) if isinstance(c, str) and c and c[0] in '=+-@' else c for c in row]
    json.dump({'values': [row]}, open(a.dest, 'w'), ensure_ascii=False)
    say(logrow=a.dest, status=row[8])


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest='cmd', required=True)
    p = sp.add_parser('unwrap'); p.add_argument('inp'); p.add_argument('outp')
    p = sp.add_parser('window'); p.add_argument('--z1', required=True); p.add_argument('--now')
    p = sp.add_parser('plan'); p.add_argument('--keys', required=True)
    p = sp.add_parser('sheet'); p.add_argument('--out', required=True); p.add_argument('--keys'); p.add_argument('--block', action='append', required=True)
    p = sp.add_parser('parse')
    for k in ('--z1', '--read-from', '--cutoff', '--sheet', '--live', '--parser', '--out'):
        p.add_argument(k, required=True)
    p.add_argument('--tsv'); p.add_argument('--reader'); p.add_argument('threads', nargs='*')
    p = sp.add_parser('chunks'); p.add_argument('--out', required=True); p.add_argument('--keys', required=True); p.add_argument('--dir', required=True)
    p = sp.add_parser('check'); p.add_argument('--out', required=True); p.add_argument('--readback', required=True)
    p.add_argument('--first', type=int, required=True); p.add_argument('--rb-start', type=int, required=True); p.add_argument('--dir', required=True)
    p = sp.add_parser('logrow'); p.add_argument('--out'); p.add_argument('--state', required=True); p.add_argument('--dest', required=True)
    a = ap.parse_args()
    {'unwrap': cmd_unwrap, 'window': cmd_window, 'plan': cmd_plan, 'sheet': cmd_sheet, 'parse': cmd_parse, 'chunks': cmd_chunks,
     'check': cmd_check, 'logrow': cmd_logrow}[a.cmd](a)


if __name__ == '__main__':
    main()
