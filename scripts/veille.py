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
import json, re, os, sys, time, hashlib, asyncio, statistics, threading, datetime as dt
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED
from collections import Counter
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from commun import *  # noqa
from taxonomie import metier_of, front_of, famx_of, langs_of, contact_of, norm as tnorm

SCHEMA = 2
DATES_V = 4  # version des règles de dates : à chaque changement, les offres sans date écrite sont relues une fois
ROBOT_V = 3  # version des règles de classement : à chaque changement, toutes les offres gardées sont reclassées une fois (sans remise à zéro)
WORKERS = int(os.environ.get('WORKERS', '32'))
MAX_DETAILS = int(os.environ.get('MAX_DETAILS', '800'))
FAST_BUDGET_S = float(os.environ.get('FAST_BUDGET_S', '230'))  # lecture des structures
SCAN_MAX_S = float(os.environ.get('SCAN_MAX_S', '0')) or min(60.0, FAST_BUDGET_S / 3)  # délai maximal d'une structure : au-delà son fil est rendu aux autres
CUT_MAX = int(os.environ.get('CUT_MAX', '3'))  # coupures de suite avant de passer au job lent
BEHIND_MAX = float(os.environ.get('BEHIND_MAX', '0.25'))  # part de structures non lues au-delà de laquelle le passage est dégradé
DETAILS_BUDGET_S = max(float(os.environ.get('DETAILS_BUDGET_S', '35')), 90)  # fiches détaillées, budget à part (90 s au moins : relecture des offres sans date écrite)
LINKS_BUDGET_S = float(os.environ.get('LINKS_BUDGET_S', '20'))  # liens des agrégateurs, budget à part
YEAR_CLOSE = os.environ.get('YEAR_CLOSE', '1') == '1'  # 0 : plus de fermeture « année passée », les fermetures récentes sont annulées
YEAR_REVERT_DAYS = int(os.environ.get('YEAR_REVERT_DAYS', '7'))  # une fermeture « année passée » que la règle ne confirme plus est annulée pendant ce délai
DL_REOPEN_DAYS = int(os.environ.get('DL_REOPEN_DAYS', '3'))  # offre fermée par sa date limite, revue en ligne sur ce nombre de jours : la date était mal lue
LINK_MAX = int(os.environ.get('LINK_MAX', '120'))  # liens d'agrégateurs testés par passage
LINK_DAYS = int(os.environ.get('LINK_DAYS', '3'))  # un lien n'est retesté qu'après ce délai
LITE_CLOSED_DAYS = int(os.environ.get('LITE_CLOSED_DAYS', '30'))  # offres fermées gardées dans offers-lite.json
SWEEP_LENT_H = float(os.environ.get('SWEEP_LENT_H', '48'))  # une source sans cycle complet depuis ce délai ne ferme rien pour « non revue »
RENDER = os.environ.get('RENDER', '') == '1'
EXTRA = os.environ.get('EXTRA', '') == '1'  # passage étendu : sociétés hors liste
AGGR = os.environ.get('AGGR', '1') == '1'  # agrégateurs (Indeed, Glassdoor, Google, France Travail), par tranches à chaque passage
FAIL_N = int(os.environ.get('FAIL_N', '36'))  # échecs de suite avant « à revoir » (36 passages de 5 min = 3 h)
RENDER_CONC = int(os.environ.get('RENDER_CONC', '6'))
T0 = time.time()
FAST = ('ats', 'api', 'html', 'wttj')
CLOSED_KEEP_DAYS = int(os.environ.get('CLOSED_KEEP_DAYS', '30'))  # offres fermées gardées (statut « closed ») avant d'être retirées du fichier
ERR_RATE_MAX = float(os.environ.get('ERR_RATE_MAX', '0.5'))  # part de sources en échec au-delà de laquelle le passage est « dégradé »
LEFTOVER = [False]  # des lectures tournent encore à la fin du budget : le processus sort sans les attendre
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
    """Intitulé gardé pour cette structure ? (les sources « trust », 100 % finance, échappent à l'intitulé finance exigé,
    pas au filtre hors finance ; le réseau bancaire de détail est écarté partout)"""
    t = x.get('title') or ''
    if reseau_detail(t): return False
    if x.get('trust'): return not hors_finance(t)
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
from commun import STOP_TOK
from sources import AGG, PLUS, HL_TITLE, LEGAL, country_ok, is_hl, merge_annuaire, merge_dedies, hors_liste, merge_results, home_countries
from agregateurs import run_aggregators
from commun import _norm
import ia


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
    m = re.search(r'efinancialcareers\.[a-z.]+/.*\.id(\d+)', u)
    if m: return ['efc', m.group(1) + '|' + (o.get('title') or '')]
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


