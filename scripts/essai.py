#!/usr/bin/env python3
"""
Essai en lecture seule des sources, depuis un serveur GitHub (workflow diag, mode « sources »). N'écrit aucune donnée.
Pour chaque recette dédiée (data/dedies.json, plus les candidates ci-dessous) : la source répond-elle, combien d'offres,
combien de stages gardés, la fiche d'un stage se lit-elle ? Puis le balayage complet de Welcome to the Jungle et un lot LinkedIn à blanc.
Le journal (dépôt public) ne contient que des comptes : aucune adresse, aucun titre, aucun texte d'offre.
"""
import os, sys, threading, time
from collections import Counter
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import commun
from commun import load, FETCHERS, keep_offer, detail_text, stage_in_text, bad_page, months_of
from sources import merge_dedies

MAX_S = float(os.environ.get('ESSAI_MAX_S', '150'))  # temps maximum d'une source

# recettes à l'essai avant d'entrer dans data/dedies.json (sites de recrutement des cabinets d'audit et de transaction services)
CANDIDATES = {
    'ey': [{'m': 'ats', 'k': 'yello', 'v': ['eyglobal.yello.co', 'c1riT--B2O-KySgYWsZO1Q', '29994=France', '29960=Belgium', '30031=Luxembourg', '30080=Switzerland', '30047=Netherlands', '29997=Germany']}],
    'kpmg': [{'m': 'ats', 'k': 'talentbrew', 'v': 'https://emplois.kpmg.fr'}],
    'pwc': [{'m': 'ats', 'k': 'workday', 'v': ['pwc.wd3.myworkdayjobs.com', 'pwc', 'Global_Campus_Careers']}],
    'mazars': [{'m': 'ats', 'k': 'smartrecruiters', 'v': 'MAZARS'}],
    'gt': [{'m': 'ats', 'k': 'recruitee', 'v': 'carrieresfrance.grant-thornton.fr'}],
    'bdo': [{'m': 'ats', 'k': 'teamtailor', 'v': 'recrutement.bdo.fr'}],
}


def recipes():
    only = [x for x in os.environ.get('ESSAI_IDS', '').split(',') if x]
    ded = load('dedies.json') or {}
    out = {}
    for cid, v in list(CANDIDATES.items()) + [(k, (v.get('methods') if isinstance(v, dict) else v)) for k, v in ded.items() if k not in CANDIDATES]:
        if only and cid not in only: continue
        out[cid] = [m for m in v or [] if isinstance(m, dict) and m.get('m') == 'ats' and m.get('k') in FETCHERS]
    return out


def main():
    structs = load('structures.json') or []
    byid = {c['id']: c for c in structs}
    urls = set()
    for o in (load('offers.json') or {}).get('offers') or []:
        if o.get('status') == 'open': urls.add(o.get('url')); urls.add(o.get('url_agg'))
    print('essai : structure | plateforme | secondes | offres lues | stages gardés | à vérifier (intitulé sans « stage ») | déjà connues | fiches lues / essayées | fiches « stage » | avec durée | erreur', flush=True)
    for cid, meths in recipes().items():
        c = byid.get(cid) or {'id': cid, 'name': cid}
        for m in meths:
            v = tuple(m['v']) if isinstance(m['v'], list) else m['v']
            t0, err, raw = time.time(), '', []
            box = {}

            def work():
                try: box['raw'] = FETCHERS[m['k']](c, v)
                except Exception as e: box['err'] = type(e).__name__ + ' ' + str(e)[:60].replace('http', 'h..p')
            th = threading.Thread(target=work, daemon=True); th.start(); th.join(MAX_S)
            if th.is_alive(): err = f'plus de {MAX_S:.0f} s : abandonné'
            raw, err = box.get('raw') or [], err or box.get('err', '')
            for x in raw: x['src'] = m['k']
            commun.CAND_PENDING.clear()
            kept = [x for x in raw if keep_offer(x)]
            pending = len(commun.CAND_PENDING)
            known = sum(1 for x in kept if x.get('url') in urls)
            ok = stg = dur = tried = 0
            for x in kept[:4]:
                if x.get('desc') and len(x['desc']) > 400:
                    tried += 1; ok += 1; stg += bool(stage_in_text(x['desc'])); dur += bool(months_of(x['desc'])); continue
                d = x.get('detail') or ('page', x['url'])
                tried += 1
                try:
                    txt = detail_text(d[0], d[1])[0] or ''
                    if bad_page(txt): continue
                    ok += 1; stg += bool(stage_in_text(txt)); dur += bool(months_of(txt))
                except Exception:
                    pass
            print(f"essai : {cid} | {m['k']} | {time.time() - t0:.0f} | {len(raw)} | {len(kept)} | {pending} | {known} | {ok}/{tried} | {stg} | {dur} | {err}", flush=True)
    commun.CAND_PENDING.clear()

    if os.environ.get('ESSAI_WTTJ', '1') == '1':
        import plus, veille
        from agregateurs import attach
        t0 = time.time()
        try:
            rows = [x for x in plus.s_wttj() if x['title'] and x['company'] and x['url']]
            n_rows = len(rows)
            recettes = merge_dedies(load('recettes.json') or {}, load('dedies.json'))
            results, pseudo = attach(rows, structs, recettes)
            byid2 = dict(byid); byid2.update({p['id']: p for p in pseudo})
            kept = new = hl = 0
            fams = Counter()
            for rid, (raw, info) in results.items():
                c = byid2.get(rid)
                if c is None: continue
                k = veille.filter_raw(c, rid, raw)
                kept += len(k); new += sum(1 for x in k if x['url'] not in urls)
                if rid.startswith('h_'): hl += len(k)
                else: fams[c.get('fam') or '?'] += len(k)
            print(f'essai : wttj balayage | {time.time() - t0:.0f} s | {n_rows} annonces retenues | {len(results)} structures ou sociétés | {kept} stages gardés | {new} pas encore connus | {hl} hors liste', flush=True)
            print('essai : wttj par famille | ' + ', '.join(f'{k} {v}' for k, v in fams.most_common(12)), flush=True)
        except Exception as e:
            print('essai : wttj en échec |', type(e).__name__, str(e)[:100].replace('http', 'h..p'), flush=True)

    if os.environ.get('ESSAI_LINKEDIN', '1') == '1':
        os.environ['LI_DRY'] = '1'
        try:
            import linkedin
            linkedin.main()
        except Exception as e:
            print('essai : linkedin en échec |', type(e).__name__, str(e)[:100], flush=True)


if __name__ == '__main__':
    main()
    sys.stdout.flush()
    os._exit(0)
