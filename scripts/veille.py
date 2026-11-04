#!/usr/bin/env python3
"""
robot de veille des offres de stage.

À chaque passage (GitHub Actions, toutes les 5 minutes, dans une boucle de 5 h 30) :
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
EXTRA = os.environ.get('EXTRA', '') == '1'  # passage étendu : sociétés hors liste
AGGR = os.environ.get('AGGR', '1') == '1'  # agrégateurs (Indeed, Glassdoor, Google, France Travail), par tranches à chaque passage
FAIL_N = int(os.environ.get('FAIL_N', '36'))  # échecs de suite avant « à revoir » (36 passages de 5 min = 3 h)
RENDER_CONC = int(os.environ.get('RENDER_CONC', '6'))
T0 = time.time()
FAST = ('ats', 'api', 'html', 'wttj')
CLOSED_KEEP_DAYS = int(os.environ.get('CLOSED_KEEP_DAYS', '30'))  # offres fermées gardées (statut « closed ») avant d'être retirées du fichier
ERR_RATE_MAX = float(os.environ.get('ERR_RATE_MAX', '0.5'))  # part de sources en échec au-delà de laquelle le passage est « dégradé »
ERRS = []  # erreurs d'une étape du passage (l'étape est sautée, les offres déjà captées sont quand même enregistrées)


def err(phase, e):
    msg = f'{type(e).__name__}: {e}'[:200] if isinstance(e, BaseException) else str(e)[:200]
    ERRS.append({'phase': phase, 'err': msg})
    print(f'ERREUR [{phase}] {msg}', file=sys.stderr, flush=True)


# Corporate VC : la plateforme de recrutement est celle de tout le groupe (ex. Sopra Steria / CS Group, Air Liquide, Swisscom,
# CNP), pas celle du fonds -> intitulé finance exigé. La liste d'identifiants sert de filet si structures.json n'a pas « sub ».
CVC_IDS = {'n_115k_la_banque_postale', 'n_574_invest', 'n_airbus_ventures', 'n_aliad_air_liquide_ventur', 'n_bmw_i_ventures', 'n_bosch_ventures',
           'n_capgemini_ventures', 'n_convivialite_ventures', 'n_credit_mutuel_innovation', 'n_decathlon_pulse', 'n_edf_pulse_ventures',
           'n_engie_new_ventures', 'n_l_oreal_bold', 'n_leonard_vinci', 'n_m_ventures', 'n_macif_innovation', 'n_maif_avenir', 'n_mh_innov',
           'n_obratori_loccitane_group', 'n_open_cnp', 'n_orange_ventures', 'n_se_ventures', 'n_seb_alliance', 'n_sopra_steria_ventures',
           'n_swisscom_ventures', 'n_via_id', 'n_zuhlke_ventures'}
# grands groupes / réseaux dont la plateforme publie tous les métiers (banques, Big Four, groupes du CAC 40, conseil, régulateurs,
# immobilier, banques privées, holdings familiales) : les postes hors finance (IT, ingénierie, marketing, RH…) sont écartés
GROUP_FAMS = ('corp', 'fintech', 'consulting', 'ts', 'bb', 'fb', 'rating', 're', 'wealth', 'am', 'fo')


def is_cvc(c):
    return (c or {}).get('sub') == 'Corporate VC' or (c or {}).get('id') in CVC_IDS


def group_kind(c):
    c = c or {}
    if is_cvc(c): return 'strict'
    if c.get('fam') in GROUP_FAMS or (c.get('size') == 'large' and c.get('fam') != 'trading'): return 'light'
    return None


def cvc_ok(title):
    return finance_strict(title, HL_TITLE)


def title_ok(c, x):
    """Intitulé gardé pour cette structure ? (les sources « trust », 100 % finance, ne sont jamais filtrées)"""
    if x.get('trust'): return True
    t = x.get('title') or ''
    g = group_kind(c)
    if g == 'strict': return cvc_ok(t)
    if g == 'light': return not hors_finance(t)
    return True
# fermeture d'une offre : absente MISS_CLOSE passages de suite ET pas revue depuis 36 h
# (avant : 2 absences = fermée en 4 h, ce qui fermait à tort les offres des pages qui changent d'ordre
# ou des plateformes qui coupent la lecture en route)
MISS_CLOSE = 3
GRACE = (NOW - dt.timedelta(hours=36)).isoformat().replace('+00:00', 'Z')
STALE = (NOW - dt.timedelta(days=6)).isoformat().replace('+00:00', 'Z')  # source douteuse depuis 6 jours : fermée
FRESH = (NOW - dt.timedelta(hours=3)).isoformat().replace('+00:00', 'Z')  # last_seen réécrit au plus toutes les 3 h (fichier stable entre deux passages)
SWEEP = (NOW - dt.timedelta(days=8)).isoformat().replace('+00:00', 'Z')  # offres d'agrégateurs ou hors liste non revues depuis 8 jours : fermées
from sources import AGG, PLUS, HL_TITLE, country_ok, is_hl, merge_annuaire, hors_liste, merge_results, home_countries
from agregateurs import run_aggregators
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

def reread_url(o):
    """Fiche complète d'après l'adresse de l'offre, quand elle pointe vers une plateforme à API (liens de Trackr, SimplifyJobs...)."""
    u = o.get('url') or ''
    last = u.split('?')[0].rstrip('/').rsplit('/', 1)[-1]
    ats = detect_ats(u)
    try:
        if 'workday' in ats and '/job/' in u:
            host, tenant, site = ats['workday']
            path = urlparse(u).path
            return ['workday', f'https://{host}/wday/cxs/{tenant}/{site}{path[path.find("/job/"):]}']
        m = re.search(r'/job/(\d+)', u)
        if 'oracle' in ats and m:
            host, site = ats['oracle']
            return ['oracle', f'{host}|{site}|{m.group(1)}']
        m = re.search(r'gh_jid=(\d+)|/jobs/(\d+)', u)
        if 'greenhouse' in ats and m:
            return ['gh', f"https://boards-api.greenhouse.io/v1/boards/{ats['greenhouse']}/jobs/{m.group(1) or m.group(2)}"]
        if 'lever' in ats and re.fullmatch(r'[0-9a-f-]{36}', last):
            eu = 'eu.' if ats['lever'][1] else ''
            return ['lever', f"https://api.{eu}lever.co/v0/postings/{ats['lever'][0]}/{last}"]
        if 'smartrecruiters' in ats and re.fullmatch(r'\d{6,}.*', last):
            return ['sr', f"https://api.smartrecruiters.com/v1/companies/{ats['smartrecruiters']}/postings/{last.split('-')[0]}"]
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


