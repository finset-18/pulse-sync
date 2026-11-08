#!/usr/bin/env python3
"""
Compare deux évaluations (dates_eval.py) : gains, pertes et changements de valeur, avec le passage de l'offre qui les explique.
Écrit le détail dans un fichier du dépôt privé (jamais dans le journal public) ; le journal ne montre que des comptes.
Usage : python dates_compare.py AVANT.json APRES.json SORTIE.json
"""
import json, sys
from commun import load, NOW

a, b, out = (json.load(open(sys.argv[1])), json.load(open(sys.argv[2])), sys.argv[3])
doc = load('offers.json') or {}
offers = doc.get('offers') or {}
if isinstance(offers, dict): offers = list(offers.values())
O = {o['id']: o for o in offers}
cur = NOW.strftime('%Y-%m')


def eff_start(r):  # début tel que le tracker l'appliquerait : date écrite > dès que possible > saison
    s, asap, season, _ = r
    return s or ('ASAP' if asap else (('S:' + season) if season and season >= cur else None))


cats = {'debut_gagne': [], 'debut_perdu': [], 'debut_change': [], 'duree_gagnee': [], 'duree_perdue': [], 'duree_changee': []}
for oid, rb in b.items():
    ra = a.get(oid)
    if not ra: continue
    sa, sb, ma, mb = eff_start(ra), eff_start(rb), ra[3], rb[3]
    o = O.get(oid, {})
    item = {'id': oid, 'titre': (o.get('title') or '')[:120], 'url': o.get('url'), 'avant': None, 'apres': None,
            'texte': (o.get('ctx') or o.get('excerpt') or '')[:700]}
    if sa != sb:
        k = 'debut_gagne' if not sa else ('debut_perdu' if not sb else 'debut_change')
        cats[k].append({**item, 'avant': sa, 'apres': sb})
    if ma != mb:
        k = 'duree_gagnee' if not ma else ('duree_perdue' if not mb else 'duree_changee')
        cats[k].append({**item, 'avant': ma, 'apres': mb})
n = len(b)
tot = lambda r, f: sum(1 for x in r.values() if f(x))
summary = {'offres': n,
           'debut_avant': tot(a, lambda r: eff_start(r)), 'debut_apres': tot(b, lambda r: eff_start(r)),
           'duree_avant': tot(a, lambda r: r[3]), 'duree_apres': tot(b, lambda r: r[3]),
           **{k: len(v) for k, v in cats.items()}}
json.dump({'resume': summary, **cats}, open(out, 'w'), ensure_ascii=False, indent=1)
print('compare : ' + ' | '.join(f'{k} {v}' for k, v in summary.items()))
