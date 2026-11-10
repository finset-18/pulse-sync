#!/usr/bin/env python3
"""
LinkedIn en profondeur (workflow linkedin.yml) : les boutiques et petits fonds publient leurs stages sur LinkedIn bien plus
souvent que sur leur site. Le passage lent n'y lit que 20 annonces par recherche, dans 8 villes (il ouvre chaque fiche).
Ici : liste seule, sans ouvrir de fiche, donc beaucoup plus large et plus profond. Stages publiés depuis une semaine,
par métier et par pays, jusqu'à PAGES pages de 10 annonces par recherche.

Les annonces suivent ensuite le chemin habituel : rattachement à la structure de la liste ou société hors liste
(agregateurs.attach, mêmes filtres), puis veille.py lit la fiche des offres nouvelles (dates, durée).
Écrit data/lent_li_<lot>.json, au format de lent.json (structs, results), que veille.py reprend.
Plusieurs lots tournent en parallèle (un serveur chacun) ; un lot s'arrête dès que LinkedIn limite les requêtes
et reprend à la recherche suivante au passage d'après. Essai mesuré (une recherche par nom de structure) : écarté,
la recherche publique renvoie surtout des homonymes.
Le journal (dépôt public) ne montre que des comptes.
Usage : SHARD=0 SHARDS=6 python scripts/linkedin.py        (essai sans écrire : LI_DRY=1)
"""
import html as _html, json, os, random, re, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import requests
from commun import load, save, NOW_ISO, UA, clean

SHARD, SHARDS = int(os.environ.get('SHARD', '0')), max(1, int(os.environ.get('SHARDS', '1')))
MAX_Q = int(os.environ.get('LI_MAX_Q', '40'))          # recherches par passage et par lot
BUDGET_S = float(os.environ.get('LI_BUDGET_S', '600'))
PAUSE = (float(os.environ.get('LI_PAUSE_MIN', '2.5')), float(os.environ.get('LI_PAUSE_MAX', '5')))
URL = 'https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search'
S = requests.Session()
S.headers.update({'User-Agent': UA, 'Accept-Language': 'fr-FR,fr;q=0.9,en;q=0.8'})

CARD = re.compile(r'(?s)<li>(.*?)</li>')
F = {'title': re.compile(r'(?s)base-search-card__title[^>]*>(.*?)</h3>'), 'company': re.compile(r'(?s)base-search-card__subtitle[^>]*>(.*?)</h4>'),
     'location': re.compile(r'(?s)job-search-card__location[^>]*>(.*?)</span>'), 'url': re.compile(r'href="(https://[a-z.]*linkedin\.com/jobs/view/[^"?]+)'),
     'posted': re.compile(r'datetime="(\d{4}-\d{2}-\d{2})"'), 'logo': re.compile(r'data-delayed-url="(https://media\.licdn\.com/[^"]+)"')}
_txt = lambda m: clean(_html.unescape(re.sub(r'<[^>]+>', ' ', m.group(1)))) if m else ''


class Limited(Exception):
    pass


def cards(htmltext):
    out = []
    for c in CARD.findall(htmltext or ''):
        u = F['url'].search(c)
        t = _txt(F['title'].search(c))
        if not u or not t: continue
        url = u.group(1)
        jid = re.search(r'-(\d{6,})$', url) or re.search(r'/(\d{6,})$', url)
        p, lg = F['posted'].search(c), F['logo'].search(c)
        out.append({'title': t, 'company': _txt(F['company'].search(c)), 'location': _txt(F['location'].search(c))[:120],
                    'url': 'https://www.linkedin.com/jobs/view/' + jid.group(1) if jid else url, 'posted': p.group(1) if p else None,
                    'logo': _html.unescape(lg.group(1)) if lg else '', 'ext': 'linkedin:' + (jid.group(1) if jid else url), 'desc': '', 'commit': 'internship', 'src': 'linkedin'})
    return out


