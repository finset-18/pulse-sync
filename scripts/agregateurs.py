#!/usr/bin/env python3
"""
Agrégateurs d'offres, lus à chaque passage : Indeed, Glassdoor, Google Jobs (via JobSpy)
et France Travail (API officielle, si FT_CLIENT_ID / FT_CLIENT_SECRET).

Les recherches (métier x ville x site) tournent par tranches : chaque passage en fait PER_PASS, le cycle
complet revient toutes les deux heures environ. Une offre d'agrégateur n'est fermée qu'après 8 jours sans
réapparaître (veille.py). Les offres d'une structure déjà lue par sa plateforme sont écartées (doublons).
"""
import os, re, time
from concurrent.futures import ThreadPoolExecutor
from commun import S, clean, is_intern, FIN_TITLE, iso, _norm, country_of, load, save, official_site
from sources import name_index, match_name, LEGAL, HL_TITLE, FAST, AGG, is_hl

PER_PASS = int(os.environ.get('AGG_PER_PASS', '30'))
# Google et Glassdoor ne renvoient rien depuis les serveurs GitHub (Glassdoor répond 403) : leurs tours sont laissés à Indeed
JS_SITES = set(os.environ.get('AGG_SITES', 'indeed').split(','))
HOURS = int(os.environ.get('AGG_HOURS', str(24 * 21)))

FR = ['stage M&A', 'stage fusions acquisitions', 'stage private equity', 'stage capital investissement', 'stage venture capital',
      'stage corporate finance', 'stage transaction services', 'stage analyste financier', 'stage asset management',
      'stage gestion de portefeuille', 'stage finance de marché', 'stage sales trading', 'stage structuration', 'stage dette privée',
      'stage financement structuré', "stage banque d'affaires", 'stage investment banking', 'stage leveraged finance',
      'stage analyste crédit', 'stage gestion de patrimoine', 'stage family office', 'stage investissement immobilier',
      'stage infrastructure investissement', 'stage equity research', 'stage risk management', 'stage quant', 'stage trésorerie',
      'stage restructuring', 'stage ECM DCM', 'stage relations investisseurs']
EN = ['investment banking intern', 'M&A intern', 'private equity intern', 'venture capital intern', 'off-cycle internship finance',
      'summer analyst', 'asset management intern', 'sales and trading intern', 'equity research intern', 'credit analyst intern',
      'leveraged finance intern', 'restructuring intern', 'transaction services intern', 'corporate finance intern', 'quant intern',
      'real estate investment intern', 'infrastructure investment intern', 'wealth management intern', 'fund accounting intern',
      'risk management intern']
# (pays Indeed/Glassdoor, lieu, requêtes, sites JobSpy)
ZONES = [
    ('france', 'Paris', FR, ('indeed', 'glassdoor', 'google')),
    ('france', 'Lyon', FR[:10], ('indeed', 'google')),
    ('uk', 'London', EN, ('indeed', 'glassdoor', 'google')),
    ('luxembourg', 'Luxembourg', EN[:12] + ['stage finance', 'stage fonds'], ('indeed', 'google')),
    ('switzerland', 'Zürich', EN[:12], ('indeed', 'google')),
    ('switzerland', 'Genève', FR[:12], ('indeed', 'google')),
    ('belgium', 'Bruxelles', FR[:10] + EN[:6], ('indeed', 'google')),
    ('germany', 'Frankfurt', EN[:12], ('indeed', 'glassdoor', 'google')),
    ('netherlands', 'Amsterdam', EN[:10], ('indeed', 'google')),
    ('usa', 'New York, NY', EN, ('indeed', 'glassdoor', 'google')),
    ('spain', 'Madrid', EN[:8], ('indeed', 'google')),
    ('italy', 'Milan', EN[:8], ('indeed', 'google')),
    ('ireland', 'Dublin', EN[:8], ('indeed', 'google')),
    ('france', 'Lille', FR[:10], ('indeed',)),
    ('france', 'Bordeaux', FR[:6], ('indeed',)),
    ('france', 'Nantes', FR[:6], ('indeed',)),
    ('france', 'Marseille', FR[:6], ('indeed',)),
    ('france', 'France', FR, ('indeed',)),
    ('hong kong', 'Hong Kong', EN[:10], ('indeed',)),
    ('singapore', 'Singapore', EN[:10], ('indeed',)),
    ('united arab emirates', 'Dubai', EN[:8], ('indeed',)),
]
FT_Q = ['stage finance', 'stage analyste financier', 'stage fusions acquisitions', 'stage private equity', 'stage banque',
        'stage gestion actifs', 'stage audit financier', 'stage investissement', 'stage marchés financiers', 'stage crédit']
