#!/usr/bin/env python3
"""
Annuaires publics « entreprise -> plateforme de recrutement » (Feashliaa/job-board-aggregator,
pa-nuzz/company-dataset-) :
1. pour chaque structure de la liste sans plateforme, cherche son compte (Workday, Greenhouse, Lever,
   Ashby) dans les annuaires, le vérifie en direct et l'ajoute (data/annuaire.json)
2. repère les sociétés financières hors liste qui recrutent sur ces plateformes (data/hors_liste.json)
"""
import csv, io, os, re, sys, time
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from commun import *  # noqa
from commun import _norm
from sources import name_keys, LEGAL

RAW = 'https://raw.githubusercontent.com'
FEASH = RAW + '/Feashliaa/job-board-aggregator/main/data/{}_companies.json'
PANUZZ = RAW + '/pa-nuzz/company-dataset-/main/data/output/companies.csv'
WORKERS = int(os.environ.get('WORKERS', '24'))
MAX_HL = int(os.environ.get('MAX_HL', '1800'))
QUIET = os.environ.get('QUIET', '') == '1'
SUFFIX = {'careers', 'career', 'jobs', 'job', 'external', 'ext', 'externalcareers', 'professional', 'group', 'global', 'inc', 'llc',
          'ltd', 'france', 'fr', 'uk', 'europe', 'emea', 'campus', 'students', 'graduates', 'hiring', 'recruiting', 'talent', 'site',
          'careersite', 'en', 'co', 'hq', 'official', 'team'}
FIN_NAME = re.compile(r"capital|partners|partenaires|invest|asset|bank|banque|bancaire|ventures|equity|securities|financ|advis|conseil|fund|fonds|"
                      r"wealth|patrimo|credit|trading|markets|gestion|holding|family.?office|private|lbo|corporate|treasury|insurance|assurance|"
                      r"reinsur|actuar|valuation|transaction|merger|acquisition|brokerage|broker|exchange|bourse|reit|realestate|immobili|infra",
                      re.I)
TECHY = re.compile(r"software|crypto|blockchain|web3|\bai\b|labs?\b|games?|gaming|health|bio|pharma|medical|robot|data\b|cloud|security|cyber", re.I)


def log(*a):
    if not QUIET:
        print(*a, flush=True)


def fetch_json(url):
    for i in range(3):
        try:
            return S.get(url, timeout=90).json()
        except Exception:
            time.sleep(3 * (i + 1))
    return None


def fetch_text(url):
    for i in range(3):
        try:
            r = S.get(url, timeout=120)
            r.raise_for_status()
            return r.text
        except Exception:
            time.sleep(3 * (i + 1))
    return ''


def candidates():
    """(plateforme, valeur, nom affiché, site) de tous les comptes connus des annuaires."""
    out = []
    for kind in ('workday', 'greenhouse', 'lever', 'ashby'):
        lst = fetch_json(FEASH.format(kind)) or []
        n0 = len(out)
        for x in (lst if isinstance(lst, list) else list(lst)):
            x = str(x).strip()
            if kind == 'workday':
                p = x.split('|')
                if len(p) != 3 or re.fullmatch(r'wd\d+', p[0]) or not re.fullmatch(r'wd\d+', p[1]):
                    continue
                t, wd, site = p
                out.append(('workday', [f'{t}.{wd}.myworkdayjobs.com', t, site], t, ''))
            elif kind == 'lever':
                out.append(('lever', [x, False], x, ''))
            else:
                out.append((kind, x, x, ''))
        log(f'annuaire {kind} : {len(out) - n0} comptes')
    txt = fetch_text(PANUZZ)
    n0 = len(out)
    for r in csv.DictReader(io.StringIO(txt)) if txt else []:
        k, code = (r.get('ats_system') or '').strip().lower(), (r.get('ats_code') or '').strip()
        if not code or k not in ('greenhouse', 'lever', 'ashby'):
            continue
        out.append((k, [code, False] if k == 'lever' else code, r.get('name') or code, r.get('website_url') or ''))
    log(f'annuaire pa-nuzz : {len(out) - n0} comptes')
    return out


def slug_key(k, v):
    if k == 'workday':
        return _norm(v[1])
    return _norm(v[0] if isinstance(v, list) else v)


