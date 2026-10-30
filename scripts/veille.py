#!/usr/bin/env python3
"""
robot de veille des offres de stage.

À chaque passage (GitHub Actions, toutes les 2 heures) :
1. lit data/structures.json (les 2 065 structures de la liste) et data/recettes.json (comment lire
   chaque structure, trouvé par la découverte : plateforme, API cachée, page HTML, rendu navigateur,
   texte surveillé, Welcome to the Jungle)
2. interroge chaque structure avec sa ou ses méthodes ; les sites qui ne s'affichent qu'avec
   JavaScript sont ouverts par un navigateur une fois par jour (RENDER=1)
3. garde les stages / summer / off-cycle / alternance / VIE / graduate, enrichit les nouvelles offres
   (domaine, contrat, durée, début, date limite, lieu)
4. écrit data/offers.json, data/meta.json (bilan lu par le site) et data/state.json
Les structures sans recette (pas encore découvertes) sont lues comme avant (page carrières + plateformes).
"""
import json, re, os, sys, time, hashlib, asyncio, datetime as dt
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from commun import *  # noqa

SCHEMA = 2
WORKERS = int(os.environ.get('WORKERS', '32'))
MAX_DETAILS = int(os.environ.get('MAX_DETAILS', '400'))
RENDER = os.environ.get('RENDER', '') == '1'
EXTRA = os.environ.get('EXTRA', '') == '1'  # passage étendu : agrégateurs + sociétés hors liste
FAIL_N = int(os.environ.get('FAIL_N', '36'))  # échecs de suite avant « à revoir » (36 passages de 5 min = 3 h)
RENDER_CONC = int(os.environ.get('RENDER_CONC', '6'))
T0 = time.time()
FAST = ('ats', 'api', 'html', 'wttj')
# fermeture d'une offre : absente MISS_CLOSE passages de suite ET pas revue depuis 36 h
# (avant : 2 absences = fermée en 4 h, ce qui fermait à tort les offres des pages qui changent d'ordre
# ou des plateformes qui coupent la lecture en route)
MISS_CLOSE = 3
GRACE = (NOW - dt.timedelta(hours=36)).isoformat().replace('+00:00', 'Z')
STALE = (NOW - dt.timedelta(days=6)).isoformat().replace('+00:00', 'Z')  # source douteuse depuis 6 jours : fermée
FRESH = (NOW - dt.timedelta(hours=3)).isoformat().replace('+00:00', 'Z')  # last_seen réécrit au plus toutes les 3 h (fichier stable entre deux passages)
SWEEP = (NOW - dt.timedelta(days=8)).isoformat().replace('+00:00', 'Z')  # offres d'agrégateurs ou hors liste non revues depuis 8 jours : fermées
from sources import AGG, HL_TITLE, country_ok, is_hl, merge_annuaire, hors_liste, run_indeed, merge_results
from commun import _norm


def reread_key(o, v):
    """Adresse de la fiche complète d'une offre déjà connue (pour retrouver durée, début, date limite)."""
    u, via = o.get('url') or '', o.get('via')
    last = u.rstrip('/').rsplit('/', 1)[-1]
    try:
        if via == 'workday' and v:
            host, tenant, site = v
            path = urlparse(u).path
            i = path.find('/job/')
            return ['workday', f'https://{host}/wday/cxs/{tenant}/{site}{path[i:]}'] if i >= 0 else None
        if via == 'oracle' and v:
            host, site = v
            return ['oracle', f'{host}|{site}|{last}']
        if via == 'smartrecruiters' and v:
            return ['sr', f'https://api.smartrecruiters.com/v1/companies/{v}/postings/{last}']
        if via not in ('watch', 'workday', 'oracle', 'smartrecruiters') and u.startswith('http'):
            return ['page', u]
    except Exception:
        return None
    return None

def m_key(m):
    return m['m'] if m['m'] != 'ats' else m['k']


def run_method(c, m):
    """Offres brutes d'une structure pour une méthode (sans navigateur)."""
    k = m['m']
    if k == 'ats':
        v = m['v']; v = tuple(v) if isinstance(v, list) else v
        return FETCHERS[m['k']](c, v)
    if k == 'api':
        return fetch_api(c, m)
    if k == 'wttj':
        return j_wttj(c, m['slug'])
    if k == 'html':
        r = get(m['url']); return page_jobs(c, r.text, r.url)
    if k == 'watch':
        r = get(m['url']); return mentions(r.text, r.url) + page_jobs(c, r.text, r.url)
    raise ValueError(k)


