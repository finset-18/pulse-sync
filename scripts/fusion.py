#!/usr/bin/env python3
"""Fusionne les recettes des lots de la découverte (data/recettes-part-*.json) dans data/recettes.json
et écrit le bilan de couverture dans data/decouverte.json."""
import glob, os, sys
from collections import Counter
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from commun import load, save, DATA, NOW_ISO

import json
rec = load('recettes.json') or {}
parts = sorted(glob.glob(os.path.join(DATA, 'recettes-part-*.json')))
n, seen_ids = 0, set()
for p in parts:
    try:
        with open(p, encoding='utf-8') as f: part = json.load(f)
    except Exception:
        continue
    for k, v in part.items():
        seen_ids.add(k)
        old = rec.get(k)
        # une découverte ratée (site injoignable, délai) ne remplace pas une méthode qui marchait
        if old and old.get('methods') and not v.get('methods') and (v.get('err') or v.get('blocked')):
            old['checked'] = v.get('checked'); old['last_err'] = v.get('err') or 'bloqué'
            continue
        rec[k] = v; n += 1
    os.remove(p)
save('recettes.json', rec)
revoir = set(load('a_revoir.json') or [])
save('a_revoir.json', sorted(revoir - seen_ids))

structs = load('structures.json') or []
first = Counter()
for c in structs:
    r = rec.get(c['id'])
    m = [x['m'] for x in (r or {}).get('methods', [])]
    key = 'pas encore vue' if r is None else ('+'.join(sorted(set(m))) if m else ('bloquée' if r.get('blocked') else 'aucune'))
    first[key] += 1
auto = sum(1 for c in structs if any(x['m'] in ('ats', 'api', 'html', 'render', 'wttj') for x in (rec.get(c['id']) or {}).get('methods', [])))
watch = sum(1 for c in structs if [x['m'] for x in (rec.get(c['id']) or {}).get('methods', [])] == ['watch'])
summary = {'updated': NOW_ISO, 'structures': len(structs), 'recettes': len(rec), 'mises_a_jour': n,
           'offres_lues': auto, 'texte_surveille': watch, 'detail': dict(first.most_common())}
save('decouverte.json', summary, pretty=True)
print(summary)
