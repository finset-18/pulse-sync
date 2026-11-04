#!/usr/bin/env python3
"""
Sources supplémentaires du robot de veille (lues au passage étendu, environ une fois par heure).

Tout s'ajoute à l'existant : ces lignes suivent le même chemin que les agrégateurs (agregateurs.py), une offre
n'est donc fermée qu'après 8 jours sans réapparaître et une source en panne ne ferme rien.

- trackr : programmes finance UK et US (api.the-trackr.com)
- efc : eFinancialCareers
- wttj_search : recherche Welcome to the Jungle
- talnet : plateformes Oleeo / tal.net (Morgan Stanley, BlackRock)
- janestreet : Jane Street
- simplify : liste publique SimplifyJobs sur GitHub
- muse, arbeitnow : API publiques

Lancé seul (python scripts/plus.py), le script affiche ce que chaque source renvoie et n'écrit rien.
"""
import datetime as dt, os, re, sys, time
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from commun import S, clean, is_intern, iso, country_of, load

WORKERS = int(os.environ.get('PLUS_WORKERS', '8'))
BUDGET = int(os.environ.get('PLUS_BUDGET_S', '420'))  # temps maximum d'une source, en secondes
T = 20
INTERN = 'Internship'
TRACKR_API = 'https://api.the-trackr.com/programmes'
# (région, secteur, saison, type)
TRACKR = [(r, 'Finance', s, t) for s in ('2027', '2028') for r, t in (
    ('UK', 'summer-internships'), ('UK', 'off-cycle-internships'), ('UK', 'spring-weeks'), ('UK', 'industrial-placements'),
    ('US', 'summer-internships'), ('US', 'off-cycle-internships'), ('US', 'spring-weeks'))]
CITY = re.compile(r"\b(paris|london|londres|frankfurt|francfort|munich|luxembourg|zurich|zürich|geneva|gen[èe]ve|brussels|bruxelles|amsterdam|dublin|madrid|milan|"
                  r"new york|hong kong|singapore|singapour|dubai|edinburgh|manchester|birmingham|glasgow|leeds|bristol)\b", re.I)
EFC = 'https://job-search-api.efinancialcareers.com/v1/efc/jobs/search'
EFC_C = ('FR', 'GB', 'LU', 'CH', 'BE', 'DE', 'NL', 'IE', 'ES', 'IT', 'US', 'HK', 'SG', 'AE')
EFC_Q = ('intern', 'stage', 'off-cycle', 'summer analyst', 'graduate')
# eFinancialCareers publie aussi des postes hors métiers de la finance chez ses clients
EFC_NO = re.compile(r"communication|marketing|\bhr\b|human resources|ressources humaines|recruit|talent|legal|juridique|software|developer|d[ée]veloppeur|\bit\b|cyber|design|event|[ée]v[ée]nementiel|"
                    r"engineer|ing[ée]nieur|devops|cloud|product (?:manager|owner)|\bux\b|procurement|achats|supply chain|logisti|facilities|office manager", re.I)
EFC_PAGES = int(os.environ.get('EFC_PAGES', '3'))
ALGOLIA = ('https://csekhvms53-dsn.algolia.net/1/indexes/wttj_jobs_production_fr/query'
           '?x-algolia-application-id=CSEKHVMS53&x-algolia-api-key=4bd8f6215d0cc52b26430765769e65a0')
WTTJ_H = {'Referer': 'https://www.welcometothejungle.com/', 'Origin': 'https://www.welcometothejungle.com'}
WTTJ_Q = ('finance', 'M&A', 'fusions acquisitions', 'private equity', 'capital investissement', 'venture capital', 'banque', "banque d'affaires",
          'investissement', 'asset management', 'gestion de portefeuille', 'gestion de patrimoine', 'marchés financiers', 'trading', 'corporate finance',
          'transaction services', 'analyste financier', 'audit financier', 'trésorerie', 'crédit', 'risques', 'dette', 'financement', 'immobilier investissement')
TAL = (('morganstanley', 1, 'Morgan Stanley'), ('morganstanley', 2, 'Morgan Stanley'), ('blackrock', 1, 'BlackRock'))
JS_CITY = {'NYC': 'New York, United States', 'LDN': 'London, United Kingdom', 'HKG': 'Hong Kong', 'SGP': 'Singapore', 'ATX': 'Austin, United States',
           'CHI': 'Chicago, United States', 'AMS': 'Amsterdam, Netherlands'}