def legacy_scan(c, seeds):
    """Ancienne méthode (structures pas encore découvertes) : page carrières + plateformes détectées."""
    raw, via, page_html, base = [], '', '', c.get('url')
    if c.get('url'):
        try: r = get(c['url']); page_html, base = r.text[:3_000_000], r.url
        except Exception: pass
    ats = {}
    for k, v in (seeds.get('ats') or {}).items(): ats[k] = tuple(v) if isinstance(v, list) else v
    for k, v in detect_ats(page_html).items(): ats.setdefault(k, v)
    for k, v in ats.items():
        if not ats_ok(c, k, v): continue
        try: raw = FETCHERS[k](c, v); via = k; break
        except Exception: pass
    if not via and page_html: raw, via = page_jobs(c, page_html, base), 'page'
    if not via: raise RuntimeError('page injoignable')
    for x in raw: x['src'] = via
    return raw, [via]


def scan(c, rec, seeds):
    """(offres brutes, infos) pour une structure, avec toutes ses méthodes rapides."""
    info = {'id': c['id'], 'ok': False, 'via': '', 'err': '', 'ran': [], 'complete': True}
    if rec is None:
        if not (c.get('mode') in ('a', 'p', 'v') or seeds): info['skip'] = True; return [], info
        try:
            raw, ran = legacy_scan(c, seeds); info.update(ok=True, via=ran[0], ran=ran)
            return raw, info
        except Exception as e:
            info['err'] = str(e)[:80]; return [], info
    raw = []
    meths = rec.get('methods') or []
    for m in meths:
        slow = m['m'] == 'render' or (m['m'] == 'watch' and m.get('render'))
        if slow: info['complete'] = False; continue
        try:
            got = run_method(c, m)
            if getattr(got, 'partial', False): info.setdefault('partial', []).append(m_key(m))
            for x in got: x['src'] = m_key(m)
            raw += got; info['ok'] = True; info['ran'].append(m_key(m)); info['via'] = info['via'] or m_key(m)
        except Exception as e:
            info['err'] = (m_key(m) + ': ' + str(e))[:100]
    if not meths: info['skip'] = True
    elif not info['ran'] and not info['complete']: info['skip'] = True  # que du rendu : traité au passage quotidien
    return raw, info


async def render_all(items):
    """Passage quotidien : ouvre dans un navigateur les sites qui n'affichent leurs offres qu'avec JavaScript."""
    from playwright.async_api import async_playwright
    from decouverte import visit, launch, new_ctx
    res = {}
    sem = asyncio.Semaphore(RENDER_CONC)
    async with async_playwright() as pw:
        browser = await launch(pw)

        async def one(c, m):
            async with sem:
                ctx = await new_ctx(browser)
                try:
                    v = await asyncio.wait_for(visit(ctx, m['url']), timeout=70)
                    raw = []
                    api = m.get('api')
                    if api:
                        want = urlparse(api['req']['url']).path
                        for spec, txt in v.jsons:
                            if urlparse(spec['url']).path != want: continue
                            try: raw += items_from(parse_json(txt), dict(api, _c=c), v.final)
                            except Exception: pass
                    pages = [(v.final, v.html)] + list(v.frames)
                    for u, h in pages:
                        raw += page_jobs(c, h, u)
                        if m['m'] == 'watch': raw += mentions(h, u)
                    for x in raw: x['src'] = m['m']
                    res.setdefault(c['id'], []).append((raw, None))
                except Exception as e:
                    res.setdefault(c['id'], []).append(([], str(e)[:80]))
                finally:
                    await ctx.close()
        await asyncio.gather(*(one(c, m) for c, m in items))
        await browser.close()
    return res