def dates_text(offers):
    """À chaque passage, pour chaque offre ouverte : début et durée relus dans le titre et les passages datés gardés (sans recharger la fiche)."""
    for o in offers.values():
        if o['status'] != 'open': continue
        src = o.get('ctx') or o.get('excerpt') or ''
        if not o.get('months') or o.get('months_src') == 'programme':
            m = months_with_title(o['title'], src)
            if m and m <= 24: o['months'] = m; o['months_src'] = 'texte'
        if not o.get('start') or o.get('start_src') in ('programme', 'saison'):
            s = start_with_title(o['title'], src)
            if s: o['start'] = s; o['start_src'] = 'texte'; o.pop('start_asap', None)
            elif not o.get('start') and (start_asap(o['title']) or start_asap(src)): o['start_asap'] = 1
            elif not o.get('start'):  # saison écrite (« Summer 2027 ») : mois de début habituel, marqué « saison »
                ss, _lab = season_of(o['title'] + ' . ' + src)
                if ss and ss >= NOW.strftime('%Y-%m'): o['start'] = ss; o['start_src'] = 'saison'
        if o.get('start') and o['start'] < NOW.strftime('%Y-%m') and o.get('start_src') in ('programme', 'saison'): o['start'] = None
    # fiches lues au navigateur (job « nav » : eFinancialCareers, Welcome to the Jungle, Indeed, et lien officiel de l'employeur)
    nav = load('dates_nav.json', {}) or {}
    for oid, r in nav.items():
        o = offers.get(oid)
        if not o or o['status'] != 'open' or not isinstance(r, dict) or not r.get('ok'): continue
        if r.get('ctx') and len(r['ctx']) > len(o.get('ctx') or ''): o['ctx'] = r['ctx']  # passages datés complets, relus aussi par le modèle local
        weak = not o.get('start') or o.get('start_src') in ('programme', 'saison', 'ia')
        if r.get('start') and r['start'] >= NOW.strftime('%Y-%m') and weak and not o.get('start_asap'):
            o['start'] = r['start']; o['start_src'] = 'texte'
        elif r.get('asap') and weak:
            o['start'] = None; o.pop('start_src', None); o['start_asap'] = 1
        if r.get('months') and (not o.get('months') or o.get('months_src') in ('programme', 'ia')):
            o['months'] = r['months']; o['months_src'] = 'texte'
        o['full'] = 1; o.pop('dk', None); o.pop('dk_wait', None)  # fiche complète lue au navigateur : « non précisé » devient fiable
    # dates lues par le modèle local (job « dates ») : seulement là où rien d'écrit n'a été trouvé, ou à la place d'une estimation
    lu = load('dates_ia.json', {}) or {}
    for oid, r in lu.items():
        o = offers.get(oid)
        if not o or o['status'] != 'open' or not isinstance(r, dict): continue
        if r.get('start') and r['start'] >= NOW.strftime('%Y-%m') and (not o.get('start') or o.get('start_src') in ('programme', 'saison')) and not o.get('start_asap'):
            o['start'] = r['start']; o['start_src'] = 'ia'
        elif r.get('asap') and (not o.get('start') or o.get('start_src') in ('programme', 'saison')):
            o['start'] = None; o.pop('start_src', None); o['start_asap'] = 1
        if r.get('months') and (not o.get('months') or o.get('months_src') == 'programme'):
            o['months'] = r['months']; o['months_src'] = 'ia'


def dates_finish(offers):
    """Après l'IA : dates conventionnelles des programmes (summer…), puis état « non précisé dans l'offre » quand la fiche a été lue en entier."""
    for o in offers.values():
        if o['status'] != 'open': continue
        if not (o.get('start') or o.get('start_asap')) or not o.get('months'):
            ps, pm = programme_dates(o.get('title'), o.get('contract'))
            if ps and not (o.get('start') or o.get('start_asap')): o['start'] = ps; o['start_src'] = 'programme'
            if pm and not o.get('months'): o['months'] = pm; o['months_src'] = 'programme'
        lu = o.get('full') and not o.get('dk') and not o.get('dk_wait')  # fiche complète lue (un résumé d'API ou une fiche illisible ne suffit pas)
        for k, ok in (('start_na', o.get('start') or o.get('start_asap')), ('months_na', o.get('months'))):
            if not ok and lu: o[k] = 1
            else: o.pop(k, None)


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
    if k == 'sitemap':
        return j_sitemap(c, m)
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
            info.setdefault('partial', []).append(m_key(m))  # recette à plusieurs pages : une page en panne, la source n'a pas été lue en entier
    if not meths: info['skip'] = True
    elif not info['ran'] and not info['complete']: info['skip'] = True  # que du rendu : traité au passage quotidien
    return raw, info


async def render_all(items, budget=None):
    """Passage quotidien : ouvre dans un navigateur les sites qui n'affichent leurs offres qu'avec JavaScript. Budget en secondes : le reste attend."""
    t0 = time.time()
    from playwright.async_api import async_playwright
    from decouverte import visit, launch, new_ctx
    res = {}
    sem = asyncio.Semaphore(RENDER_CONC)
    async with async_playwright() as pw:
        browser = await launch(pw)

        async def one(c, m):
            async with sem:
                if budget and time.time() - t0 > budget: return
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


def merge_render(results, rr):
    """Ajoute aux résultats les pages lues au navigateur ({id: [(offres, erreur), ...]})."""
    for cid, lst in rr.items():
        raw, info = results.get(cid) or ([], {'id': cid, 'ok': False, 'via': '', 'err': '', 'ran': [], 'complete': True})
        info.pop('skip', None)
        for got, e in lst:
            if e is None:
                raw += got; info['ok'] = True; info['ran'].append('render'); info['via'] = info['via'] or 'render'
            else:
                info['err'] = 'rendu: ' + e
        info['complete'] = True
        results[cid] = (raw, info)


def consume_lent(state, results, structs, byid):
    """Reprend data/lent.json (hors liste, sources plus, rendu navigateur) s'il est nouveau. Renvoie sa date, ou None."""
    doc = load('lent.json')
    if not isinstance(doc, dict) or not doc.get('updated') or doc['updated'] == state.get('lent_seen'): return None
    pseudo = [p for p in doc.get('structs') or [] if isinstance(p, dict) and p.get('id') and p.get('name')]
    ids = set(byid) | {p['id'] for p in pseudo}
    agg = {cid: (v[0], v[1]) for cid, v in (doc.get('results') or {}).items()
           if cid in ids and isinstance(v, list) and len(v) == 2 and isinstance(v[0], list) and isinstance(v[1], dict)}
    for i in agg.values():
        i[1].setdefault('ran', []); i[1].setdefault('ok', False); i[1].setdefault('via', ''); i[1].setdefault('err', ''); i[1].setdefault('complete', True)
    rr = {cid: [(g, e) for g, e in v] for cid, v in (doc.get('render') or {}).items() if cid in ids}
    cycles = doc.get('cycles')
    for p in pseudo:
        if p['id'] not in byid:
            structs.append(p); byid[p['id']] = p
    merge_results(results, agg)
    merge_render(results, rr)
    dom = doc.get('domaines')
    if isinstance(dom, dict) and dom:
        cache = load('domaines.json') or {}
        cache.update(dom)
        save('domaines.json', cache)
    state['lent_seen'] = doc['updated']
    state['last_full'] = doc['updated']
    if isinstance(cycles, dict): state.setdefault('cycles', {}).update(cycles)  # sources réellement lues en entier, avec leur date
    cut, only = state.get('cut') or {}, set(state.get('lent_only') or [])
    for cid in doc.get('fast_ok') or []:  # redevenue rapide au passage lent : retour dans le rapide
        cut.pop(cid, None); only.discard(cid)
    state['cut'], state['lent_only'] = cut, sorted(only)
    return doc['updated']