SIMPLIFY = 'https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json'
MUSE = 'https://www.themuse.com/api/public/jobs'


def row(src, company, title, url, location='', posted=None, ext=None, desc='', commit=INTERN, trust=False, logo='', csite='', start=None):
    return {'title': clean(title or ''), 'company': clean(company or ''), 'csite': csite if str(csite).startswith('http') else '', 'url': url, 'location': clean(location or '')[:120], 'posted': posted,
            'ext': src + ':' + str(ext or url), 'desc': clean(desc or '')[:8000], 'commit': commit, 'src': src, 'trust': trust,
            'logo': logo if str(logo).startswith('https://') else '', 'start': start}


def trackr_location(p):
    out = []
    for loc in p.get('locations') or []:
        a = [x.strip() for x in str(loc).split('|') if x.strip()]
        if a:
            country = {'US': 'United States', 'UK': 'United Kingdom', 'GB': 'United Kingdom'}.get(a[0], a[0])
            out.append(a[-1] + ', ' + country if len(a) > 1 else country)
    if out:
        return ' / '.join(out[:3])
    m = CITY.search(p.get('name') or '')
    if m:
        return m.group(1)
    return {'UK': 'United Kingdom', 'US': 'United States'}.get(p.get('region'), '')


def trackr_open(p, today):
    if re.search(r'clos|expired|not.?open|upcoming', str(p.get('status') or ''), re.I):
        return False
    c = str(p.get('closingDate') or '')[:10]
    return not (re.match(r'20\d\d-\d\d-\d\d', c) and c < today)


def s_trackr():
    out, today = [], dt.date.today().isoformat()
    for region, industry, season, kind in TRACKR:
        r = S.get(TRACKR_API, params={'region': region, 'industry': industry, 'season': season, 'type': kind}, timeout=45)
        r.raise_for_status()
        for p in r.json().get('programmes') or []:
            u = p.get('url') or ''
            if not u.startswith('http') or not trackr_open(p, today):
                continue
            co = p.get('company')
            name = (co.get('name') if isinstance(co, dict) else co) or str(p.get('companyId') or '').replace('-', ' ').title()
            info = co if isinstance(co, dict) else {}
            out.append(row('trackr', name, p.get('name'), u, trackr_location(p), iso(p.get('openingDate')), p.get('id'), trust=True,
                           logo=info.get('logo') or info.get('logoUrl') or info.get('imageUrl') or '', csite=info.get('website') or info.get('url') or ''))
    return out


def efc_one(a):
    country, q, page = a
    r = S.get(EFC, params={'q': q, 'countryCode2': country, 'page': page, 'pageSize': 100, 'language': 'en'}, timeout=T)
    r.raise_for_status()
    js = r.json()
    res = []
    for j in js.get('data') or []:
        u, t = j.get('detailsPageUrl') or '', j.get('title') or ''
        text = t + ' ' + (j.get('summary') or '')[:300]
        if not u or EFC_NO.search(t) or not (is_intern(t) or re.search(r'contract type:? *internship|\bstage de\b|\bstagiaire\b', text, re.I)):
            continue
        res.append(row('efc', j.get('companyName') or j.get('clientBrandName'), t, 'https://www.efinancialcareers.com' + u,
                       (j.get('jobLocation') or {}).get('displayName'), iso(j.get('postedDate')), j.get('id'), j.get('description') or j.get('summary'), trust=True,
                       logo=j.get('companyLogoUrl') or j.get('jobAdvertLogoUrl') or ''))
    return res


def s_efc():
    tasks = [(c, q, p) for c in EFC_C for q in EFC_Q for p in range(1, EFC_PAGES + 1)]
    out = []
    t0 = time.time()
    with ThreadPoolExecutor(WORKERS) as ex:
        for got in ex.map(lambda a: _safe(efc_one, a, t0), tasks):
            out += got
    return out


def _safe(f, a, t0):
    if time.time() - t0 > BUDGET:
        return []
    try:
        return f(a)
    except Exception:
        return []


