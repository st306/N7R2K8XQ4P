#!/usr/bin/env python3
"""STP Mail Read fetch v1.0 (19 Nov 2026, Go Live; Stu "Approve" 19 Nov 11:04).

Pulls the property alert emails for one window from the STP Brain Mail Read web app
(Apps Script, gmail.readonly, four alert senders only) and writes parser-ready files,
so the STP Daily intake never has to read Gmail threads inline.

Usage:
  python3 -I stp_mailread_fetch.py --url URL --key KEY --after EPOCH --before EPOCH \
      --read-from ISO --cutoff ISO --out DIR

Writes DIR/thread_NNN.json, each {"id": "mailread_NNN", "messages": [...]} with the same
message fields as Gmail get_thread FULL_CONTENT (id, threadId, sender, subject, date,
htmlBody, plaintextBody). Line endings are made "\n" (the parser's OnTheMarket pattern
needs that). OnTheMarket weekly-review emails are left out and counted.
Prints one JSON line: {"ok", "pages", "messages", "msg": {"RM","ZO","OTM"},
"newest_msg", "weekly_skipped", "dropped", "fault"}.
Exit 0 when ok, 2 on a fault (the caller then sets HOLD).
"""
import argparse, json, os, subprocess, sys, time

PORTAL = {'property@alert.rightmove.co.uk': 'RM', 'propertyalerts@comms-2.zoopla.co.uk': 'ZO',
          'property-alerts@mail.onthemarket.com': 'OTM', 'no-reply@onthemarket-mail.com': 'OTM'}
MAX_PAGES = 150          # 15 messages a page, so up to 2,250 emails a window
PAUSE = 8                # seconds between pages (Gmail per-minute quota)
QUOTA_WAIT = 30          # seconds after a quota error
QUOTA_TRIES = 5


def post(url, body, tmp):
    with open(tmp, 'w') as f:
        json.dump(body, f)
    r = subprocess.run(['curl', '-sSL', '--max-time', '300', '-H', 'Content-Type: text/plain',
                        '--data-binary', '@' + tmp, url], capture_output=True, text=True)
    try:
        return json.loads(r.stdout)
    except Exception:
        return {'ok': False, 'error': 'not json: ' + (r.stdout or r.stderr)[:160]}


def fetch_page(url, key, after, before, token, tmp):
    body = {'key': key, 'op': 'alerts', 'after': after, 'before': before}
    if token:
        body['pageToken'] = token
    quota = other = 0
    while True:
        o = post(url, body, tmp)
        if o.get('ok'):
            return o, None
        err = str(o.get('error', ''))
        if 'Quota' in err or 'quota' in err or 'Rate Limit' in err:
            quota += 1
            if quota >= QUOTA_TRIES:
                return None, 'Mail Read quota: ' + err[:120]
            time.sleep(QUOTA_WAIT)
            continue
        other += 1
        if other >= 2:
            return None, 'Mail Read failed: ' + err[:120]
        time.sleep(15)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--url', required=True)
    ap.add_argument('--key', required=True)
    ap.add_argument('--after', type=int, required=True)
    ap.add_argument('--before', type=int, required=True)
    ap.add_argument('--read-from', required=True)
    ap.add_argument('--cutoff', required=True)
    ap.add_argument('--out', required=True)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    tmp = os.path.join(a.out, '_body.json')
    res = {'ok': False, 'pages': 0, 'messages': 0, 'msg': {'RM': 0, 'ZO': 0, 'OTM': 0},
           'newest_msg': '', 'weekly_skipped': 0, 'dropped': 0, 'fault': ''}
    token, n = '', 0
    while True:
        if n:
            time.sleep(PAUSE)
        page, fault = fetch_page(a.url, a.key, a.after, a.before, token, tmp)
        if fault:
            res['fault'] = fault + ' (page %d)' % n
            break
        msgs = []
        for m in page.get('thread', {}).get('messages', []):
            p = PORTAL.get(str(m.get('sender', '')).strip().lower())
            if not p:
                res['dropped'] += 1
                continue
            if p == 'OTM' and 'weekly review' in str(m.get('subject', '')).lower():
                res['weekly_skipped'] += 1
                continue
            for k in ('htmlBody', 'plaintextBody'):
                m[k] = str(m.get(k) or '').replace('\r\n', '\n')
            msgs.append(m)
            d = str(m.get('date', ''))
            if a.read_from < d <= a.cutoff:
                res['msg'][p] += 1
                if d > res['newest_msg']:
                    res['newest_msg'] = d
        res['dropped'] += int(page.get('dropped') or 0)
        with open(os.path.join(a.out, 'thread_%03d.json' % n), 'w') as f:
            json.dump({'id': 'mailread_%03d' % n, 'messages': msgs}, f)
        res['messages'] += len(msgs)
        n += 1
        res['pages'] = n
        token = page.get('next') or ''
        if not token:
            res['ok'] = True
            break
        if n >= MAX_PAGES:
            res['fault'] = 'window capped at %d pages' % MAX_PAGES
            break
    try:
        os.remove(tmp)
    except OSError:
        pass
    print(json.dumps(res))
    sys.exit(0 if res['ok'] else 2)


if __name__ == '__main__':
    main()
