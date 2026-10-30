#!/usr/bin/env python3
"""Agregador de notícies de proximitat de Dial·EC.

Llegeix `fonts.json` (fonts, zones i a quina zona va cada emissora), baixa tots els feeds RSS/Atom,
els neteja i en fa, per a cada zona, una llista de les notícies més recents. En surten els fitxers
que llegeix l'app:

  <sortida>/zones.json      zones, a quina zona va cada emissora, la de recanvi i els feeds que
                            l'app llegeix directament quan l'usuari refresca
  <sortida>/<zona>.json     les notícies de la zona (titular, resum curt, mitjà, enllaç, imatge, hora)

Ús:
  python3 agrega.py --sortida sortida/ [--estat estat.json] [--informe informe.md]
  python3 agrega.py --index zones.json      # només zones.json, sense baixar res

Només fa servir la biblioteca estàndard de Python 3.
"""
import argparse
import gzip
import hashlib
import html
import json
import re
import ssl
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

# Els tallafocs dels diaris de Prensa Ibérica (Regió7, Diari de Girona, Empordà) rebutgen els agents que no
# semblen un navegador (406). Ens identifiquem igualment al final.
UA = 'Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Mobile Safari/537.36 DialEC-noticies/1.0'
SUMMARY_MAX = 300
ZONE_LIMIT = 20
# Una zona amb menys notícies pròpies que això s'omple amb les de la zona de recanvi.
ZONE_MIN_OWN = 12
# Les notícies més velles no entren (un feed municipal lent pot tenir-les de fa setmanes).
MAX_AGE_DAYS = 7
# Un feed local que s'assembla tant al «farcit» del mitjà (el que torna amb una adreça falsa) no és local.
GENERIC_OVERLAP = 0.6
# Dies sense poder llegir una font, o sense cap notícia nova, perquè l'informe en parli.
REPORT_FAIL_DAYS = 2
REPORT_SILENT_DAYS = 7
# Feeds per zona que l'app llegeix directament en refrescar.
LIVE_MAX = 3

CTX = ssl.create_default_context()


# ---------- Baixar ----------

def fetch(url, timeout=20, tries=2):
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept-Encoding': 'gzip, deflate'})
            with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
                data = r.read(6_000_000)
                enc = r.headers.get('Content-Encoding', '')
                if enc == 'gzip':
                    data = gzip.decompress(data)
                elif enc == 'deflate':
                    data = zlib.decompress(data)
                return data
        except Exception as e:  # noqa: BLE001 — qualsevol error de xarxa compta igual
            last = e
            if attempt + 1 < tries:
                time.sleep(2)
    raise last


# ---------- Dates ----------

# El 9 Nou escriu les dates en català: «Dv, 30 oct. 2026 11:06:00 +0100».
CA_MONTHS = {'gen': 'Jan', 'febr': 'Feb', 'feb': 'Feb', 'març': 'Mar', 'mar': 'Mar', 'abr': 'Apr', 'maig': 'May',
             'juny': 'Jun', 'jul': 'Jul', 'ag': 'Aug', 'ago': 'Aug', 'set': 'Sep', 'oct': 'Oct', 'nov': 'Nov', 'des': 'Dec'}
CA_DAYS = re.compile(r'^\s*(dl|dt|dc|dj|dv|ds|dg|dilluns|dimarts|dimecres|dijous|divendres|dissabte|diumenge)\.?,?\s*', re.I)


def parse_date(text):
    """Data d'un element en UTC, o None. Accepta RFC 822, ISO 8601 i RFC 822 en català."""
    if not text:
        return None
    s = text.strip()
    for attempt in (s, catalan_to_english(s)):
        if attempt is None:
            continue
        try:
            d = parsedate_to_datetime(attempt)
        except (TypeError, ValueError, IndexError):
            try:
                d = datetime.fromisoformat(attempt.replace('Z', '+00:00'))
            except ValueError:
                continue
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc)
    return None