def wttj_one(q):
    res = []
    for page in range(0, 5):
        r = S.post(ALGOLIA, json={'query': q, 'hitsPerPage': 200, 'page': page, 'facetFilters': [['contract_type:internship', 'contract_type:apprenticeship', 'contract_type:vie']]},
                   headers=WTTJ_H, timeout=T)
        r.raise_for_status()
        js = r.json()
        for h in js.get('hits') or []:
            org = h.get('organization') or {}
            if not org.get('slug') or not h.get('slug'):
                continue
            off = (h.get('offices') or [{}])[0] or {}
            ct = h.get('contract_type') or ''
            logo = org.get('logo') or {}
            logo = (logo.get('url') if isinstance(logo, dict) else logo) or ''
            months = h.get('contract_duration_maximum') or h.get('contract_duration_minimum')
            text = ' '.join(str(x) for x in (h.get('summary'), h.get('key_missions'), h.get('profile')) if x) + (f' Durée {months} mois.' if months else '')
            start = str(h.get('start_date') or h.get('starts_at') or '')[:7]
            res.append(row('wttj_search', org.get('name'), h.get('name'), f"https://www.welcometothejungle.com/fr/companies/{org['slug']}/jobs/{h['slug']}",
                           ', '.join(x for x in (off.get('city'), off.get('country')) if x), iso(h.get('published_at')), h.get('slug'), text,
                           'Alternance' if ct == 'apprenticeship' else 'VIE' if ct == 'vie' else INTERN, logo=logo,
                           csite=org.get('website') or org.get('website_url') or '', start=start if re.match(r'20\d\d-\d\d$', start) else None))
        if page + 1 >= (js.get('nbPages') or 0):
            break
    return res


def s_wttj():
    out = []
    t0 = time.time()
    with ThreadPoolExecutor(4) as ex:
        for got in ex.map(lambda q: _safe(wttj_one, q, t0), WTTJ_Q):
            out += got
    return out


TAL_A = re.compile(r'<a\b[^>]*href="([^"]*/opp/(\d+)[^"]*)"[^>]*>(.*?)</a>(.*?)(?=</tr>|<a\b[^>]*/opp/|$)', re.I | re.S)
TAL_NEXT = re.compile(r'href="([^"]*[?&](?:amp;)?start=(\d+)[^"]*)"', re.I)


def tal_parse(html, base):
    """(offres [(id, titre, lieu, url)], adresses des pages suivantes {start: url})"""
    res, seen = [], set()
    for m in TAL_A.finditer(html):
        href, oid, title, tail = m.groups()
        title = clean(re.sub(r'<[^>]+>', ' ', title))
        if not title or oid in seen:
            continue
        seen.add(oid)
        cells = [clean(re.sub(r'<[^>]+>', ' ', c)) for c in re.findall(r'<td\b[^>]*>(.*?)</td>', tail, re.I | re.S)]
        res.append((oid, title, next((c for c in cells if c), ''), href.replace('&amp;', '&')))
    pages = {int(s): h.replace('&amp;', '&') for h, s in TAL_NEXT.findall(html)}
    return res, pages


def tal_board(client, board, company, trust=True):
    from urllib.parse import urljoin
    url, out, done = f'https://{client}.tal.net/candidate/jobboard/vacancy/{board}/adv/', [], set()
    for _ in range(40):
        r = S.get(url, timeout=T)
        r.raise_for_status()
        got, pages = tal_parse(r.text, r.url)
        for oid, title, loc, href in got:
            if oid not in done:
                done.add(oid)
                out.append(row('talnet', company, title, urljoin(r.url, href).split('?')[0], loc, None, client + ':' + oid, commit='', trust=trust))
        nxt = sorted(s for s in pages if s > (len(done) - len(got)))
        if not got or not nxt:
            break
        url = urljoin(r.url, pages[nxt[0]])
    return out


def s_talnet():
    """Comptes tal.net connus, plus ceux récoltés chaque semaine par comptes.py. Un compte protégé renvoie 0 offre."""
    boards = list(TAL)
    for client, name in ((load('comptes.json') or {}).get('tal') or {}).items():
        if not any(b[0] == client for b in TAL):
            boards += [(client, n, name) for n in (1, 2)]
    out = []
    for client, board, company in boards:
        try:
            out += tal_board(client, board, company, any(b[0] == client for b in TAL))  # comptes récoltés : intitulé financier exigé
        except Exception:
            pass
    return out