def same(c, key, name, site):
    """Le compte de l'annuaire est-il bien celui de la structure ? (correspondance stricte)"""
    if site and site_of(c) and host_of(site) == host_of(site_of(c)):
        return True
    ks = name_keys(c.get('name'))
    host = host_of(site_of(c)).split('.')[0] if site_of(c) else ''
    if len(_norm(host)) >= 4:
        ks.add(_norm(host))
    ks = {k for k in ks if len(k) >= 4}
    for cand in {key, _norm(name), _norm(LEGAL.sub(' ', name or ''))}:
        if not cand:
            continue
        if cand in ks:
            return True
        for k in ks:
            if len(k) >= 6 and cand.startswith(k) and cand[len(k):] in SUFFIX:
                return True
    return False


def verify(c, k, v):
    """Le compte répond-il ? Pour un nom court (risque d'homonyme), il doit aussi publier des postes de finance."""
    try:
        res = FETCHERS[k](c, tuple(v) if isinstance(v, list) else v)
    except Exception:
        return False
    if not isinstance(res, list):
        return False
    strong = (k == 'workday' and len(slug_key(k, v)) >= 5) or len(slug_key(k, v)) >= 8
    return strong or any(FIN_TITLE.search(x.get('title') or '') for x in res)


def main():
    structs = load('structures.json') or []
    recettes = load('recettes.json') or {}
    cands = candidates()
    if not cands:
        print('annuaires indisponibles : rien à faire')
        return
    by_key = {}
    for k, v, name, site in cands:
        by_key.setdefault(slug_key(k, v), []).append((k, v, name, site))
    by_host = {}
    for k, v, name, site in cands:
        if site:
            by_host.setdefault(host_of(site), []).append((k, v, name, site))

    # 1. structures de la liste
    todo, used = [], set()
    for c in structs:
        have = {m.get('k') for m in (recettes.get(c['id']) or {}).get('methods', []) if m.get('m') == 'ats'}
        keys = set()
        for kk in name_keys(c.get('name')) | {_norm(host_of(site_of(c)).split('.')[0])}:
            if len(kk) >= 4:
                keys.add(kk)
        pool = list(by_host.get(host_of(site_of(c)), [])) if site_of(c) else []
        for kk in keys:
            pool += by_key.get(kk, [])
            for sfx in SUFFIX:
                pool += by_key.get(kk + sfx, [])
        for k, v, name, site in pool:
            if k in have or not same(c, slug_key(k, v), name, site):
                continue
            todo.append((c, k, v))
    seen, uniq = set(), []
    for c, k, v in todo:
        sig = (c['id'], k, str(v))
        if sig not in seen:
            seen.add(sig)
            uniq.append((c, k, v))
    log(f'{len(uniq)} comptes à vérifier pour {len({c["id"] for c, _, _ in uniq})} structures')
    with ThreadPoolExecutor(WORKERS) as ex:
        oks = list(ex.map(lambda t: verify(*t), uniq))
    ann = {}
    for (c, k, v), ok in zip(uniq, oks):
        used.add((k, str(v)))
        if ok and not any(m['k'] == k and k != 'workday' for m in ann.get(c['id'], [])):
            ann.setdefault(c['id'], []).append({'m': 'ats', 'k': k, 'v': v, 'src': 'annuaire'})
    # les plateformes déjà connues par la découverte ne sont pas reprises hors liste
    for r in recettes.values():
        for m in r.get('methods') or []:
            if m.get('m') == 'ats':
                used.add((m.get('k'), str(m.get('v'))))
    save('annuaire.json', ann, pretty=True)

    # 2. sociétés financières hors liste
    hl, ids = [], set()
    pri = sorted(cands, key=lambda t: (not t[3], t[0] == 'workday'))  # celles qui ont un site d'abord, Workday en dernier (plus lent)
    n_wd = 0
    for k, v, name, site in pri:
        if (k, str(v)) in used or not FIN_NAME.search(name or '') or TECHY.search(name or ''):
            continue
        if k == 'workday':
            if n_wd >= 400:
                continue
            n_wd += 1
        hid = 'h_' + k[:2] + '_' + slug_key(k, v)[:40]
        if hid in ids:
            continue
        ids.add(hid)
        nice = name if (name and not re.fullmatch(r'[\w.-]+', name)) else re.sub(r'[-_.]+', ' ', name or '').strip().title()
        hl.append({'id': hid, 'name': nice[:80], 'site': site, 'methods': [{'m': 'ats', 'k': k, 'v': v}]})
        if len(hl) >= MAX_HL:
            break
    save('hors_liste.json', hl)
    n_struct = len(ann)
    print(f'annuaires : {n_struct} structures de la liste gagnent une plateforme, {len(hl)} sociétés hors liste suivies')


if __name__ == '__main__':
    main()
