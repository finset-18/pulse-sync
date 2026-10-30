#!/usr/bin/env python3
"""
Sources complémentaires du robot de veille.

- annuaire.json : plateformes de recrutement trouvées dans les annuaires publics pour les structures
  de la liste (ajoutées à leurs recettes, vérifiées par annuaires.py)
- hors_liste.json : sociétés financières hors liste qui publient sur une plateforme connue
  (identifiants « h_… », lues au passage étendu)
- agrégateurs (Indeed, via JobSpy) : stages des structures sans lecture automatique et des
  sociétés hors liste ; ces offres ne sont fermées qu'après 8 jours sans réapparaître
"""
import re, json, time, datetime as dt
from commun import load, clean, is_intern, FIN_TITLE, iso, _norm, COUNTRIES, country_of

AGG = ('indeed',)
FAST = ('ats', 'api', 'html', 'wttj')
# intitulé de poste financier (plus strict que FIN_TITLE, qui accepte « marketing ») : exigé hors liste
HL_TITLE = re.compile(r"financ|m&a|merger|fusion|acquisition|private equity|capital[- ]invest|venture|\blbo\b|invest|asset|wealth|patrimoin|"
                      r"\bfunds?\b|fonds|banking|banque|corporate finance|transaction|valuation|[ée]valuation|credit|cr[ée]dit|equity|debt|dette|"
                      r"trading|trader|markets\b|march[ée]s financiers|quant|treasury|tr[ée]sorerie|actuar|\brisk|risque", re.I)
LEGAL = re.compile(r"\b(sas|sasu|sa|sarl|sca|snc|ltd|limited|llp|plc|gmbh|ag|inc|llc|lp|nv|bv|se|s\.a\.?|group|groupe|holding|france|uk|europe|international)\b\.?", re.I)


def is_hl(cid):
    return str(cid).startswith('h_')


def merge_annuaire(recettes, ann):
    """Ajoute aux recettes les plateformes trouvées dans les annuaires (sans doublon)."""
    out = dict(recettes)
    for cid, meths in (ann or {}).items():
        r = dict(out.get(cid) or {'id': cid, 'methods': []})
        cur = list(r.get('methods') or [])
        kinds = {m.get('k') for m in cur if m.get('m') == 'ats'}
        add = [m for m in meths if m.get('m') == 'ats' and m.get('k') not in kinds]
        if add:
            r['methods'] = add + cur
            out[cid] = r
    return out


def home_countries(c):
    """Pays d'une structure d'après son nom et sa ville (« Baker Tilly France » -> France)."""
    t = (c.get('name') or '') + ' ' + (c.get('city') or '')
    return {n for n, rx in COUNTRIES if rx.search(t)}


def country_ok(c, loc):
    """Une offre lue sur un compte trouvé dans les annuaires (souvent le compte mondial du groupe) :
    gardée seulement dans le pays de la structure, ou à défaut dans un pays suivi."""
    cc = country_of(loc or '')
    home = home_countries(c)
    return (cc in home) if home else bool(cc)


def hors_liste():
    """Pseudo-structures hors liste et leurs recettes."""
    structs, recs = [], {}
    for h in load('hors_liste.json') or []:
        if not h.get('id') or not h.get('methods'):
            continue
        structs.append({'id': h['id'], 'name': h.get('name') or h['id'], 'fam': '', 'city': '', 'site': h.get('site') or '',
                        'url': h.get('url') or '', 'hors_liste': True})
        recs[h['id']] = {'id': h['id'], 'methods': h['methods']}
    return structs, recs


def name_keys(name):
    name = name or ''
    base = re.sub(r'\(.*?\)', ' ', name)
    ks = {_norm(name), _norm(base), _norm(base.replace('&', ' and ')), _norm(LEGAL.sub(' ', base))}
    return {k for k in ks if len(k) >= 3}


def name_index(structs):
    idx = {}
    for c in structs:
        if is_hl(c['id']):
            continue
        for k in name_keys(c.get('name')):
            idx.setdefault(k, c['id'])
    return idx


def match_name(name, idx):
    for k in sorted(name_keys(name), key=len, reverse=True):
        if k in idx:
            return idx[k]
    return None


