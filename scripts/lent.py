#!/usr/bin/env python3
"""
Passage lent (workflow lent.yml) : sociétés hors liste, agrégateurs et sources supplémentaires, rendu navigateur.
Il ne touche pas aux offres : il écrit data/lent.json (lignes brutes), que veille.py reprend à son passage suivant.
Une partie en panne est signalée et n'empêche pas d'écrire les autres.
"""
import asyncio, json, os, sys, time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ['EXTRA'] = '1'  # agregateurs.py lit alors aussi les sources supplémentaires (plus.py)
from commun import load, save, NOW_ISO, NOW
from sources import merge_annuaire, merge_dedies, hors_liste, merge_results, AGG
import veille
import agregateurs
from agregateurs import run_aggregators

BUDGET_HL = float(os.environ.get('LENT_HL_BUDGET_S', '900'))
BUDGET_RENDER = float(os.environ.get('LENT_RENDER_BUDGET_S', '1200'))
MAX_MB = float(os.environ.get('LENT_MAX_MB', '50'))  # lent.json au-delà : refusé, le fichier précédent reste en place
CYCLE_OK = 0.8  # part de recherches réussies pour qu'un cycle d'agrégateurs compte comme complet
WORKERS = int(os.environ.get('WORKERS', '32'))
T0 = time.time()


def complete(o):
    """Offre connue dont le robot n'a plus rien à tirer de la description."""
    return bool(o and o.get('excerpt') and ((o.get('months') and o.get('start')) or o.get('txt2')))


def slim(raw, by_url):
    """Sans la description quand l'offre est déjà connue et complète : lent.json reste léger."""
    for x in raw:
        if x.get('desc') and complete(by_url.get(x.get('url'))): x.pop('desc', None)
    return raw


def rotated(lst, start):
    """La liste à partir de start : d'un passage à l'autre, ce que le budget a coupé passe en premier."""
    if not lst: return lst
    start %= len(lst)
    return lst[start:] + lst[:start]


def scan_hl(hs, recettes, log=print):
    """(résultats, structures assez rapides pour le passage rapide). Coupée au budget : les structures lues sont gardées."""
    res, quick, t = {}, [], {}

    def timed(c):
        t0 = time.time()
        r = veille.scan(c, recettes.get(c['id']), {})
        t[c['id']] = time.time() - t0
        return r
    ex = ThreadPoolExecutor(WORKERS)
    futs = {ex.submit(timed, c): c for c in hs}
    try:
        for f in as_completed(futs, timeout=BUDGET_HL):
            c = futs[f]
            try:
                res[c['id']] = f.result()
                if t.get(c['id'], 1e9) <= veille.SCAN_MAX_S and res[c['id']][1].get('ok'): quick.append(c['id'])
            except Exception as e: log('hors liste', c['id'], str(e)[:80])
    except Exception as e:
        log('hors liste : lecture coupée', str(e)[:80])
    ex.shutdown(wait=False, cancel_futures=True)
    return res, quick


