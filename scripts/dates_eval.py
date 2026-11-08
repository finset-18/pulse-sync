#!/usr/bin/env python3
"""
Test de non-régression des règles de dates (manuel) : applique les règles d'un dossier de scripts donné au titre et aux
passages gardés de chaque offre ouverte, et écrit le résultat en JSON. Ne modifie aucune offre.
Usage : python dates_eval.py DOSSIER_SCRIPTS SORTIE.json
"""
import json, os, sys

src, out = sys.argv[1], sys.argv[2]
sys.path.insert(0, os.path.abspath(src))
import commun as c  # noqa: E402  (la version du dossier donné)

doc = c.load('offers.json') or {}
offers = doc.get('offers') or {}
if isinstance(offers, dict): offers = list(offers.values())
sw = getattr(c, 'start_with_title', None)
mw = getattr(c, 'months_with_title', None)
season = getattr(c, 'season_of', None)
res = {}
for o in offers:
    if o.get('status') != 'open': continue
    title, txt = o.get('title') or '', o.get('ctx') or o.get('excerpt') or ''
    s = sw(title, txt) if sw else (c.start_of(title) or c.start_of(txt, loose=False))
    m = mw(title, txt) if mw else (c.months_of(title) or c.months_of(txt))
    a = bool(c.start_asap(title) or c.start_asap(txt))
    ss = season(title + ' . ' + txt)[0] if season else None
    res[o['id']] = [s, a, ss, m]
json.dump(res, open(out, 'w'))
print(f'eval : {len(res)} offres ouvertes évaluées avec {src}')