def catalan_to_english(s):
    t = CA_DAYS.sub('', s)
    m = re.match(r'(\d{1,2})\s+([a-zç]+)\.?\s+(\d{4})\s+(.*)$', t, re.I)
    if not m:
        return None
    mon = CA_MONTHS.get(m.group(2).lower())
    if not mon:
        return None
    return f'{m.group(1)} {mon} {m.group(3)} {m.group(4)}'


# ---------- Text ----------

def strip_html(s):
    s = re.sub(r'<(script|style)\b.*?</\1>', ' ', s or '', flags=re.S | re.I)
    s = re.sub(r'<[^>]+>', ' ', s)
    return re.sub(r'\s+', ' ', html.unescape(s)).strip()


# Els peus que afegeix WordPress al resum.
WP_FOOTERS = [
    re.compile(r'\s*(The post|La entrada|L.entrada|L.article)\b.*?(appeared first on|se publicó primero en|apareix primer a|va aparèixer primer a).*$', re.I),
    re.compile(r'\s*(\[(…|\.\.\.|&hellip;)\]|Continua llegint.*|Llegeix més.*|Read more.*)$', re.I),
]


def clean_summary(desc, title):
    s = strip_html(desc)
    for f in WP_FOOTERS:
        s = f.sub('', s)
    s = s.strip()
    if title and normalize(s).startswith(normalize(title)):
        s = s[len(title):].lstrip(' .:-–—|')
    if normalize(s) == normalize(title) or len(s) < 20:
        return ''
    if len(s) > SUMMARY_MAX:
        cut = s[:SUMMARY_MAX]
        cut = cut[:cut.rfind(' ')] if ' ' in cut[SUMMARY_MAX // 2:] else cut
        s = cut.rstrip(' ,.;:') + '…'
    return s


def normalize(s):
    s = unicodedata.normalize('NFD', (s or '').lower())
    s = ''.join(c for c in s if unicodedata.category(c) != 'Mn')
    return re.sub(r'[^a-z0-9]+', ' ', s).strip()


CA_ONLY = set('amb els per més aquest aquesta també però és són perquè això seva seu des dels als pel pels hi va van'.split())
ES_ONLY = set('con los las por pero porque esto su desde hay fue y muy él ella están tiene'.split())


def looks_spanish(text):
    """Clarament en castellà (alguns digitals locals publiquen en totes dues llengües)."""
    w = re.findall(r"[a-zàáèéíìïòóúùüçñ·]+", (text or '').lower())
    ca = sum(1 for x in w if x in CA_ONLY)
    es = sum(1 for x in w if x in ES_ONLY)
    return es >= 3 and es > 2 * ca


def canonical_url(u):
    p = urllib.parse.urlsplit(u.strip())
    host = p.netloc.lower().removeprefix('www.')
    query = '&'.join(q for q in p.query.split('&') if q and not q.startswith(('utm_', 'fbclid', 'gclid')))
    return f'{host}{p.path.rstrip("/")}' + (f'?{query}' if query else '')


def similar_titles(a, b):
    ta, tb = set(normalize(a).split()), set(normalize(b).split())
    if len(ta) < 5 or len(tb) < 5:
        return normalize(a) == normalize(b)
    # El mateix titular amb un retoc («… aquest cap de setmana!»), no dues notícies que s'assemblen.
    return len(ta & tb) / len(ta | tb) >= 0.8


# ---------- Feeds ----------

def local(tag):
    return tag.rsplit('}', 1)[-1]


def image_of(item, desc):
    for c in item.iter():
        n = local(c.tag)
        url = c.get('url') or (c.get('href') if n == 'link' and c.get('rel') == 'enclosure' else None)
        if n in ('enclosure', 'thumbnail', 'content', 'link') and url:
            kind = (c.get('type') or '').lower()
            if kind.startswith(('audio', 'video', 'text', 'application')):
                continue
            if n == 'link' and not kind.startswith('image'):
                continue
            if url.startswith('https://'):
                return url
    m = re.search(r'<img[^>]+src=["\'](https://[^"\']+)', desc or '', re.I)
    return html.unescape(m.group(1)) if m else None


def parse_feed(data):
    """Elements d'un feed RSS 2.0, RSS 1.0 o Atom: diccionaris amb title, url, summary, image, date."""
    root = ET.fromstring(data)
    if local(root.tag) not in ('rss', 'feed', 'RDF'):
        raise ValueError(f'no és un feed ({local(root.tag)})')
    out = []
    for it in root.iter():
        if local(it.tag) not in ('item', 'entry'):
            continue
        fields = {}
        for c in it:
            fields.setdefault(local(c.tag), c)
        title = strip_html(fields['title'].text if 'title' in fields else '')
        link = fields.get('link')
        url = ''
        if link is not None:
            url = (link.get('href') or link.text or '').strip()
        if not url and 'guid' in fields and (fields['guid'].text or '').startswith('http'):
            url = fields['guid'].text.strip()
        desc = ''
        for k in ('description', 'summary', 'encoded', 'content'):
            if k in fields and (fields[k].text or '').strip():
                desc = fields[k].text
                break
        date = None
        for k in ('pubDate', 'published', 'updated', 'date'):
            if k in fields:
                date = parse_date(fields[k].text)
                if date:
                    break
        if title and url.startswith(('http://', 'https://')):
            out.append({'title': title, 'url': url, 'summary': clean_summary(desc, title),
                        'image': image_of(it, desc), 'date': date})
    return out


def mark_date_only(items):
    """Moltes notícies amb la mateixa hora exacta = notícies del diari en paper, que només tenen data."""
    times = [i['date'].strftime('%H:%M:%S') for i in items if i['date']]
    if len(times) >= 3:
        top = max(set(times), key=times.count)
        repeated = top if times.count(top) >= max(3, len(times) // 3) else None
    else:
        repeated = None
    for i in items:
        d = i['date']
        i['dateOnly'] = bool(d) and (d.strftime('%H:%M:%S') == repeated)


def prepare(items, now, source):
    """Treu el que no ha d'entrar i deixa cada notícia amb la font i una data acceptable."""
    keep = []
    contains = source.get('linkContains')
    for i in items:
        if contains and contains not in i['url']:
            continue
        if looks_spanish(i['title'] + ' ' + i['summary']):
            continue
        d = i['date']
        if d is None:
            continue
        if d > now + timedelta(hours=1):
            # Data del futur (Catalunya Press en té): si és d'avui, «ara»; si és de més enllà, fora.
            if d > now + timedelta(days=1):
                continue
            i['date'] = now
        if d < now - timedelta(days=MAX_AGE_DAYS):
            continue
        i['source'] = source['name']
        i['sourceId'] = source['id']
        keep.append(i)
    return keep


# ---------- Zones ----------

def dedupe_into(target, items, seen_urls):
    for i in items:
        key = canonical_url(i['url'])
        if key in seen_urls or any(similar_titles(i['title'], t['title']) for t in target):
            continue
        seen_urls.add(key)
        target.append(i)


def build_zone(zone_id, zones, by_source, built):
    """Llista d'una zona: les seves fonts primer; si en té poques, s'omple amb la zona de recanvi."""
    if zone_id in built:
        return built[zone_id]
    zone = zones[zone_id]
    cap = zone.get('maxPerSource', 6)
    pool = sorted((i for s in zone['sources'] for i in by_source.get(s, [])), key=lambda i: i['date'], reverse=True)
    own, seen, per = [], set(), {}
    candidates = []
    dedupe_into(candidates, pool, seen)
    for i in candidates:
        # Per mitjà, no per feed: El Punt Avui hi entra amb el feed del municipi i el de la comarca.
        if per.get(i['source'], 0) >= cap:
            continue
        per[i['source']] = per.get(i['source'], 0) + 1
        own.append(dict(i, zone=zone_id))
        if len(own) >= ZONE_LIMIT:
            break
    result = own
    fallback = zone.get('fallback')
    if fallback and len(own) < ZONE_MIN_OWN:
        extra = []
        dedupe_into(extra, [i for i in build_zone(fallback, zones, by_source, built)
                            if not any(similar_titles(i['title'], o['title']) for o in own)], seen)
        result = own + extra[:ZONE_LIMIT - len(own)]
    built[zone_id] = result
    return result


def item_json(i):
    return {
        'id': hashlib.sha1(canonical_url(i['url']).encode()).hexdigest()[:12],
        'title': i['title'],
        'summary': i['summary'] or None,
        'url': i['url'],
        'source': i['source'],
        'image': i['image'],
        'published': int(i['date'].timestamp()),
        'dateOnly': i['dateOnly'],
        'zone': i['zone'],
    }


# ---------- Programa ----------

def run(fonts, out_dir, state, now=None, fetcher=fetch):
    now = now or datetime.now(timezone.utc)
    sources = fonts['sources']
    controls = fonts.get('controls', {})
    urls = sorted({s['url'] for s in sources.values()} | set(controls.values()))
    with ThreadPoolExecutor(16) as ex:
        raw = dict(zip(urls, ex.map(lambda u: _safe(fetcher, u), urls)))

    control_links = {}
    for host, url in controls.items():
        data = raw.get(url)
        if isinstance(data, bytes):
            try:
                control_links[host] = {canonical_url(i['url']) for i in parse_feed(data)}
            except Exception:  # noqa: BLE001
                pass

    by_source = {}
    stamp = now.isoformat(timespec='seconds')
    for sid, s in sources.items():
        s = dict(s, id=sid)
        st = state.setdefault(sid, {})
        st['url'] = s['url']
        data = raw.get(s['url'])
        try:
            if isinstance(data, Exception):
                raise data
            items = parse_feed(data)
            host = urllib.parse.urlsplit(s['url']).netloc
            ctl = control_links.get(host)
            if ctl and not s.get('allowGeneric') and s['url'] != controls.get(host):
                links = {canonical_url(i['url']) for i in items}
                if links and len(links & ctl) / len(links) >= GENERIC_OVERLAP:
                    raise ValueError('torna el farcit general del mitjà, no notícies locals')
            mark_date_only(items)
            dates = [i['date'] for i in items if i['date']]
            kept = prepare(items, now, s)
            by_source[sid] = kept
            st.update(status='ok', lastOk=stamp, failures=0, firstFailure=None, items=len(items), kept=len(kept), error=None)
            if dates:
                newest = max(dates).isoformat(timespec='seconds')
                if newest > st.get('newest', ''):
                    st['newest'] = newest
        except Exception as e:  # noqa: BLE001
            st.update(status='error', failures=st.get('failures', 0) + 1, error=f'{type(e).__name__}: {e}'[:160])
            if st['failures'] == 1:
                st['firstFailure'] = stamp
    for sid in list(state):
        if sid not in sources:
            del state[sid]

    zones = fonts['zones']
    built = {}
    out_dir.mkdir(parents=True, exist_ok=True)
    changed = []
    for zid in zones:
        items = build_zone(zid, zones, by_source, built)
        doc = {'zone': zid, 'name': zones[zid]['name'], 'title': zones[zid]['title'],
               'items': [item_json(i) for i in items]}
        if write_if_changed(out_dir / f'{zid}.json', doc, stamp):
            changed.append(zid)
    if write_if_changed(out_dir / 'zones.json', index_of(fonts), stamp):
        changed.append('zones')
    for old in out_dir.glob('*.json'):
        if old.stem != 'zones' and old.stem not in zones:
            old.unlink()
            changed.append(old.stem)
    return changed


def index_of(fonts):
    """`zones.json`: el que l'app necessita per triar la zona. L'app en porta una còpia, que es fa amb
    `--index`, per quan encara no l'ha baixat.

    `live`: els feeds (com a molt [LIVE_MAX]) que l'app llegeix ella mateixa quan l'usuari estira per
    refrescar, perquè hi trobi el que s'ha publicat després de l'última passada."""
    zones, sources = fonts['zones'], fonts['sources']

    def live(z):
        # Els propis de la zona abans que El Punt Avui, que en un municipi publica poc.
        ids = z.get('live') or sorted(z['sources'], key=lambda i: 'elpuntavui.cat' in sources[i]['url'])[:LIVE_MAX]
        return [{k: v for k, v in (('name', sources[i]['name']), ('url', sources[i]['url']),
                                    ('linkContains', sources[i].get('linkContains'))) if v} for i in ids]
    return {
        'version': 1,
        'default': fonts['default'],
        'zones': {z: {'name': v['name'], 'title': v['title'], 'fallback': v.get('fallback'), 'live': live(v)}
                  for z, v in zones.items()},
        'territories': fonts['territories'],
        'stations': fonts['stations'],
    }


def _safe(fetcher, url):
    try:
        return fetcher(url)
    except Exception as e:  # noqa: BLE001
        return e


def write_if_changed(path, doc, stamp):
    """Desa només si el contingut ha canviat: si no, el commit de la Action no es fa."""
    if path.exists():
        try:
            old = json.loads(path.read_text(encoding='utf-8'))
            old.pop('generated', None)
            if old == doc:
                return False
        except ValueError:
            pass
    path.write_text(json.dumps(dict(doc, generated=stamp), ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    return True


def report(state, now, fail_days=REPORT_FAIL_DAYS):
    """Informe de les fonts amb problemes, en Markdown. Buit si totes van bé."""
    rows = []
    for sid, st in sorted(state.items()):
        if st.get('status') == 'error' and st.get('firstFailure'):
            since = datetime.fromisoformat(st['firstFailure'])
            if now - since >= timedelta(days=fail_days):
                rows.append(f"| `{sid}` | no es pot llegir des del {since:%Y-%m-%d} | {st.get('error', '')} |")
                continue
        if st.get('newest'):
            newest = datetime.fromisoformat(st['newest'])
            if now - newest >= timedelta(days=REPORT_SILENT_DAYS):
                rows.append(f"| `{sid}` | cap notícia nova des del {newest:%Y-%m-%d} | |")
    if not rows:
        return ''
    return ('Fonts de notícies amb problemes (els feeds que fallen no entren a les zones; '
            'les zones s\'omplen amb la de recanvi).\n\n| Font | Què passa | Error |\n|---|---|---|\n' + '\n'.join(rows) + '\n')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--fonts', default=str(Path(__file__).with_name('fonts.json')))
    ap.add_argument('--sortida', help='carpeta on van zones.json i <zona>.json')
    ap.add_argument('--index', help='només escriu zones.json en aquest fitxer (la còpia de l\'app), sense baixar res')
    ap.add_argument('--estat', help='fitxer JSON amb l\'estat de cada font (es llegeix i es reescriu)')
    ap.add_argument('--informe', help='fitxer Markdown amb les fonts amb problemes (s\'esborra si no n\'hi ha)')
    ap.add_argument('--dies-error', type=int, default=REPORT_FAIL_DAYS,
                    help='dies que una font ha de fallar perquè surti a l\'informe (0 = si falla ara)')
    a = ap.parse_args()
    fonts = json.loads(Path(a.fonts).read_text(encoding='utf-8'))
    if a.index:
        Path(a.index).write_text(json.dumps(index_of(fonts), ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
        return
    if not a.sortida:
        ap.error('cal --sortida (o --index)')
    state = {}
    if a.estat and Path(a.estat).exists():
        state = json.loads(Path(a.estat).read_text(encoding='utf-8'))
    now = datetime.now(timezone.utc)
    changed = run(fonts, Path(a.sortida), state, now)
    if a.estat:
        Path(a.estat).write_text(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True), encoding='utf-8')
    if a.informe:
        text = report(state, now, a.dies_error)
        p = Path(a.informe)
        if text:
            p.write_text(text, encoding='utf-8')
        elif p.exists():
            p.unlink()
    bad = sorted(k for k, v in state.items() if v.get('status') == 'error')
    print(f'zones canviades: {len(changed)}; fonts que fallen ({len(bad)}): {", ".join(bad) or "cap"}')


if __name__ == '__main__':
    sys.exit(main())