def main():
    structs = load('structures.json')
    seeds_all = {c['id']: c for c in (load('companies.json') or [])}
    if not structs: structs = list(seeds_all.values())
    recettes = load('recettes.json') or {}
    ann = load('annuaire.json') or {}
    ann_keys = {(cid, m.get('k')) for cid, ms in ann.items() for m in ms}
    recettes = merge_annuaire(recettes, ann)
    if EXTRA:
        hs, hr = hors_liste()
        ids0 = {c['id'] for c in structs}
        structs = structs + [h for h in hs if h['id'] not in ids0]
        recettes.update(hr)
    old_doc = load('offers.json') or {}
    old_snap = json.dumps(old_doc.get('offers', []), sort_keys=True, ensure_ascii=False)
    old = {o['id']: o for o in old_doc.get('offers', [])}
    state = load('state.json') or {}
    state['run'] = state.get('run', 0) + 1
    if state.get('schema') != SCHEMA: old = {}; state['schema'] = SCHEMA
    state.pop('pages', None)
    # sources déjà lues (« structure|méthode ») : les offres d'une source lue pour la première fois sont
    # marquées « déjà en ligne » au lieu d'apparaître comme nouvelles
    known = set(state.get('known_src') or [])
    if not known:
        known = {o['company_id'] + '|' + (o.get('via') or '') for o in old.values()}
    state.pop('known', None)
    fails = state.get('fails') or {}

    # un même compte de plateforme partagé (filiale + groupe) n'est lu qu'une fois
    seen_src, todo = set(), []
    for c in sorted(structs, key=lambda c: (str(c['id']).startswith(('n_', 'x_')), len(c['name']))):
        r = recettes.get(c['id'])
        if r:
            sig = json.dumps([m for m in r.get('methods', []) if m['m'] in ('ats', 'wttj')], sort_keys=True)
            if sig != '[]' and sig in seen_src: continue
            seen_src.add(sig)
        todo.append(c)
    byid = {c['id']: c for c in structs}

    results = {}
    with ThreadPoolExecutor(WORKERS) as ex:
        futs = {ex.submit(scan, c, recettes.get(c['id']), seeds_all.get(c['id'], {})): c for c in todo}
        for f in as_completed(futs):
            c = futs[f]
            try: results[c['id']] = f.result()
            except Exception as e: results[c['id']] = ([], {'id': c['id'], 'ok': False, 'err': str(e)[:80], 'ran': [], 'complete': True})
    t_fast = time.time() - T0
    if EXTRA:
        agg, pseudo = run_indeed(structs, recettes)
        for p in pseudo:
            if p['id'] not in byid:
                structs.append(p); byid[p['id']] = p
        merge_results(results, agg)

    if RENDER:
        items = [(byid[cid], m) for cid, r in recettes.items() if cid in byid
                 for m in r.get('methods', []) if m['m'] == 'render' or (m['m'] == 'watch' and m.get('render'))]
        print(f'rendu navigateur : {len(items)} pages', flush=True)
        rr = asyncio.run(render_all(items)) if items else {}
        for cid, lst in rr.items():
            raw, info = results.get(cid) or ([], {'id': cid, 'ok': False, 'via': '', 'err': '', 'ran': [], 'complete': True})
            info.pop('skip', None)
            for got, err in lst:
                if err is None:
                    raw += got; info['ok'] = True; info['ran'].append('render'); info['via'] = info['via'] or 'render'
                else:
                    info['err'] = 'rendu: ' + err
            info['complete'] = True
            results[cid] = (raw, info)

    offers = dict(old); new_ids = []
    url_idx = {}
    for o in old.values(): url_idx[o['url']] = o
    closed_now = 0
    for cid, (raw, info) in results.items():
        if info.get('skip'): continue
        c = byid[cid]
        if not info['ok']:
            if not is_hl(cid): fails[cid] = fails.get(cid, 0) + 1
            continue
        fails.pop(cid, None)
        fresh_src = {s for s in info['ran'] if cid + '|' + s not in known}
        known.update(cid + '|' + s for s in info['ran'])
        raw_by_src = Counter(x.get('src') for x in raw)
        raw = [x for x in raw if keep_offer(x) and not foreign(c, x['url'])]
        if c.get('fam') in ('corp', 'consulting', 'fintech', '', None):
            raw = [x for x in raw if FIN_TITLE.search(x.get('title') or '')]
        raw = [x for x in raw if (cid, x.get('src')) not in ann_keys or country_ok(c, x.get('location'))]
        if is_hl(cid):
            raw = [x for x in raw if country_of(x.get('location') or '') and HL_TITLE.search(x.get('title') or '')]
        live = set()
        own_titles = {_norm(o['title']) for o in offers.values() if o['company_id'] == cid and o['status'] == 'open' and o.get('via') not in AGG}
        for x in raw:
            x['title'] = clean(x['title'])
            prev = url_idx.get(x['url'])
            if prev and prev['company_id'] != cid: continue
            if not prev and x.get('src') in AGG and _norm(x['title']) in own_titles: continue
            oid = prev['id'] if prev else hashlib.sha1((cid + '|' + str(x.get('ext') or x['url'])).encode()).hexdigest()[:14]
            if oid in live: continue
            live.add(oid)
            if oid in offers:
                o = offers[oid]; o['status'] = 'open'; o.pop('closed_at', None); o.pop('miss', None)
                if (o.get('last_seen') or '') <= FRESH: o['last_seen'] = NOW_ISO
                if x.get('src'): o['via'] = x['src']
                continue
            o = {'id': oid, 'company_id': cid, 'company': c['name'], 'fam': c.get('fam', ''), 'site': site_of(c),
                 'title': x['title'][:200], 'url': x['url'], 'location': clean(x.get('location'))[:120], 'posted': x.get('posted'),
                 'first_seen': NOW_ISO, 'last_seen': NOW_ISO, 'status': 'open', 'via': x.get('src') or info['via'], 'deadline': x.get('deadline'),
                 '_desc': x.get('desc') or '', '_commit': x.get('commit') or ''}
            if x.get('detail') and len(x.get('desc') or '') < 600: o['dk'] = list(x['detail'])
            elif x.get('url') and not x.get('desc') and o['via'] in ('api', 'render', 'html', 'page', 'watch'): o['dk'] = ['page', x['url']]
            if (x.get('src') or info['via']) in fresh_src and not x.get('posted'): o['initial'] = True
            offers[oid] = o; url_idx[o['url']] = o
            new_ids.append(oid)
        ran = set(info['ran'])
        # source douteuse : lecture incomplète (requête en échec), rien lu du tout, ou moins de la moitié des
        # offres déjà connues revues -> page modifiée, blocage, pagination : on ne compte pas d'absence
        expect, back = Counter(), Counter()
        for oid, o in offers.items():
            if o['company_id'] == cid and o['status'] == 'open' and o.get('via') in ran:
                expect[o['via']] += 1
                if oid in live: back[o['via']] += 1
        shaky = set(info.get('partial') or []) | {s for s in ran if raw_by_src[s] == 0 and expect[s]}
        shaky |= {s for s, n in expect.items() if n >= 4 and back[s] < 0.5 * n}
        nothing = not any(raw_by_src.values())
        for oid, o in offers.items():
            if o['company_id'] != cid or oid in live or o['status'] != 'open': continue
            if not info['complete'] and o.get('via') not in ran: continue
            if o.get('via') in AGG and o.get('via') not in ran: continue
            if nothing or o.get('via') in shaky:
                if (o.get('last_seen') or '') <= STALE: o['status'] = 'closed'; o['closed_at'] = NOW_ISO; closed_now += 1
                continue
            if (o.get('last_seen') or '') <= GRACE:  # pas revue depuis 36 h alors que sa source a été lue en entier
                o['status'] = 'closed'; o['closed_at'] = NOW_ISO; closed_now += 1

    for o in offers.values():
        if o['status'] == 'open' and (o.get('via') in AGG or is_hl(o['company_id'])) and (o.get('last_seen') or '') <= SWEEP:
            o['status'] = 'closed'; o['closed_at'] = NOW_ISO; closed_now += 1

    # rattrapage : les offres ouvertes sans durée sont relues une fois en entier (description complète)
    ats_of = {}
    for cid2, r in recettes.items():
        for m in r.get('methods', []):
            if m['m'] == 'ats' and m.get('k') in ('workday', 'oracle', 'smartrecruiters'):
                v = m['v']; ats_of.setdefault((cid2, m['k']), tuple(v) if isinstance(v, list) else v)
    for o in offers.values():
        if o['status'] != 'open' or o.get('months') or o.get('dk') or o.get('reread'): continue
        k = reread_key(o, ats_of.get((o['company_id'], o.get('via'))))
        if k: o['dk'] = k
        o['reread'] = 1

    # fiches détaillées : nouvelles offres d'abord, puis rattrapage des anciennes pas encore enrichies
    new_set = set(new_ids)
    need = [offers[i] for i in new_ids if offers[i].get('dk') and not offers[i].get('initial')]
    need += [o for o in offers.values() if o.get('dk') and (o['id'] not in new_set or o.get('initial')) and o['status'] == 'open']
    need = need[:MAX_DETAILS]

    def fill(o):
        try:
            kind, u = o['dk']; txt, dl, loc, posted = detail_text(kind, u)
            o['_desc'] = txt; o['deadline'] = o.get('deadline') or dl
            if loc and not o['location']: o['location'] = clean(loc)[:120]
            if posted and not o.get('posted') and kind != 'workday': o['posted'] = posted
            o.pop('dk', None)
        except Exception:
            o['dk_fail'] = o.get('dk_fail', 0) + 1
            if o['dk_fail'] >= 3: o.pop('dk', None)
    with ThreadPoolExecutor(WORKERS) as ex: list(ex.map(fill, need))

    def enrich(o):
        c = byid.get(o['company_id'], {}); d = o.pop('_desc', '') or ''; com = o.pop('_commit', '') or ''
        ct = contract_of(o['title'] + ' ' + com)
        if not (o.get('contract') and ct == 'Stage' and not com): o['contract'] = ct  # relecture : garder le contrat déjà trouvé
        o['domain'] = domain_of(o['title'], d, c.get('fam'))
        o['months'] = months_of(o['title']) or months_of(d) or o.get('months')
        if o['months'] and o['months'] > 24: o['months'] = None
        o['start'] = start_of(o['title']) or start_of(d, loose=False) or (o.get('start') if (o.get('start') or '') >= NOW.strftime('%Y-%m') else None)
        dl = o.get('deadline')
        o['deadline'] = (str(dl)[:10] if dl and re.match(r'20\d{2}-\d{2}-\d{2}', str(dl)) else None) or deadline_of(d)
        if not o.get('location'): o['location'] = c.get('city', '')
        o['country'] = country_of((o.get('location') or '') + ' ' + (c.get('city') or ''))
        if d: o['excerpt'] = d[:500]
    for o in list(offers.values()):
        if o['id'] in new_set or '_desc' in o: enrich(o)

    limit = (NOW - dt.timedelta(days=30)).isoformat()
    offers = {k: o for k, o in offers.items() if not (o['status'] == 'closed' and (o.get('closed_at') or '') < limit)}
    out = sorted(offers.values(), key=lambda o: (o.get('posted') or o['first_seen']), reverse=True)

    # bilan : structures lues, échecs répétés (renvoyés à la découverte et montrés dans Alertes)
    failed = sorted(k for k, n in fails.items() if n >= FAIL_N)
    revoir = set(load('a_revoir.json') or []) | set(failed)
    save('a_revoir.json', sorted(revoir))
    ok_ids = sorted(cid for cid, (_, i) in results.items() if i.get('ok') and not is_hl(cid))
    tracked = sorted(set(ok_ids) | {cid for cid, r in recettes.items() if any(m['m'] in ('render',) or (m['m'] == 'watch' and m.get('render')) for m in r.get('methods', [])) and cid not in fails})
    via = Counter()
    for cid, (_, i) in results.items():
        if i.get('skip') or is_hl(cid): continue
        via[i.get('via') or 'échec'] += 1
    meta = {'updated': NOW_ISO, 'run': state['run'], 'seconds': round(time.time() - T0), 'seconds_fast': round(t_fast), 'render': RENDER,
            'structures': sum(1 for c in structs if not is_hl(c['id'])), 'companies_scanned': sum(1 for k, (_, i) in results.items() if not i.get('skip') and not is_hl(k)),
            'companies_ok': len(ok_ids), 'tracked_count': len(tracked), 'via': dict(via.most_common()),
            'open': sum(1 for o in out if o['status'] == 'open'), 'new_this_run': sum(1 for i in new_ids if i in offers and not offers[i].get('initial')),
            'initial_this_run': sum(1 for i in new_ids if i in offers and offers[i].get('initial')), 'closed_this_run': closed_now,
            'failed': failed, 'hors_liste_open': sum(1 for o in out if o['status'] == 'open' and is_hl(o['company_id'])), 'extra': EXTRA, 'tracked': tracked}
    state['known_src'] = sorted(known); state['fails'] = fails
    if RENDER: state['last_render'] = NOW_ISO
    if EXTRA: state['last_extra'] = NOW_ISO
    changed = json.dumps(out, sort_keys=True, ensure_ascii=False) != old_snap
    save('offers.json', {'updated': NOW_ISO if changed else old_doc.get('updated', NOW_ISO), 'offers': out})
    save('meta.json', meta, pretty=True)
    save('state.json', state)
    m2 = dict(meta); m2.pop('tracked'); m2['failed'] = len(failed)
    print(json.dumps(m2, ensure_ascii=False, indent=1))


if __name__ == '__main__':
    main()
