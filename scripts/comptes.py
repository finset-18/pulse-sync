#!/usr/bin/env python3
"""
Recherche automatique des plateformes de recrutement, lancée chaque semaine après annuaires.py.

1. récolte : chaque lien d'offre vu sur Trackr, SimplifyJobs (quant et noms financiers) ou dans offers.json
   révèle la plateforme d'une entreprise (Workday, Oracle, Greenhouse, tal.net...)
2. essai direct : pour chaque structure de la liste sans plateforme, teste les adresses probables
   sur les plateformes à API publique, à partir de son nom et de son site

Les comptes trouvés sont gardés dans data/comptes.json, qui ne fait que grandir, puis ajoutés à annuaire.json
(structures de la liste) et hors_liste.json (autres sociétés). Rien n'est retiré.
"""
import os, re, sys, time
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from commun import *  # noqa
from commun import _norm
from sources import name_index, match_name, name_keys
from annuaires import FIN_NAME, TECHY

WORKERS = int(os.environ.get('WORKERS', '24'))
BUDGET = int(os.environ.get('COMPTES_BUDGET_S', '4200'))
TRACKR = 'https://api.the-trackr.com/programmes'
SIMPLIFY = 'https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json'
TAL_RE = re.compile(r'https?://([\w-]+)\.tal\.net', re.I)
GUESS = ('greenhouse', 'lever', 'ashby', 'smartrecruiters', 'recruitee', 'personio', 'workable', 'pinpoint', 'bamboohr')
T0 = time.time()


def links():
    """(entreprise, lien, source sûre pour la finance)"""
    out = []
    for season in ('2026', '2027', '2028'):
        for region in ('UK', 'US', 'EU'):
            for kind in ('summer-internships', 'off-cycle-internships', 'spring-weeks', 'industrial-placements', 'graduate-programmes'):
                try:
                    r = S.get(TRACKR, params={'region': region, 'industry': 'Finance', 'season': season, 'type': kind}, timeout=45)
                    for p in r.json().get('programmes') or [] if r.ok else []:
                        co = p.get('company')
                        name = (co.get('name') if isinstance(co, dict) else co) or str(p.get('companyId') or '').replace('-', ' ').title()
                        out.append((name, p.get('url') or '', True))
                except Exception as e:
                    print('trackr', season, region, kind, str(e)[:80], flush=True)
    try:
        for j in S.get(SIMPLIFY, timeout=90).json():
            name = j.get('company_name') or ''
            quant = str(j.get('category')) in ('Quant', 'Quantitative Finance')
            if quant or (FIN_NAME.search(name) and not TECHY.search(name)):
                out.append((name, j.get('url') or '', quant))
    except Exception as e:
        print('simplify', str(e)[:80], flush=True)
    for o in (load('offers.json') or {}).get('offers') or []:
        if o.get('via') in ('trackr', 'efc', 'wttj_search'):
            out.append((o.get('company') or '', o.get('url') or '', o.get('via') != 'wttj_search'))
    return out


def harvest(ls):
    """{(plateforme, valeur en texte): (plateforme, valeur, entreprise)} et les comptes tal.net"""
    found, tal = {}, {}
    for name, url, _ in ls:
        m = TAL_RE.match(url)
        if m:
            tal.setdefault(m.group(1).lower(), name)
            continue
        for k, v in detect_ats(url).items():
            if k in FETCHERS and k != 'wttj':
                v = list(v) if isinstance(v, tuple) else v
                found.setdefault((k, str(v)), (k, v, name))
    return found, tal


def slugs(c):
    """Adresses probables d'une structure : son nom, puis le nom de domaine de son site."""
    out, host = [], host_of(site_of(c) or c.get('url') or '').split('.')[0]
    for s in sorted(name_keys(c.get('name')), key=len, reverse=True)[:2] + [_norm(host), re.sub(r'[^a-z0-9]+', '-', (c.get('name') or '').lower()).strip('-')]:
        if len(s) >= 4 and s not in out:
            out.append(s)
    return out[:3]