def s_janestreet():
    out = []
    for j in S.get('https://www.janestreet.com/jobs/main.json', timeout=T).json():
        if 'intern' not in str(j.get('availability') or '').lower():
            continue
        out.append(row('janestreet', 'Jane Street', str(j.get('position')) + ' Intern', f"https://www.janestreet.com/join-jane-street/position/{j.get('id')}/",
                       JS_CITY.get(j.get('city'), j.get('city')), None, j.get('id'), j.get('overview'), trust=True))
    return out


def s_simplify():
    out = []
    for j in S.get(SIMPLIFY, timeout=60).json():
        if not j.get('active') or not j.get('is_visible', True) or not str(j.get('url') or '').startswith('http'):
            continue
        locs = [str(x) for x in j.get('locations') or []]
        loc = next((x for x in locs if country_of(x)), locs[0] if locs else '')
        out.append(row('simplify', j.get('company_name'), j.get('title'), j['url'], loc, iso(j.get('date_posted')), j.get('id'),
                       trust=str(j.get('category')) in ('Quant', 'Quantitative Finance'), csite=j.get('company_url') or ''))
    return out


def s_muse():
    out = []
    for cat in ('Accounting and Finance', 'Business Operations', 'Sales'):
        for page in range(0, 20):
            r = S.get(MUSE, params={'level': 'Internship', 'category': cat, 'page': page}, timeout=T)
            r.raise_for_status()
            js = r.json()
            for j in js.get('results') or []:
                u = (j.get('refs') or {}).get('landing_page')
                if u:
                    out.append(row('muse', (j.get('company') or {}).get('name'), j.get('name'), u, ((j.get('locations') or [{}])[0] or {}).get('name'),
                                   iso(j.get('publication_date')), j.get('id')))
            if page + 1 >= (js.get('page_count') or 0):
                break
    return out


def s_arbeitnow():
    out = []
    for page in range(1, 7):
        r = S.get('https://www.arbeitnow.com/api/job-board-api', params={'page': page}, timeout=T)
        r.raise_for_status()
        lst = r.json().get('data') or []
        for j in lst:
            t = j.get('title') or ''
            if is_intern(t) or re.search('intern', ' '.join(j.get('job_types') or []), re.I):
                out.append(row('arbeitnow', j.get('company_name'), t, j.get('url'), j.get('location'), iso(j.get('created_at')), j.get('slug'), j.get('description')))
        if not lst:
            break
    return out


SOURCES = {'trackr': s_trackr, 'efc': s_efc, 'wttj': s_wttj, 'talnet': s_talnet, 'janestreet': s_janestreet,
           'simplify': s_simplify, 'muse': s_muse, 'arbeitnow': s_arbeitnow}


def plus_rows(log=print):
    """Toutes les annonces des sources supplémentaires. Une source en panne est signalée et ignorée."""
    rows, stats = [], {}

    def run(item):
        name, f = item
        try:
            return name, f(), None
        except Exception as e:
            return name, [], str(e)[:80]
    with ThreadPoolExecutor(len(SOURCES)) as ex:
        for name, got, err in ex.map(run, SOURCES.items()):
            stats[name] = err or len(got)
            rows += [x for x in got if x['title'] and x['company'] and x['url']]
    log('agr+ : ' + ', '.join(f'{k} {v}' for k, v in stats.items()))
    return rows


if __name__ == '__main__':
    from collections import Counter
    from commun import keep_offer
    from sources import HL_TITLE
    rows = plus_rows()
    seen = {x['url'] for x in rows}
    print('annonces uniques', len(seen), flush=True)
    for src, n in Counter(x['src'] for x in rows).most_common():
        lst = [x for x in rows if x['src'] == src]
        ok = [x for x in lst if keep_offer(x) and (x['trust'] or HL_TITLE.search(x['title']))]
        print(f'{src} : {n} lues, {len(ok)} gardées, {sum(1 for x in ok if country_of(x["location"]) == "France")} en France, {sum(1 for x in ok if not country_of(x["location"]))} sans pays', flush=True)
        for x in ok[:3]:
            print('   ', x['company'][:30], '|', x['title'][:70], '|', x['location'][:40], flush=True)