def hours_since(ts):
    try: return (NOW - dt.datetime.fromisoformat(str(ts).replace('Z', '+00:00'))).total_seconds() / 3600
    except Exception: return 1e9


def set_country(o, c):
    """Pays du poste d'après son lieu ; sinon pays du siège, signalé comme tel."""
    if o.get('country_hq') and not c and not country_of(o.get('location') or ''): return  # structure pas chargée à ce passage : pays du siège gardé
    o['country'] = country_of(o.get('location') or '')
    o.pop('country_hq', None)
    if not o['country']:
        hc = home_countries(c) if c else set()
        if len(hc) == 1: o['country'] = next(iter(hc)); o['country_hq'] = 1


def classify(o, text=''):
    """Champs déduits : type de structure, métier, front office, langues, e-mail de recrutement."""
    fx = famx_of(o)
    if fx: o['famx'] = fx
    else: o.pop('famx', None)
    if o.get('metier_src') != 'ia':
        o['metier'] = metier_of(o)
        if front_of(o): o['front'] = 1
        else: o.pop('front', None)
    if text or not o.get('langs'):
        lg = langs_of(o.get('title'), text)
        if lg: o['langs'] = lg
    ct = contact_of(text)
    if ct: o['contact'] = ct


def filter_raw(c, cid, raw, ann_keys=()):
    """Lignes brutes d'une structure qui passent les filtres (stage, boîte, intitulé, pays). Utilisé aussi par lent.py."""
    raw = [x for x in raw if keep_offer(x) and (x.get('trust') or not foreign(c, x['url']))]
    if c.get('fam') in ('corp', 'consulting', 'fintech', '', None):
        raw = [x for x in raw if x.get('trust') or FIN_TITLE.search(x.get('title') or '') or (c.get('fam') == 'consulting' and re.search(r'consult', x.get('title') or '', re.I))]
    raw = [x for x in raw if title_ok(c, x)]  # Corporate VC : intitulé finance exigé ; grands groupes : postes hors finance écartés
    raw = [x for x in raw if (cid, x.get('src')) not in ann_keys or country_ok(c, x.get('location'))]
    if is_hl(cid):
        raw = [x for x in raw if x.get('trust') or (country_of(x.get('location') or '') and HL_TITLE.search(x.get('title') or ''))]
    return raw


def company_key(o):
    """Regroupement entre structures seulement si le nom complet normalisé est identique, et jamais sur un nom fait de mots génériques."""
    name = _norm(o.get('company'))
    words = [_norm(w) for w in re.split(r"[\s&()/,.'’-]+", LEGAL.sub(' ', o.get('company') or '')) if _norm(w)]
    if len(name) < 4 or not [w for w in words if len(w) >= 3 and w not in STOP_TOK]: return 'id:' + str(o.get('company_id'))
    return 'nom:' + name


def dup_key(o):
    """Même boîte + même contrat + titre normalisé (sans le lieu ni « H/F ») + pays."""
    t = tnorm(o.get('title'))
    t = re.sub(r"\(?\b(?:h\s*/\s*f|f\s*/\s*h|m\s*/\s*f|f\s*/\s*m|m\s*/\s*w\s*/\s*d|w\s*/\s*m\s*/\s*d)\b\)?", ' ', t)
    for w in re.findall(r"[a-z]{3,}", tnorm(o.get('location'))):
        t = re.sub(r'\b' + w + r'\b', ' ', t)
    return (company_key(o), o.get('contract') or '', _norm(t) or _norm(o.get('title')), o.get('country') or '')


def split_terms(g):
    """Sépare les offres dont la durée ou le début, connus des deux côtés, diffèrent."""
    out = []
    for o in sorted(g, key=lambda o: o['id']):
        for b in out:
            if all((not o.get(k) or not x.get(k) or o[k] == x[k]) for x in b for k in ('months', 'start')):
                b.append(o); break
        else:
            out.append([o])
    return out


def group_dups(offers, new_ids=()):
    """Regroupe les offres ouvertes identiques : une principale (avec « cities »), les autres avec « dup_of ». Rien n'est supprimé.
    La principale ne change pas quand une offre arrive : les marques posées sur le site restent au bon endroit."""
    groups = {}
    for o in offers.values():
        if o.get('status') != 'open': continue
        if not o.get('location'):  # sans lieu, rien ne dit que c'est la même offre
            o.pop('dup_of', None); o.pop('cities', None)
            continue
        groups.setdefault(dup_key(o), []).append(o)
    n = 0
    for g in [b for g0 in groups.values() for b in split_terms(g0)]:
        ids = {o['id'] for o in g}
        was_main = {o['dup_of'] for o in g if o.get('dup_of') in ids}
        main = min(g, key=lambda o: (o['id'] not in was_main, o['id'] in new_ids, bool(o.get('dup_of')), is_hl(o['company_id']), o.get('via') in AGG, o.get('first_seen') or '', o['id']))
        cities = []
        for o in g:
            o.pop('dup_of', None); o.pop('cities', None)
            v = clean(o.get('location')).split(',')[0].strip()
            if v and v not in cities: cities.append(v)
        if len(g) == 1: continue
        for o in g:
            if o is main: continue
            o['dup_of'] = main['id']; n += 1
            for k in ('months', 'start', 'contact', 'excerpt'):
                if o.get(k) and not main.get(k): main[k] = o[k]
        if len(cities) > 1: main['cities'] = cities[:30]
    return n