def tasks():
    out = []
    for country, loc, qs, sites in ZONES:
        for q in qs:
            for s in sites:
                if s in JS_SITES:
                    out.append(('js', s, country, loc, q))
    if os.environ.get('FT_CLIENT_ID') and os.environ.get('FT_CLIENT_SECRET'):
        out += [('ft', 'francetravail', '', '', q) for q in FT_Q]
    # sites mélangés pour qu'une tranche ne frappe pas un seul site d'affilée
    return sorted(out, key=lambda t: (_norm(t[4] + t[3]), t[1]))


def _s(v):
    if v is None or (isinstance(v, float) and v != v):
        return ''
    if isinstance(v, (list, tuple)):
        return ', '.join(_s(x) for x in v if _s(x))
    if isinstance(v, dict):
        return ', '.join(_s(x) for x in v.values() if isinstance(x, str) and x)
    return clean(str(v))


def _date(v):
    s = _s(v)
    return iso(s[:10]) if re.match(r'20\d{2}-\d{2}-\d{2}', s) else None


def run_jobspy(site, country, loc, q):
    from jobspy import scrape_jobs
    kw = dict(site_name=[site], search_term=q, location=loc, results_wanted=50, hours_old=HOURS, country_indeed=country,
              description_format='markdown', verbose=0)
    if site == 'google':
        kw['google_search_term'] = f'{q} {loc} since last month'
    df = scrape_jobs(**kw)
    out = []
    for r in df.to_dict('records'):
        u = _s(r.get('job_url'))
        if not u.startswith('http'):
            continue
        out.append({'title': _s(r.get('title')), 'company': _s(r.get('company')), 'url': u, 'location': re.sub(r',\s*[A-Z]{1,2}\d{1,2}\b', '', _s(r.get('location')))[:120],
                    'csite': _s(r.get('company_url_direct')) if _s(r.get('company_url_direct')).startswith('http') else '',
                    'logo': _s(r.get('company_logo')) if _s(r.get('company_logo')).startswith('https://') else '',
                    'posted': _date(r.get('date_posted')), 'ext': site + ':' + (_s(r.get('id')) or u),
                    'desc': _s(r.get('description'))[:4000], 'commit': _s(r.get('job_type')), 'src': site})
    return out


_FT = {}


def ft_token():
    if _FT.get('t') and _FT['exp'] > time.time():
        return _FT['t']
    r = S.post('https://entreprise.francetravail.fr/connexion/oauth2/access_token?realm=%2Fpartenaire',
               data={'grant_type': 'client_credentials', 'client_id': os.environ['FT_CLIENT_ID'],
                     'client_secret': os.environ['FT_CLIENT_SECRET'], 'scope': 'api_offresdemploiv2 o2dsoffre'}, timeout=20)
    r.raise_for_status()
    js = r.json()
    _FT.update(t=js['access_token'], exp=time.time() + int(js.get('expires_in', 1000)) - 60)
    return _FT['t']


def run_ft(q):
    r = S.get('https://api.francetravail.io/partenaire/offresdemploi/v2/offres/search',
              params={'motsCles': q, 'range': '0-149', 'publieeDepuis': 31}, headers={'Authorization': 'Bearer ' + ft_token(),
                                                                                       'Accept': 'application/json'}, timeout=30)
    if r.status_code == 204:
        return []
    r.raise_for_status()
    out = []
    for j in r.json().get('resultats') or []:
        oid = j.get('id')
        if not oid:
            continue
        out.append({'title': _s(j.get('intitule')), 'company': _s((j.get('entreprise') or {}).get('nom')),
                    'url': f'https://candidat.francetravail.fr/offres/recherche/detail/{oid}',
                    'location': _s((j.get('lieuTravail') or {}).get('libelle')) + ', France', 'posted': iso(j.get('dateCreation')),
                    'ext': 'ft:' + oid, 'desc': _s(j.get('description'))[:4000],
                    'commit': _s(j.get('typeContratLibelle')) + ' ' + _s(j.get('natureContrat')), 'src': 'francetravail'})
    return out


WD = 'https://www.wikidata.org/w/api.php'
WD_MAX = int(os.environ.get('WD_MAX', '250'))  # recherches de site par passage


def domain_for(name, cache):
    """Site officiel d'une entreprise d'après Wikidata (propriété P856), pour afficher son logo."""
    k = _norm(name)
    if k in cache:
        return cache[k]
    site = ''
    try:
        h = {'User-Agent': 'pulse-sync/1.0 (logos)'}
        r = S.get(WD, params={'action': 'wbsearchentities', 'search': name, 'language': 'fr', 'uselang': 'fr', 'type': 'item',
                              'limit': 3, 'format': 'json'}, headers=h, timeout=10).json()
        ids = [x['id'] for x in r.get('search', [])]
        if ids:
            e = S.get(WD, params={'action': 'wbgetentities', 'ids': '|'.join(ids), 'props': 'claims', 'format': 'json'},
                      headers=h, timeout=10).json().get('entities', {})
            for i in ids:
                cl = (e.get(i, {}).get('claims', {}).get('P856') or [])
                if cl:
                    site = str(cl[0].get('mainsnak', {}).get('datavalue', {}).get('value') or '')
                    break
    except Exception:
        return ''
    cache[k] = site if site.startswith('http') else ''
    return cache[k]