def load_offers():
    """offers.json illisible (conflit git, fichier tronqué) : on s'arrête au lieu de repartir de zéro et d'effacer les offres captées."""
    p = os.path.join(DATA, 'offers.json')
    doc = load('offers.json')
    if doc is None and os.path.exists(p) and os.path.getsize(p) > 0:
        raise RuntimeError('data/offers.json illisible : passage arrêté pour ne pas perdre les offres déjà captées')
    return doc or {}


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
    old_doc = load_offers()
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
    if AGGR:
        try:
            agg, pseudo = run_aggregators(structs, recettes, state['run'])
            for p in pseudo:
                if p['id'] not in byid:
                    structs.append(p); byid[p['id']] = p
            merge_results(results, agg)
        except Exception as e:
            err('agrégateurs', e)

    if RENDER:
        items = [(byid[cid], m) for cid, r in recettes.items() if cid in byid
                 for m in r.get('methods', []) if m['m'] == 'render' or (m['m'] == 'watch' and m.get('render'))]
        print(f'rendu navigateur : {len(items)} pages', flush=True)
        try:
            rr = asyncio.run(render_all(items)) if items else {}
        except Exception as e:
            err('rendu navigateur', e); rr = {}
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
    failed_now = []  # sources en échec à ce passage (structure de la liste dont aucune méthode n'a répondu)

    def close(o, reason=None):
        nonlocal closed_now
        o['status'] = 'closed'; o['closed_at'] = NOW_ISO; closed_now += 1
        if reason: o['closed_reason'] = reason
        else: o.pop('closed_reason', None)

    for cid, (raw, info) in results.items():
      try:
        if info.get('skip'): continue
        c = byid[cid]
        if not info['ok']:
            if not is_hl(cid):
                fails[cid] = fails.get(cid, 0) + 1
                failed_now.append({'id': cid, 'err': (info.get('err') or '')[:100]})
            continue
        fails.pop(cid, None)
        fresh_src = {s for s in info['ran'] if cid + '|' + s not in known}
        known.update(cid + '|' + s for s in info['ran'])
        raw_by_src = Counter(x.get('src') for x in raw)
        raw = [x for x in raw if keep_offer(x) and (x.get('trust') or not foreign(c, x['url']))]
        if c.get('fam') in ('corp', 'consulting', 'fintech', '', None):
            raw = [x for x in raw if x.get('trust') or FIN_TITLE.search(x.get('title') or '') or (c.get('fam') == 'consulting' and re.search(r'consult', x.get('title') or '', re.I))]
        raw = [x for x in raw if title_ok(c, x)]  # Corporate VC : intitulé finance exigé ; grands groupes : postes hors finance écartés
        raw = [x for x in raw if (cid, x.get('src')) not in ann_keys or country_ok(c, x.get('location'))]
        if is_hl(cid):
            raw = [x for x in raw if x.get('trust') or (country_of(x.get('location') or '') and HL_TITLE.search(x.get('title') or ''))]
        live = set()
        own_titles = {_norm(o['title']) + '|' + country_of(o.get('location') or '') for o in offers.values() if o['company_id'] == cid and o['status'] == 'open'}
        for x in raw:
            x['title'] = clean(x['title'])
            prev = url_idx.get(x['url'])
            if prev and prev['company_id'] != cid: continue
            if not prev and x.get('src') in AGG and _norm(x['title']) + '|' + country_of(x.get('location') or '') in own_titles: continue
            oid = prev['id'] if prev else hashlib.sha1((cid + '|' + str(x.get('ext') or x['url'])).encode()).hexdigest()[:14]
            if oid in live: continue
            live.add(oid)
            if oid in offers:
                o = offers[oid]; o['status'] = 'open'; o.pop('closed_at', None); o.pop('miss', None)
                if (o.get('last_seen') or '') <= FRESH: o['last_seen'] = NOW_ISO
                if (not o.get('site') or NOLOGO_HOST.search(host_of(o['site']) + '.')) and official_site(c): o['site'] = official_site(c)
                if x.get('logo') and not o.get('logo'): o['logo'] = x['logo']
                if x.get('start') and not o.get('start'): o['start'] = x['start']
                if x.get('months') and not o.get('months'): o['months'] = x['months']
                if x.get('src') in PLUS and x.get('desc') and not o.get('txt2') and not (o.get('months') and o.get('start')):
                    o['_desc'] = x['desc']; o['txt2'] = 1  # texte complet reçu une fois pour retrouver durée et début
                if x.get('src') and (x['src'] not in PLUS or o.get('via') in AGG): o['via'] = x['src']
                continue
            o = {'id': oid, 'company_id': cid, 'company': c['name'], 'fam': c.get('fam', ''), 'site': official_site(c),
                 'title': x['title'][:200], 'url': x['url'], 'location': clean(x.get('location'))[:120], 'posted': x.get('posted'),
                 'first_seen': NOW_ISO, 'last_seen': NOW_ISO, 'status': 'open', 'via': x.get('src') or info['via'], 'deadline': x.get('deadline'),
                 '_desc': x.get('desc') or '', '_commit': x.get('commit') or ''}
            if x.get('logo'): o['logo'] = x['logo']  # logo de l'entreprise fourni par l'agrégateur
            if x.get('start'): o['start'] = x['start']
            if x.get('months'): o['months'] = x['months']  # durée calculée d'après les dates de début et de fin de l'API
            if x.get('trust'): o['trust'] = 1  # source 100 % finance : jamais écartée pour son intitulé
            if x.get('detail') and len(x.get('desc') or '') < 600: o['dk'] = list(x['detail'])
            elif x.get('url') and not x.get('desc') and o['via'] in ('api', 'render', 'html', 'page', 'watch'): o['dk'] = ['page', x['url']]
            if (x.get('src') or info['via']) in fresh_src and (not x.get('posted') or x.get('src') in PLUS): o['initial'] = True
            offers[oid] = o; url_idx[o['url']] = o
            new_ids.append(oid)
            own_titles.add(_norm(o['title']) + '|' + country_of(o.get('location') or ''))
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
                if (o.get('last_seen') or '') <= STALE: close(o)
                continue
            if (o.get('last_seen') or '') <= GRACE:  # pas revue depuis 36 h alors que sa source a été lue en entier
                close(o)
      except Exception as e:
        err('structure ' + str(cid), e)
        failed_now.append({'id': cid, 'err': ('plantage : ' + str(e))[:100]})

    # offres déjà enregistrées qui ne passent plus les filtres : FERMÉES (statut « closed », motif gardé), jamais supprimées
    hors_fin_closed = 0
    for o in offers.values():
        if o['status'] != 'open': continue
        c = byid.get(o['company_id']) or {}
        if (o.get('via') in AGG or is_hl(o['company_id'])) and (o.get('last_seen') or '') <= SWEEP:
            close(o)
        elif is_hl(o['company_id']) and not o.get('trust') and not HL_TITLE.search(o.get('title') or ''):
            close(o, 'hors_finance'); hors_fin_closed += 1
        elif not title_ok(c, o):
            close(o, 'hors_finance'); hors_fin_closed += 1
        elif not_offer(o.get('title')):
            close(o, 'pas_une_offre')

    # correctifs des offres déjà enregistrées avant ce passage (une seule fois chacune)
    new_now = set(new_ids)
    for o in offers.values():
        if o['status'] != 'open' or o['id'] in new_now: continue
        c = byid.get(o['company_id']) or {}
        # ville du siège recopiée comme lieu du poste (ancienne règle) : effacée, la fiche est relue pour trouver le vrai lieu
        hq = clean(c.get('city') or '')
        if not o.get('loc_fix') and hq and clean(o.get('location')) == hq and (re.search(r',|/| et | and ', hq) or o.get('via') in ('page', 'html', 'watch', 'render')):
            o['location'] = ''; o['loc_fix'] = 1
            if o.get('url', '').startswith('http') and not o.get('dk'): o['dk'] = reread_url(o) or ['page', o['url']]
        # date de début égale au mois de la date limite : souvent la date limite lue comme début -> relue une fois
        if not o.get('start_fix') and o.get('start') and o.get('deadline') and o['start'] == str(o['deadline'])[:7] and o.get('via') != 'wttj_search':
            o['start'] = None; o['start_fix'] = 1; o.pop('reread2', None)

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

    # deuxième rattrapage : toute offre ouverte sans durée ou sans début est relue une fois, avec la plateforme déduite de son adresse
    for o in offers.values():
        if o['status'] != 'open' or (o.get('months') and o.get('start')) or o.get('dk') or o.get('reread2') or o.get('via') == 'efc': continue
        k = reread_url(o) or reread_key(o, ats_of.get((o['company_id'], o.get('via'))))
        if k: o['dk'] = k
        o['reread2'] = 1

    # fiches détaillées : nouvelles offres d'abord, puis rattrapage des anciennes pas encore enrichies
    new_set = set(new_ids)
    need = [offers[i] for i in new_ids if offers[i].get('dk') and not offers[i].get('initial')]
    need += [o for o in offers.values() if o.get('dk') and (o['id'] not in new_set or o.get('initial')) and o['status'] == 'open']
    need = need[:MAX_DETAILS]

    def fill(o):
        try:
            kind, u = o['dk']; txt, dl, loc, posted = detail_text(kind, u)
            o['_desc'] = txt; o['deadline'] = o.get('deadline') or dl
            loc = loc or location_of(txt)
            if loc and not o['location']: o['location'] = clean(loc)[:120]
            if posted and not o.get('posted') and kind != 'workday': o['posted'] = posted
            o.pop('dk', None)
        except Exception:
            o['dk_fail'] = o.get('dk_fail', 0) + 1
            if o['dk_fail'] >= 3: o.pop('dk', None)
    try:
        with ThreadPoolExecutor(WORKERS) as ex: list(ex.map(fill, need))
    except Exception as e:
        err('fiches détaillées', e)

    def enrich(o):
        c = byid.get(o['company_id'], {}); d = o.pop('_desc', '') or ''; com = o.pop('_commit', '') or ''
        ct = contract_of(o['title'] + ' ' + com)
        if not (o.get('contract') and ct == 'Stage' and not com): o['contract'] = ct  # relecture : garder le contrat déjà trouvé
        o['domain'] = domain_of(o['title'], d, c.get('fam'))
        # durée et début : seulement s'ils sont écrits (titre, texte, champs de la plateforme) ; sinon null, jamais devinés
        o['months'] = months_of(o['title']) or months_of(d) or o.get('months') or None
        if o['months'] and o['months'] > 24: o['months'] = None
        old_start = o.get('start') if (o.get('start') or '') >= NOW.strftime('%Y-%m') else None
        o['start'] = start_of(o['title']) or start_of(d, loose=False) or old_start or None
        dl = o.get('deadline')
        o['deadline'] = (str(dl)[:10] if dl and re.match(r'20\d{2}-\d{2}-\d{2}', str(dl)) else None) or deadline_of(d)
        if o['start'] and o['deadline'] and o['start'] == o['deadline'][:7] and not (start_of(o['title']) or start_of(d, loose=False)):
            o['start'] = None  # le « début » gardé n'était que le mois de la date limite
        if not o['start'] and (start_asap(o['title']) or start_asap(d)): o['start_asap'] = 1
        else: o.pop('start_asap', None)
        if cesure_of(o['title'] + ' ' + d[:4000]): o['cesure'] = 1
        # lieu : celui du poste (plateforme, fiche, « Lieu : … » dans le texte) ; la ville du siège n'est jamais recopiée
        if not o.get('location'): o['location'] = location_of(d)
        o['country'] = country_of(o.get('location') or '')
        o.pop('country_hq', None)
        if not o['country']:
            hc = home_countries(c) if c else set()
            if len(hc) == 1: o['country'] = next(iter(hc)); o['country_hq'] = 1  # pays du siège, signalé comme tel
        if d: o['excerpt'] = d[:500]
    for o in list(offers.values()):
        if o['id'] in new_set or '_desc' in o:
            try: enrich(o)
            except Exception as e:
                o.pop('_desc', None); o.pop('_commit', None); err('enrichissement ' + str(o.get('id')), e)

    limit = (NOW - dt.timedelta(days=CLOSED_KEEP_DAYS)).isoformat()
    offers = {k: o for k, o in offers.items() if not (o['status'] == 'closed' and (o.get('closed_at') or '') < limit)}
    for o in offers.values():  # champs de travail jamais écrits dans le fichier
        o.pop('_desc', None); o.pop('_commit', None)
    # ordre du fichier : moment de détection par le robot (first_seen), plus récentes en haut
    out = sorted(offers.values(), key=lambda o: (o.get('first_seen') or '', o.get('posted') or '', o['id']), reverse=True)

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
    scanned = meta['companies_scanned'] or 1
    meta.update({'errors': len(ERRS), 'error_list': ERRS[:40], 'failed_sources': sorted(x['id'] for x in failed_now)[:400],
                 'failed_sources_count': len(failed_now), 'failed_sources_sample': failed_now[:25],
                 'error_rate': round(len(failed_now) / scanned, 3), 'hors_finance_closed_this_run': hors_fin_closed})
    meta['degraded'] = bool(ERRS) or meta['error_rate'] > ERR_RATE_MAX
    state['known_src'] = sorted(known); state['fails'] = fails
    if RENDER: state['last_render'] = NOW_ISO
    if EXTRA: state['last_extra'] = NOW_ISO
    changed = json.dumps(out, sort_keys=True, ensure_ascii=False) != old_snap
    save('offers.json', {'updated': NOW_ISO if changed else old_doc.get('updated', NOW_ISO), 'offers': out})
    save('meta.json', meta, pretty=True)
    save('state.json', state)
    m2 = dict(meta); m2.pop('tracked'); m2['failed'] = len(failed)
    for k in ('failed_sources', 'failed_sources_sample', 'error_list'): m2.pop(k, None)
    print(json.dumps(m2, ensure_ascii=False, indent=1))
    for e in ERRS[:10]: print('erreur', e['phase'], '-', e['err'])


if __name__ == '__main__':
    main()