def main():
    structs = load('structures.json') or list({c['id']: c for c in (load('companies.json') or [])}.values())
    ann = load('annuaire.json') or {}
    ann_keys = {(cid, m.get('k')) for cid, ms in ann.items() for m in ms}
    recettes = merge_dedies(merge_annuaire(load('recettes.json') or {}, ann), load('dedies.json'))
    hs, hr = hors_liste()
    ids = {c['id'] for c in structs}
    hs = [h for h in hs if h['id'] not in ids]
    recettes.update(hr)
    by_url = {}
    for o in (load('offers.json') or {}).get('offers') or []:
        by_url[o.get('url')] = o
        if o.get('url_agg'): by_url[o['url_agg']] = o
    errors, results, pseudo, render, quick = [], {}, [], {}, []
    dom0 = load('domaines.json') or {}
    prev = load('lent.json')
    prev = prev if isinstance(prev, dict) else {}
    rot = dict(prev.get('rot') or {})  # positions gardées d'un passage à l'autre dans lent.json
    cycles = dict(prev.get('cycles') or {})

    # structures renvoyées ici par le passage rapide (trop lentes pour lui), en tête ; puis les hors liste, à partir de la position gardée
    st = load('state.json') or {}
    slow = [c for c in structs if c['id'] in set(st.get('lent_only') or [])]
    queue = slow + rotated(hs, rot.get('hl', 0))
    try:
        results, quick = scan_hl(queue, recettes)
        rot['hl'] = rot.get('hl', 0) + sum(1 for c in queue[len(slow):] if c['id'] in results)
        for cid, (_, info) in results.items():
            if info.get('ok') and not info.get('partial'): cycles[cid] = NOW_ISO
    except Exception as e:
        errors.append('hors liste : ' + str(e)[:150])
    try:
        i = rot.get('agg', 0)
        agregateurs.LAST.clear()
        agg, pseudo = run_aggregators(structs + hs, recettes, i)
        merge_results(results, agg)
        last = agregateurs.LAST
        rot['agg'] = i + 1
        rot['agg_ok'] = rot.get('agg_ok', 0) + last.get('ok', 0)
        rot['agg_ko'] = rot.get('agg_ko', 0) + last.get('ko', 0)
        if last.get('slices') and (i + 1) % last['slices'] == 0:  # toutes les tranches sont passées : cycle complet
            tot = rot['agg_ok'] + rot['agg_ko']
            if tot and rot['agg_ok'] >= CYCLE_OK * tot:
                for s in last.get('sites') or (): cycles[s] = NOW_ISO
            rot['agg_ok'] = rot['agg_ko'] = 0
        for s, n in (last.get('rows') or {}).items():  # sources lues en entier à chaque passage
            if n and s in AGG and s not in (last.get('sites') or ()): cycles[s] = NOW_ISO
    except Exception as e:
        errors.append('agrégateurs : ' + str(e)[:150])
    byid = {c['id']: c for c in structs + hs + pseudo}
    items = [(byid[cid], m) for cid, r in recettes.items() if cid in byid
             for m in r.get('methods', []) if m['m'] == 'render' or (m['m'] == 'watch' and m.get('render'))]
    items.sort(key=lambda x: (x[0]['id'], str(x[1].get('url'))))
    print(f'rendu navigateur : {len(items)} pages', flush=True)
    try:
        if items:
            render = asyncio.run(veille.render_all(rotated(items, rot.get('render', 0)), budget=BUDGET_RENDER))
            rot['render'] = rot.get('render', 0) + sum(len(v) for v in render.values())
    except Exception as e:
        errors.append('rendu navigateur : ' + str(e)[:150])

    # mêmes filtres que veille.py : les postes qu'il écarterait n'ont pas à voyager dans lent.json
    n_in = n_out = 0
    res_out = {}
    for cid, (raw, info) in results.items():
        c = byid.get(cid)
        if c is None: continue
        info['raw_n'] = Counter(x.get('src') for x in raw)
        kept = veille.filter_raw(c, cid, raw, ann_keys)
        n_in += len(raw); n_out += len(kept)
        res_out[cid] = [slim(kept, by_url), info]
    ren_out = {cid: [[slim(veille.filter_raw(byid[cid], cid, g, ann_keys) if e is None else g, by_url), e] for g, e in lst] for cid, lst in render.items() if cid in byid}

    dom = load('domaines.json') or {}
    doc = {'updated': NOW_ISO, 'seconds': round(time.time() - T0), 'errors': errors, 'structs': hs + pseudo, 'results': res_out, 'render': ren_out,
           'domaines': {k: v for k, v in dom.items() if dom0.get(k) != v}, 'rot': rot, 'cycles': cycles, 'fast_ok': [c for c in quick if c in {s['id'] for s in slow}],
           'lines': {'read': n_in, 'kept': n_out}}
    if not res_out and not ren_out:
        print('passage lent vide : lent.json laissé tel quel', errors, flush=True)
        return 1
    size = len(json.dumps(doc, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
    doc['bytes'] = size
    if size > MAX_MB * 1e6:
        print(f'::error title=lent.json trop gros::{size / 1e6:.1f} Mo pour un maximum de {MAX_MB:g} Mo ({n_out} lignes sur {n_in}) : fichier non écrit, le précédent reste en place', flush=True)
        return 2
    save('lent.json', doc)
    print(json.dumps({k: doc[k] for k in ('updated', 'seconds', 'errors', 'lines', 'bytes')}, ensure_ascii=False), f"{len(res_out)} structures, {len(ren_out)} rendues", flush=True)
    return 0


if __name__ == '__main__':
    rc = main()
    sys.stdout.flush(); sys.stderr.flush()
    os._exit(rc)  # une lecture coupée peut tourner encore : tout est enregistré