def check_links(offers, close):
    """Offres d'agrégateurs : le lien répond-il encore ? (404, 410, page expirée = fermée). Budget par passage, les plus anciennes vérifications d'abord."""
    if LINK_MAX <= 0: return 0
    limit = (NOW - dt.timedelta(days=LINK_DAYS)).date().isoformat()
    todo = [o for o in offers.values() if o['status'] == 'open' and o.get('via') in AGG and (o.get('link_ck') or '') <= limit and str(o.get('url')).startswith('http')]
    todo.sort(key=lambda o: (o.get('link_ck') or '', o.get('last_seen') or ''))
    todo = todo[:LINK_MAX]
    denied, t0 = Counter(), time.time()

    def one(o):
        h = host_of(o['url'])
        if denied[h] >= 3 or time.time() - t0 > LINKS_BUDGET_S: return o, None  # site qui refuse les robots : on arrête pour ce passage
        st = link_state(o['url'])
        if st == 'blocked': denied[h] += 1
        return o, st
    dead = 0
    with ThreadPoolExecutor(16) as ex:
        for o, st in ex.map(one, todo):
            if st is None: continue
            o['link_ck'] = NOW.date().isoformat()
            if st == 'dead': close(o, 'lien_mort'); dead += 1
    return dead


LITE_DROP = ('excerpt', 'reread', 'reread2', 'txt2', 'loc_fix', 'start_fix', 'dk', 'dk_fail', 'url_agg', 'link_ck', 'miss', 'ia', 'metier_src', 'contract_src', 'trust',
             'dl_days', 'dl_day', 'dl_no', 'dk_wait', 'dk_round', 'bad_fix', 'ctx', 'ctx_rr', 'ia_d', 'page_rr', 'full', 'rr_v')


def write_lite(out, updated):
    """offers-lite.json : ce que le site charge (sans extraits ni doublons, ouvertes + fermées récentes)."""
    limit = (NOW - dt.timedelta(days=LITE_CLOSED_DAYS)).isoformat()
    rows = [{k: v for k, v in o.items() if k not in LITE_DROP} for o in out
            if not o.get('dup_of') and (o['status'] == 'open' or (o.get('closed_at') or '') >= limit)]
    # table {id d'un doublon: id de sa principale} : le site y retrouve les marques posées sur une offre devenue doublon
    ids = {o['id'] for o in rows}
    dups = {o['id']: o['dup_of'] for o in out if o.get('dup_of')}
    table = {}
    for i, m in dups.items():
        seen = {i}
        while m in dups and m not in seen:  # principale devenue doublon à son tour
            seen.add(m); m = dups[m]
        if m in ids: table[i] = m
    save('offers-lite.json', {'updated': updated, 'offers': rows, 'dup_of': table})


def write_details(out):
    """details/<ab>.json = {id: {excerpt, contact}} ; seuls les fichiers qui changent sont réécrits."""
    shards = {}
    for o in out:
        if o.get('excerpt') or o.get('contact'):
            shards.setdefault(o['id'][:2], {})[o['id']] = {'excerpt': o.get('excerpt') or '', 'contact': o.get('contact') or ''}
    d = os.path.join(DATA, 'details')
    have = set()
    for f in (os.listdir(d) if os.path.isdir(d) else ()):
        if f.endswith('.json'): have.add(f[:-5])
        elif f.endswith('.tmp'): os.remove(os.path.join(d, f))  # reste d'une ancienne écriture
    for k in sorted(set(shards) | have):
        cur = shards.get(k, {})
        if load(f'details/{k}.json') != cur: save(f'details/{k}.json', cur)


def write_hist(out):
    """historique.json = {company_id: [{t, c, o, f}]} : jamais purgé, même quand l'offre sort de offers.json."""
    p = os.path.join(DATA, 'historique.json')
    h = load('historique.json')
    if h is None and os.path.exists(p) and os.path.getsize(p) > 0:
        raise RuntimeError('historique.json illisible : laissé tel quel')
    h = h or {}
    by_id = {e['i']: e for lst in h.values() for e in lst if e.get('i')}
    legacy = {(cid, e.get('t'), e.get('o')): e for cid, lst in h.items() for e in lst if not e.get('i')}
    for o in sorted(out, key=lambda o: (o.get('first_seen') or '', o['id'])):
        e = by_id.get(o['id']) or legacy.get((o['company_id'], o.get('title'), o.get('first_seen')))
        if e is None:
            if o.get('dup_of'): continue
            e = {'t': o.get('title'), 'c': o.get('contract') or '', 'o': o.get('first_seen'), 'f': None}
            h.setdefault(o['company_id'], []).append(e)
        e['i'] = o['id']; by_id[o['id']] = e  # le titre ou la date peuvent changer, pas l'identifiant
        e['t'] = o.get('title')
        e['c'] = o.get('contract') or e.get('c') or ''
        e['f'] = o.get('closed_at') if o['status'] == 'closed' else None
    save('historique.json', h)


def delay_stats(state, new_rows):
    """Délai entre la publication et la détection, par source : nouvelles offres seulement (300 dernières par source)."""
    w = state.setdefault('delays', {})
    for o in new_rows:
        if o.get('initial') or not o.get('posted') or o.get('status') != 'open': continue
        try:
            a = dt.datetime.fromisoformat(o['first_seen'].replace('Z', '+00:00')); b = dt.datetime.fromisoformat(o['posted'].replace('Z', '+00:00'))
        except Exception:
            continue
        w.setdefault(o.get('via') or '?', []).append(round(max(0, (a - b).total_seconds() / 3600), 1))
    for k in w: w[k] = w[k][-300:]
    return {k: {'n': len(v), 'median_h': round(statistics.median(v), 1)} for k, v in w.items() if v}