FR = ['stage M&A', 'stage fusions acquisitions', 'stage private equity', 'stage capital investissement', 'stage venture capital', 'stage corporate finance',
      'stage transaction services', 'stage audit financier', 'stage asset management', 'stage analyste financier', "stage banque d'investissement",
      'stage finance de marché', 'stage dette privée', 'stage financement structuré', 'stage financement de projets', 'stage gestion de patrimoine',
      'stage family office', 'stage investissement immobilier', 'stage infrastructure', 'stage restructuring', 'stage equity research', 'stage trésorerie',
      'stage analyste crédit', 'stage contrôle de gestion', 'stagiaire analyste', 'césure finance', 'off-cycle analyst', 'stage evaluation financière']
EN = ['investment banking intern', 'private equity intern', 'M&A intern', 'off-cycle internship', 'venture capital intern', 'asset management intern']
EN_ZONES = ['United Kingdom', 'Luxembourg', 'Switzerland', 'Germany', 'Netherlands', 'Belgium', 'Spain', 'Italy', 'Ireland']
QUERIES = [(q, 'France') for q in FR] + [(q, z) for z in EN_ZONES for q in EN]
PAGES = int(os.environ.get('LI_PAGES', '5'))


def search(q, loc, start=0):
    """Une page (10 annonces) de stages publiés depuis une semaine, les plus récents d'abord."""
    r = S.get(URL, params={'keywords': q, 'location': loc, 'f_JT': 'I', 'f_TPR': 'r604800', 'start': start, 'sortBy': 'DD'}, timeout=20)
    if r.status_code in (429, 999) or (r.status_code == 200 and 'authwall' in r.url): raise Limited(str(r.status_code))
    if r.status_code == 400: return []  # fin de liste
    r.raise_for_status()
    return cards(r.text)


def gather(queries, log=print):
    """Annonces des recherches de ce lot. Renvoie (lignes, bilan). S'arrête à la première limite de requêtes."""
    rows, seen, t0, n_req, done, limited, errs = [], set(), time.time(), 0, 0, False, 0
    for q, loc in queries:
        if time.time() - t0 > BUDGET_S or limited: break
        for page in range(PAGES):
            try:
                got = search(q, loc, page * 10); n_req += 1
            except Limited:
                limited = True; break
            except Exception:
                errs += 1; break
            new = [x for x in got if x['url'] not in seen]
            for x in new: seen.add(x['url']); rows.append(x)
            time.sleep(random.uniform(*PAUSE))
            if len(got) < 10 or not new: break  # fin de liste, ou page déjà vue par une autre recherche
        else:
            pass
        if not limited: done += 1
        if errs >= 5: break
    return rows, {'recherches': done, 'requetes': n_req, 'annonces': len(rows), 'limite': limited, 'erreurs': errs, 'secondes': round(time.time() - t0)}


def main():
    from agregateurs import attach
    structs = load('structures.json') or []
    name = f'lent_li_{SHARD}.json'
    prev = load(name)
    prev = prev if isinstance(prev, dict) else {}
    mine = [x for i, x in enumerate(QUERIES) if i % SHARDS == SHARD]
    pos = int(prev.get('pos') or 0) % max(1, len(mine))
    order = mine[pos:] + mine[:pos]  # après une limite, le passage suivant reprend à la recherche où celui-ci s'est arrêté
    rows, st = gather(order[:MAX_Q])
    results, pseudo = attach(rows, structs, load('recettes.json') or {})
    doc = {'updated': NOW_ISO, 'shard': SHARD, 'shards': SHARDS, 'pos': (pos + st['recherches']) % max(1, len(mine)), 'structs': pseudo,
           'results': {cid: [raw, info] for cid, (raw, info) in results.items()},
           'stats': dict(st, lot_recherches=len(mine), structures=len(results), hors_liste=len(pseudo), gardees=sum(len(r) for r, _ in results.values()))}
    print('linkedin lot %d/%d : ' % (SHARD + 1, SHARDS) + json.dumps(doc['stats'], ensure_ascii=False), flush=True)
    if os.environ.get('LI_DRY') == '1':
        json.dump(doc, open(os.environ.get('LI_DRY_OUT', '/tmp/li_dry.json'), 'w'), ensure_ascii=False)
        return 0
    if results or not prev: save(name, doc)  # passage vide (limite dès la première requête) : le fichier précédent reste
    return 0


if __name__ == '__main__':
    sys.exit(main())