def probe(a):
    """Le compte répond-il et publie-t-il des postes crédibles pour cette structure ?"""
    c, k, v = a
    if time.time() - T0 > BUDGET:
        return False
    try:
        res = FETCHERS[k](c, tuple(v) if isinstance(v, list) else v)
    except Exception:
        return False
    if not isinstance(res, list) or not res:
        return False
    slug = _norm(v[0] if isinstance(v, list) else v)
    return len(slug) >= 8 or any(FIN_TITLE.search(x.get('title') or '') for x in res)


def alive(a):
    k, v = a
    if time.time() - T0 > BUDGET:
        return False
    try:
        return isinstance(FETCHERS[k]({'name': '', 'id': ''}, tuple(v) if isinstance(v, list) else v), list)
    except Exception:
        return False


def main():
    structs = load('structures.json') or []
    recettes = load('recettes.json') or {}
    ann, hl = load('annuaire.json') or {}, load('hors_liste.json') or []
    mem = load('comptes.json') or {}
    m_ann, m_hl, m_tal = mem.get('ann') or {}, mem.get('hl') or [], mem.get('tal') or {}
    used = set()
    for r in list(recettes.values()) + [{'methods': x} for x in ann.values()] + [{'methods': h.get('methods')} for h in hl + m_hl]:
        for m in r.get('methods') or []:
            if m.get('m') == 'ats':
                used.add((m.get('k'), str(m.get('v'))))
    for ms in m_ann.values():
        used |= {(m['k'], str(m['v'])) for m in ms}
    idx, byid = name_index(structs), {c['id']: c for c in structs}

    ls = links()
    found, tal = harvest(ls)
    print(f'{len(ls)} liens, {len(found)} comptes récoltés, {len(tal)} comptes tal.net', flush=True)
    m_tal.update(tal)
    new = [v for key, v in found.items() if key not in used]
    with ThreadPoolExecutor(WORKERS) as ex:
        oks = list(ex.map(alive, [(k, v) for k, v, _ in new]))
    n_list = n_hl = 0
    ids = {h['id'] for h in hl + m_hl}
    for (k, v, name), ok in zip(new, oks):
        if not ok:
            continue
        cid = match_name(name, idx)
        if cid:
            m_ann.setdefault(cid, []).append({'m': 'ats', 'k': k, 'v': v, 'src': 'comptes'}); n_list += 1
        else:
            s = v[1] if k == 'workday' else v[0] if isinstance(v, list) else v
            hid = 'h_' + k[:2] + '_' + _norm(s)[:40]
            if hid not in ids:
                ids.add(hid); n_hl += 1
                m_hl.append({'id': hid, 'name': name[:80], 'site': '', 'methods': [{'m': 'ats', 'k': k, 'v': v}]})
        used.add((k, str(v)))

    tries = []
    for c in structs:
        r = (recettes.get(c['id']) or {}).get('methods') or []
        if any(m.get('m') == 'ats' for m in r) or c['id'] in ann or c['id'] in m_ann:
            continue
        for s in slugs(c):
            for k in GUESS:
                v = [s, False] if k == 'lever' else s
                if (k, str(v)) not in used:
                    tries.append((c, k, v))
    print(f'{len(tries)} adresses à essayer', flush=True)
    with ThreadPoolExecutor(WORKERS) as ex:
        oks = list(ex.map(probe, tries))
    n_guess = 0
    for (c, k, v), ok in zip(tries, oks):
        if ok and not any(m['k'] == k for m in m_ann.get(c['id'], [])):
            m_ann.setdefault(c['id'], []).append({'m': 'ats', 'k': k, 'v': v, 'src': 'essai'}); n_guess += 1

    save('comptes.json', {'ann': m_ann, 'hl': m_hl, 'tal': m_tal}, pretty=True)
    for cid, ms in m_ann.items():
        if cid in byid:
            cur = ann.setdefault(cid, [])
            cur += [m for m in ms if (m['k'], str(m['v'])) not in {(x['k'], str(x['v'])) for x in cur}]
    have = {h['id'] for h in hl}
    hl += [h for h in m_hl if h['id'] not in have]
    save('annuaire.json', ann, pretty=True)
    save('hors_liste.json', hl)
    print(f'comptes : {n_list} structures via la récolte, {n_guess} via les essais, {n_hl} sociétés hors liste, {len(m_tal)} comptes tal.net')


if __name__ == '__main__':
    main()