# (pays Indeed, lieu, requêtes)
QUERIES = [
    ('france', 'Paris', ['stage M&A', 'stage fusions acquisitions', 'stage private equity', 'stage capital investissement',
                         'stage venture capital', 'stage corporate finance', 'stage transaction services', 'stage analyste financier',
                         'stage asset management', 'stage gestion de portefeuille', 'stage finance de marché', 'stage dette privée',
                         'stage banque d\'affaires', 'stage investment banking', 'stage family office', 'stage investissement immobilier',
                         'stage analyste crédit', 'stage financement structuré', 'stage gestion de patrimoine', 'stage trading']),
    ('france', 'Lyon', ['stage finance', 'stage M&A', 'stage private equity']),
    ('uk', 'London', ['investment banking intern', 'private equity intern', 'off-cycle internship', 'M&A internship',
                      'asset management internship', 'venture capital intern', 'summer analyst', 'credit analyst intern',
                      'equity research intern', 'trading internship']),
    ('luxembourg', 'Luxembourg', ['internship finance', 'stage finance', 'internship private equity', 'internship fund', 'stage fonds']),
    ('switzerland', 'Zürich', ['internship finance', 'internship private equity', 'internship asset management', 'Praktikum Finance']),
    ('switzerland', 'Genève', ['stage finance', 'internship finance', 'stage banque privée']),
    ('belgium', 'Bruxelles', ['internship finance', 'stage finance', 'stage M&A']),
]


def _s(v):
    if v is None or (isinstance(v, float) and v != v):
        return ''
    if isinstance(v, (list, tuple)):
        return ', '.join(_s(x) for x in v if _s(x))
    return clean(str(v))


def _date(v):
    s = _s(v)
    return iso(s[:10]) if re.match(r'20\d{2}-\d{2}-\d{2}', s) else None


def run_indeed(structs, recettes, log=print):
    """Résultats {id: (offres brutes, infos)} + pseudo-structures créées pour les sociétés hors liste."""
    try:
        from jobspy import scrape_jobs
    except Exception as e:
        log('indeed : JobSpy indisponible (' + str(e)[:60] + ')')
        return {}, []
    idx = name_index(structs)
    fast = {cid for cid, r in recettes.items() if any(m.get('m') in FAST for m in (r.get('methods') or []))}
    rows, seen, okq, errs = [], set(), 0, 0
    for country, loc, qs in QUERIES:
        for q in qs:
            try:
                df = scrape_jobs(site_name=['indeed'], search_term=q, location=loc, results_wanted=60, hours_old=24 * 14,
                                 country_indeed=country, description_format='markdown', verbose=0)
                okq += 1
            except Exception:
                errs += 1
                continue
            for r in df.to_dict('records'):
                u = _s(r.get('job_url'))
                if not u.startswith('http') or u in seen:
                    continue
                seen.add(u)
                rows.append(r)
            time.sleep(0.5)
    log(f'indeed : {okq} recherches, {errs} échecs, {len(rows)} annonces')
    results, pseudo = {}, {}
    for r in rows:
        title, comp = _s(r.get('title')), _s(r.get('company'))
        if not title or not comp or not is_intern(title) or not FIN_TITLE.search(title + ' ' + comp):
            continue
        cid = match_name(comp, idx)
        if cid and cid in fast:
            continue  # structure déjà lue par sa plateforme ou son site : ce serait un doublon
        if not cid:
            if not HL_TITLE.search(title):
                continue
            key = _norm(LEGAL.sub(' ', comp)) or _norm(comp)
            if len(key) < 3:
                continue
            cid = 'h_in_' + key[:40]
            pseudo.setdefault(cid, {'id': cid, 'name': comp[:80], 'fam': '', 'city': '', 'site': '', 'url': '', 'hors_liste': True})
        x = {'title': title, 'url': _s(r.get('job_url')), 'location': _s(r.get('location'))[:120], 'posted': _date(r.get('date_posted')),
             'ext': _s(r.get('id')) or _s(r.get('job_url')), 'desc': _s(r.get('description'))[:4000], 'commit': _s(r.get('job_type')),
             'src': 'indeed'}
        raw, _ = results.setdefault(cid, ([], {'id': cid, 'ok': True, 'via': 'indeed', 'err': '', 'ran': ['indeed'],
                                                'complete': False, 'partial': ['indeed']}))
        raw.append(x)
    return results, list(pseudo.values())


def merge_results(results, extra):
    """Ajoute les résultats d'un agrégateur à ceux des méthodes propres de chaque structure."""
    for cid, (raw, info) in extra.items():
        if cid in results:
            r0, i0 = results[cid]
            i0.pop('skip', None)
            r0 += raw
            i0['ok'] = True
            for s in info['ran']:
                if s not in i0['ran']:
                    i0['ran'].append(s)
                i0.setdefault('partial', []).append(s)
            i0['via'] = i0.get('via') or info['via']
        else:
            results[cid] = (raw, info)
    return results