def stays_closed(o):
    """Fermée pour date limite passée ou année passée : revoir l'annonce en ligne ne la rouvre pas (sauf date limite repoussée,
    ou annonce revue plusieurs jours après sa fermeture : la date était mal lue)."""
    if o.get('status') != 'closed': return False
    if o.get('closed_reason') == 'annee_passee': return True
    if o.get('closed_reason') != 'deadline' or str(o.get('deadline') or '')[:10] >= NOW.date().isoformat(): return False
    day = NOW.date().isoformat()
    if o.get('dl_day') != day:
        o['dl_day'] = day; o['dl_days'] = o.get('dl_days', 0) + 1
    if o['dl_days'] < DL_REOPEN_DAYS: return True
    o['dl_no'] = str(o.get('deadline'))[:10]; o['deadline'] = None  # date écartée : ne referme plus l'offre, relecture comprise
    o.pop('dl_day', None); o.pop('dl_days', None)
    return False


def main():
    structs = load('structures.json')
    seeds_all = {c['id']: c for c in (load('companies.json') or [])}
    if not structs: structs = list(seeds_all.values())
    recettes = load('recettes.json') or {}
    ann = load('annuaire.json') or {}
    ann_keys = {(cid, m.get('k')) for cid, ms in ann.items() for m in ms}
    recettes = merge_annuaire(recettes, ann)
    recettes = merge_dedies(recettes, load('dedies.json'))
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
    state['schema'] = SCHEMA  # jamais de remise à zéro : un state.json absent ou ancien ne doit pas effacer les offres
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
    behind = set(state.get('behind') or [])
    cut = dict(state.get('cut') or {})
    lent_only = set(state.get('lent_only') or [])  # structures trop lentes pour ce passage : lues par lent.py
    todo = [c for c in todo if c['id'] not in lent_only]
    # d'abord ce qui n'a pas pu démarrer au passage précédent ; les structures coupées passent après toutes les autres
    todo.sort(key=lambda c: (cut.get(c['id'], 0), c['id'] not in behind))

    results = {}
    sem, lock, started, freed, overrun = threading.BoundedSemaphore(WORKERS), threading.Lock(), {}, set(), set()

    def free(cid):
        with lock:
            if cid in freed: return
            freed.add(cid)
        sem.release()

    def timed(c):
        sem.acquire()
        cid = c['id']
        if time.time() - T0 > FAST_BUDGET_S:  # trop tard : jamais démarrée
            sem.release()
            return None
        started[cid] = time.time()
        try: return scan(c, recettes.get(cid), seeds_all.get(cid, {}))
        finally: free(cid)

    def take(f):
        c = futs[f]
        try:
            r = f.result()
            if r is not None: results[c['id']] = r
        except Exception as e:
            results[c['id']] = ([], {'id': c['id'], 'ok': False, 'err': str(e)[:80], 'ran': [], 'complete': True})

    # le double de fils : un fil coupé reste occupé (Python ne sait pas l'arrêter), le sémaphore limite ceux qui travaillent vraiment
    ex = ThreadPoolExecutor(WORKERS * 2)
    futs = {ex.submit(timed, c): c for c in todo}
    pending = set(futs)
    while pending and time.time() - T0 < FAST_BUDGET_S:
        done, pending = wait(pending, timeout=min(0.25, max(0.01, FAST_BUDGET_S - (time.time() - T0))), return_when=FIRST_COMPLETED)
        for f in done: take(f)
        now = time.time()
        for cid, t in list(started.items()):
            if now - t > SCAN_MAX_S and cid not in freed:
                overrun.add(cid); free(cid)
    done_late = [f for f in pending if f.done()]
    for f in done_late: take(f); pending.discard(f)
    if pending: LEFTOVER[0] = True
    ex.shutdown(wait=False, cancel_futures=True)
    now = time.time()
    for f in pending:
        cid = futs[f]['id']
        if cid in started and now - started[cid] > SCAN_MAX_S: overrun.add(cid)
    for c in todo:
        cid = c['id']
        if cid in overrun:
            cut[cid] = cut.get(cid, 0) + 1
            if cut[cid] >= CUT_MAX: lent_only.add(cid)
        elif cid in results: cut.pop(cid, None)
    state['cut'], state['lent_only'] = cut, sorted(lent_only)
    state['behind'] = sorted(c['id'] for c in todo if c['id'] not in results and c['id'] not in lent_only)  # non lues : ni échec ni absence, simplement reportées
    if state['behind']: print(f"budget de lecture atteint : {len(state['behind'])} structures reportées, {len(overrun)} coupées", flush=True)
    t_fast = time.time() - T0
    lent_new = None
    try:
        lent_new = consume_lent(state, results, structs, byid)
        if lent_new: print('passage lent repris :', lent_new, flush=True)
    except Exception as e:
        err('reprise du passage lent', e)
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
        merge_render(results, rr)

    offers = dict(old); new_ids = []
    url_idx = {}
    for o in old.values():
        url_idx[o['url']] = o
        if o.get('url_agg'): url_idx[o['url_agg']] = o  # adresse d'agrégateur remplacée par le lien officiel : l'offre reste reconnue
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
        raw_by_src.update({k: v for k, v in (info.get('raw_n') or {}).items() if not raw_by_src[k]})  # lignes lues avant le tri fait par lent.py
        raw = filter_raw(c, cid, raw, ann_keys)
        live = set()
        own_titles = {_norm(o['title']) + '|' + country_of(o.get('location') or '') for o in offers.values() if o['company_id'] == cid and o['status'] == 'open'}
        for x in raw:
            x['title'] = clean(x['title'])
            direct = x.get('direct') if direct_ok(x.get('direct')) else ''
            prev = url_idx.get(x['url'])
            if not prev and direct:
                prev = url_idx.get(direct)
                if prev and prev.get('via') not in AGG: continue  # déjà lue sur le site de l'entreprise : l'agrégateur n'apporte rien
            if prev and prev['company_id'] != cid: continue
            if not prev and x.get('src') in AGG and _norm(x['title']) + '|' + country_of(x.get('location') or '') in own_titles: continue
            if not prev and past_title(x['title']): continue  # jamais enregistrée : elle serait fermée aussitôt, à chaque retour
            oid = prev['id'] if prev else hashlib.sha1((cid + '|' + str(x.get('ext') or x['url'])).encode()).hexdigest()[:14]
            if oid in live: continue
            live.add(oid)
            if oid in offers:
                o = offers[oid]
                dl = str(x.get('deadline') or '')[:10]
                if re.match(r'20\d{2}-\d{2}-\d{2}$', dl) and dl > str(o.get('deadline') or ''): o['deadline'] = dl
                if stays_closed(o): continue
                o['status'] = 'open'; o.pop('closed_at', None); o.pop('closed_reason', None); o.pop('miss', None)
                if (o.get('last_seen') or '') <= FRESH: o['last_seen'] = NOW_ISO
                if (not o.get('site') or NOLOGO_HOST.search(host_of(o['site']) + '.')) and official_site(c): o['site'] = official_site(c)
                if x.get('logo') and not o.get('logo'): o['logo'] = x['logo']
                if x.get('start') and (not o.get('start') or o.get('start_src') in ('programme', 'saison')):
                    o['start'] = x['start']
                    if x.get('start_src'): o['start_src'] = x['start_src']
                if x.get('months') and not o.get('months'): o['months'] = x['months']
                if x.get('src') in PLUS and x.get('desc') and not o.get('txt2') and not (o.get('months') and o.get('start')):
                    o['_desc'] = x['desc']; o['txt2'] = 1  # texte complet reçu une fois pour retrouver durée et début
                if x.get('src') and (x['src'] not in PLUS or o.get('via') in AGG): o['via'] = x['src']
                if direct and o.get('via') in AGG and o['url'] != direct:  # lien officiel d'une offre connue : l'identifiant ne change pas
                    o.setdefault('url_agg', o['url']); o['url'] = direct; url_idx[direct] = o
                    o.pop('reread', None); o.pop('reread2', None); o.pop('link_ck', None)
                continue
            o = {'id': oid, 'company_id': cid, 'company': c['name'], 'fam': c.get('fam', ''), 'site': official_site(c),
                 'title': x['title'][:200], 'url': direct or x['url'], 'location': clean(x.get('location'))[:120], 'posted': x.get('posted'),
                 'first_seen': NOW_ISO, 'last_seen': NOW_ISO, 'status': 'open', 'via': x.get('src') or info['via'], 'deadline': x.get('deadline'),
                 '_desc': x.get('desc') or '', '_commit': x.get('commit') or ''}
            if direct: o['url_agg'] = x['url']; url_idx[x['url']] = o
            if x.get('logo'): o['logo'] = x['logo']  # logo de l'entreprise fourni par l'agrégateur
            if x.get('start'): o['start'] = x['start']
            if x.get('start_src'): o['start_src'] = x['start_src']
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
    # ces offres ne sont revues que par le passage lent : seule une source lue en entier récemment peut conclure à leur absence
    cycles = state.get('cycles') or {}

    def sweep_ok(o):
        return EXTRA or hours_since(cycles.get(o.get('via') if o.get('via') in AGG else o['company_id'])) < SWEEP_LENT_H
    for o in offers.values():
        if o['status'] != 'open': continue
        c = byid.get(o['company_id']) or {}
        if sweep_ok(o) and (o.get('via') in AGG or is_hl(o['company_id'])) and (o.get('last_seen') or '') <= SWEEP:
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
        # fiche lue alors que le site renvoyait une page de maintenance ou anti-robot : ce texte est effacé et la fiche relue
        if not o.get('bad_fix') and o.get('excerpt') and BAD_PAGE.search(o['excerpt'][:300]):
            o.pop('excerpt', None); o['bad_fix'] = 1
            if not (o.get('months') and o.get('start')) and o.get('url', '').startswith('http') and not o.get('dk'):
                o['dk'] = reread_url(o) or ['page', o['url']]
        # lecture en attente après un blocage : on retente quand le délai est passé
        if o.get('dk_wait') and not o.get('dk') and o['dk_wait'][1] <= NOW_ISO:
            if not (o.get('months') and o.get('start')): o['dk'] = o['dk_wait'][0]
            o.pop('dk_wait', None)

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
        if o['status'] != 'open' or (o.get('months') and o.get('start')) or o.get('dk') or o.get('reread2'): continue
        k = reread_url(o) or reread_key(o, ats_of.get((o['company_id'], o.get('via'))))
        if k: o['dk'] = k
        o['reread2'] = 1

    # troisième rattrapage : offre sans début ou sans durée, lue avant que les passages datés soient gardés -> relue une fois
    for o in offers.values():
        if o['status'] != 'open' or 'ctx' in o or o.get('ctx_rr') or o.get('dk') or o.get('dk_wait'): continue
        if o.get('months') and (o.get('start') or o.get('start_asap')): continue
        k = reread_url(o) or reread_key(o, ats_of.get((o['company_id'], o.get('via'))))
        if k: o['dk'] = k
        else: o['ctx'] = date_ctx(o['title'] + ' . ' + (o.get('excerpt') or ''), 600)
        o['ctx_rr'] = 1

    # quatrième rattrapage : offre sans début dont le texte venait d'une API (WTTJ, eFC, Trackr…) -> la fiche elle-même est lue une fois
    for o in offers.values():
        if o['status'] != 'open' or o.get('page_rr') or o.get('dk') or o.get('dk_wait'): continue
        if (o.get('start') and o.get('start_src') not in ('programme', 'saison')) or o.get('start_asap'): continue
        k = reread_url(o) or (['page', o['url']] if str(o.get('url')).startswith('http') else None)
        if k: o['dk'] = k
        o['page_rr'] = 1

    # cinquième rattrapage (règles de dates v2) : toute offre ouverte sans début écrit ou sans durée, dont la fiche complète n'a pas été lue,
    # est relue une fois (les offres « non précisé » d'avant venaient souvent d'un résumé d'API, pas de la fiche)
    for o in offers.values():
        if o['status'] != 'open' or o.get('rr_v', 0) >= DATES_V or o.get('dk') or o.get('dk_wait'): continue
        written = o.get('start') and o.get('start_src') not in ('programme', 'saison')
        if (written or o.get('start_asap')) and o.get('months') and o.get('months_src') != 'programme': continue
        k = reread_url(o) or reread_key(o, ats_of.get((o['company_id'], o.get('via'))))
        if k: o['dk'] = k
        o['rr_v'] = DATES_V

    # fiches détaillées : nouvelles offres d'abord, puis rattrapage des anciennes pas encore enrichies
    new_set = set(new_ids)
    need = [offers[i] for i in new_ids if offers[i].get('dk') and not offers[i].get('initial')]
    need += sorted((o for o in offers.values() if o.get('dk') and (o['id'] not in new_set or o.get('initial')) and o['status'] == 'open'),
                   key=lambda o: o.get('first_seen') or '', reverse=True)  # les offres récentes d'abord
    need = need[:MAX_DETAILS]

    t_details = time.time()

    def fill(o):
        if time.time() - t_details > DETAILS_BUDGET_S: return  # gardée pour le passage suivant
        try:
            kind, u = o['dk']; txt, dl, loc, posted = detail_text(kind, u)
            o['_desc'] = txt; o['deadline'] = o.get('deadline') or dl
            loc = loc or location_of(txt)
            if loc and not o['location']: o['location'] = clean(loc)[:120]
            if posted and not o.get('posted') and kind != 'workday': o['posted'] = posted
            o.pop('dk', None)
        except Exception:
            o['dk_fail'] = o.get('dk_fail', 0) + 1
            if o['dk_fail'] >= 3:  # site qui bloque pour l'instant : nouvel essai dans 12 h, 4 fois au plus
                if o.get('dk_round', 0) < 4: o['dk_wait'] = [o['dk'], (NOW + dt.timedelta(hours=12)).strftime('%Y-%m-%dT%H:%M:%SZ')]
                o['dk_round'] = o.get('dk_round', 0) + 1; o.pop('dk', None); o.pop('dk_fail', None)
                if o['dk_round'] > 4: o.setdefault('ctx', '')  # fiche illisible : on garde ce que l'API a donné
    try:
        with ThreadPoolExecutor(WORKERS) as ex: list(ex.map(fill, need))
    except Exception as e:
        err('fiches détaillées', e)

    texts = {}

    def enrich(o):
        c = byid.get(o['company_id'], {}); d = o.pop('_desc', '') or ''; com = o.pop('_commit', '') or ''
        if d: texts[o['id']] = d
        ct = contract_of(o['title'] + ' ' + com)
        if o.get('contract_src') != 'ia' and not (o.get('contract') and ct == 'Stage' and not com): o['contract'] = ct  # relecture : garder le contrat déjà trouvé
        o['domain'] = domain_of(o['title'], d, c.get('fam'))
        # durée et début : seulement s'ils sont écrits (titre, texte, champs de la plateforme) ; sinon null, jamais devinés
        o['months'] = months_with_title(o['title'], d) or o.get('months') or None
        if o['months'] and o['months'] > 24: o['months'] = None
        old_start = o.get('start') if (o.get('start') or '') >= NOW.strftime('%Y-%m') else None
        found = start_with_title(o['title'], d)
        if found and found != o.get('start'): o['start_src'] = 'texte'  # date écrite : remplace une saison ou une estimation
        o['start'] = found or old_start or None
        dl = o.get('deadline')
        o['deadline'] = (str(dl)[:10] if dl and re.match(r'20\d{2}-\d{2}-\d{2}', str(dl)) else None) or deadline_of(d)
        if o['deadline'] and o['deadline'] == o.get('dl_no'): o['deadline'] = None  # date déjà démentie par l'annonce restée en ligne
        if o['start'] and o['deadline'] and o['start'] == o['deadline'][:7] and not start_with_title(o['title'], d):
            o['start'] = None  # le « début » gardé n'était que le mois de la date limite
        if not o['start'] and (start_asap(o['title']) or start_asap(d)): o['start_asap'] = 1
        else: o.pop('start_asap', None)
        if cesure_of(o['title'] + ' ' + d[:4000]): o['cesure'] = 1
        # lieu : celui du poste (plateforme, fiche, « Lieu : … » dans le texte) ; la ville du siège n'est jamais recopiée
        if not o.get('location'): o['location'] = location_of(d)
        set_country(o, c)
        if d: o['excerpt'] = d[:500]; o['ctx'] = date_ctx(o['title'] + ' . ' + d, 1200)  # passages datés, relus à chaque passage (et lus par le modèle local)
        if len(d) >= 300: o['full'] = 1  # fiche complète vraiment lue : seule condition pour afficher « non précisé dans l'offre »
        classify(o, d)
    for o in list(offers.values()):
        if o['id'] in new_set or '_desc' in o:
            try: enrich(o)
            except Exception as e:
                o.pop('_desc', None); o.pop('_commit', None); err('enrichissement ' + str(o.get('id')), e)

    # une fois : toutes les offres gardées repassent par les règles actuelles (pays, métier, type de structure, langues, contact)
    if state.get('robot_v') != ROBOT_V:
        for o in offers.values():
            try:
                set_country(o, byid.get(o['company_id'], {}))
                classify(o, o.get('excerpt') or '')
            except Exception as e:
                err('reclassement ' + str(o.get('id')), e)
        state['robot_v'] = ROBOT_V

    # fermetures : date limite passée, titre qui vise une période finie, lien d'agrégateur mort
    today = NOW.date().isoformat()
    year_closed = year_reopened = 0
    for o in offers.values():
        if o['status'] != 'open': continue
        dl = str(o.get('deadline') or '')[:10]
        if re.match(r'20\d{2}-\d{2}-\d{2}$', dl) and dl < today: close(o, 'deadline')
        elif YEAR_CLOSE and past_title(o.get('title')): close(o, 'annee_passee'); year_closed += 1
    # réversible : une fermeture « année passée » récente que la règle ne confirme plus est annulée
    year_revert = (NOW - dt.timedelta(days=YEAR_REVERT_DAYS)).isoformat()
    for o in offers.values():
        if o.get('closed_reason') == 'annee_passee' and (o.get('closed_at') or '') >= year_revert and not (YEAR_CLOSE and past_title(o.get('title'))):
            o['status'] = 'open'; o.pop('closed_at', None); o.pop('closed_reason', None); year_reopened += 1
    dead_links = 0
    try:
        dead_links = check_links(offers, close)
    except Exception as e:
        err('liens des agrégateurs', e)

    try: dates_text(offers)
    except Exception as e: err('dates (texte)', e)
    # option IA : complète le classement des nouvelles offres et lit début et durée quand les règles n'ont rien trouvé, jamais bloquant
    try:
        ia.run(offers, texts, new_set, state)
    except Exception as e:
        err('classement IA', e)
    try: dates_finish(offers)
    except Exception as e: err('dates (fin)', e)

    n_dups = 0
    try:
        n_dups = group_dups(offers, new_set)
    except Exception as e:
        err('regroupement des doublons', e)

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
            'open': sum(1 for o in out if o['status'] == 'open' and not o.get('dup_of')), 'open_with_dups': sum(1 for o in out if o['status'] == 'open'), 'dups': n_dups,
            'dead_links_this_run': dead_links, 'annee_passee_this_run': year_closed, 'annee_passee_reopened': year_reopened, 'ia': dict(ia.LAST),
            'cut': len(overrun), 'lent_only': len(state.get('lent_only') or []), 'lent_consumed': bool(lent_new), 'last_full': state.get('last_full') or state.get('last_extra'), 'behind': len(state.get('behind') or []),
            'delay_by_source': delay_stats(state, [offers[i] for i in new_ids if i in offers]),
            'new_this_run': sum(1 for i in new_ids if i in offers and not offers[i].get('initial')),
            'initial_this_run': sum(1 for i in new_ids if i in offers and offers[i].get('initial')), 'closed_this_run': closed_now,
            'failed': failed, 'hors_liste_open': sum(1 for o in out if o['status'] == 'open' and is_hl(o['company_id']) and not o.get('dup_of')), 'extra': EXTRA, 'tracked': tracked}
    scanned = meta['companies_scanned'] or 1
    meta.update({'errors': len(ERRS), 'error_list': ERRS[:40], 'failed_sources': sorted(x['id'] for x in failed_now)[:400],
                 'failed_sources_count': len(failed_now), 'failed_sources_sample': failed_now[:25],
                 'error_rate': round(len(failed_now) / scanned, 3), 'hors_finance_closed_this_run': hors_fin_closed})
    tried = sum(1 for _, i in results.values() if not i.get('skip')) + len(state.get('behind') or [])
    meta['blocked'] = bool(tried and not ok_ids)  # rien lu du tout
    meta['behind_rate'] = round(len(state.get('behind') or []) / max(1, len(todo)), 3)
    meta['degraded'] = bool(ERRS) or meta['error_rate'] > ERR_RATE_MAX or meta['blocked'] or meta['behind_rate'] > BEHIND_MAX
    state['known_src'] = sorted(known); state['fails'] = fails
    if RENDER: state['last_render'] = NOW_ISO
    if EXTRA: state['last_extra'] = NOW_ISO
    changed = json.dumps(out, sort_keys=True, ensure_ascii=False) != old_snap
    updated = NOW_ISO if changed else old_doc.get('updated', NOW_ISO)
    save('offers.json', {'updated': updated, 'offers': out})
    # fichiers dérivés d'offers.json : une panne ici ne touche pas les offres enregistrées
    for name, f in (('offers-lite', lambda: write_lite(out, updated)), ('details', lambda: write_details(out)), ('historique', lambda: write_hist(out))):
        try: f()
        except Exception as e: err(name, e)
    meta.update({'errors': len(ERRS), 'error_list': ERRS[:40]})
    meta['degraded'] = bool(meta['degraded'] or ERRS)
    try: meta['lite_bytes'] = os.path.getsize(os.path.join(DATA, 'offers-lite.json'))
    except OSError: pass
    save('meta.json', meta, pretty=True)
    save('state.json', state)
    m2 = dict(meta); m2.pop('tracked'); m2['failed'] = len(failed)
    for k in ('failed_sources', 'failed_sources_sample', 'error_list'): m2.pop(k, None)
    print(json.dumps(m2, ensure_ascii=False, indent=1))
    for e in ERRS[:10]: print('erreur', e['phase'], '-', e['err'])


if __name__ == '__main__':
    main()
    if LEFTOVER[0]:  # lectures reportées encore en cours : tout est enregistré, inutile de les attendre
        sys.stdout.flush(); sys.stderr.flush(); os._exit(0)