def one(t):
    kind, site, country, loc, q = t
    try:
        if kind == 'js':
            return site, run_jobspy(site, country, loc, q), None
        return site, run_ft(q), None
    except Exception as e:
        return site, [], str(e)[:80]


def run_aggregators(structs, recettes, run_no, log=print):
    """Résultats {id: (offres brutes, infos)} + pseudo-structures des sociétés hors liste."""
    try:
        import jobspy  # noqa
    except Exception:
        log('agrégateurs : JobSpy indisponible')
    all_t = tasks()
    n_slices = max(1, -(-len(all_t) // PER_PASS))
    mine = [t for i, t in enumerate(all_t) if i % n_slices == run_no % n_slices]
    rows, seen, stats = [], set(), {}
    # un fil par site : jamais deux requêtes en même temps sur le même site
    by_site = {}
    for t in mine:
        by_site.setdefault(t[1], []).append(t)

    def lane(ts):
        res = []
        for t in ts:
            res.append(one(t))
            time.sleep(1.5)
        return res
    with ThreadPoolExecutor(max(1, len(by_site))) as ex:
        for res in ex.map(lane, by_site.values()):
            for site, got, err in res:
                st = stats.setdefault(site, [0, 0, 0])
                st[0 if err is None else 1] += 1
                for x in got:
                    if x['url'] in seen:
                        continue
                    seen.add(x['url'])
                    rows.append(x)
                st[2] += len(got)
    log('agrégateurs : tranche ' + str(run_no % n_slices + 1) + '/' + str(n_slices) + ' : ' +
        ', '.join(f'{k} {v[0]} ok / {v[1]} échecs / {v[2]} annonces' for k, v in sorted(stats.items())))

    if os.environ.get('EXTRA') == '1':
        try:
            from plus import plus_rows
            for x in plus_rows(log):
                if x['url'] not in seen:
                    seen.add(x['url'])
                    rows.append(x)
        except Exception as e:
            log('agr+ : ' + str(e)[:120])

    idx = name_index(structs)
    fast = {cid for cid, r in recettes.items() if any(m.get('m') in FAST for m in (r.get('methods') or []))}
    # structures dont la plateforme donne vraiment des offres : les annonces d'agrégateurs seraient des doublons.
    # Les autres (page lue mais 0 offre, comme Goldman Sachs) gardent les offres trouvées par les agrégateurs.
    offs = (load('offers.json') or {}).get('offers') or []
    served = {o['company_id'] for o in offs if o.get('status') == 'open' and o.get('via') not in AGG
              and o.get('via') not in ('html', 'watch', 'page', 'render', 'api')}
    results, pseudo, dedup = {}, {}, set()
    for x in rows:
        title, comp, csite = x['title'], x.pop('company', ''), x.pop('csite', '')
        trust = x.get('trust')  # source 100 % finance : l'intitulé n'a pas à contenir un mot-clé financier
        if not title or not comp or not (trust or is_intern(title) or x.get('commit')) or not (trust or FIN_TITLE.search(title + ' ' + comp)):
            continue
        cid = match_name(comp, idx)
        if cid and cid in fast and cid in served:
            continue  # structure déjà lue par sa plateforme ou son site : ce serait un doublon
        if not cid:
            if not trust and not HL_TITLE.search(title):
                continue
            key = _norm(LEGAL.sub(' ', comp)) or _norm(comp)
            if len(key) < 3:
                continue
            cid = 'h_in_' + key[:40]
            ps = pseudo.setdefault(cid, {'id': cid, 'name': comp[:80], 'fam': '', 'city': '', 'site': '', 'url': '', 'hors_liste': True})
            if csite and not ps['site']:
                # site de l'entreprise (pour son logo), jamais celui de l'agrégateur ni d'une plateforme de recrutement
                ps['site'] = official_site({'site': csite})
        dk = (cid, _norm(title), country_of(x.get('location') or ''))
        if dk in dedup:
            continue  # même offre vue sur plusieurs agrégateurs
        dedup.add(dk)
        src = x['src']
        raw, info = results.setdefault(cid, ([], {'id': cid, 'ok': True, 'via': src, 'err': '', 'ran': [], 'complete': False, 'partial': []}))
        if src not in info['ran']:
            info['ran'].append(src)
            info['partial'].append(src)
        raw.append(x)
    # sociétés hors liste sans site : on cherche leur site officiel (logo) ; le cache évite de refaire les mêmes recherches
    cache, n = load('domaines.json') or {}, 0
    for ps in pseudo.values():
        if ps['site'] or n >= WD_MAX:
            continue
        if _norm(ps['name']) not in cache:
            n += 1
        ps['site'] = domain_for(ps['name'], cache)
    if n:
        save('domaines.json', cache)
    return results, list(pseudo.values())
