#!/usr/bin/env python3
"""
Diagnostic en lecture seule : depuis un serveur GitHub, quelles fiches d'offres le tracker arrive-t-il vraiment à lire ?
Prend au hasard des offres ouvertes sans début ou sans durée, par site, et les lit exactement comme le tracker
(même session, mêmes en-têtes, même chemin de lecture). N'écrit rien dans les données.
Le journal (dépôt public) ne contient que des comptes par site : aucune adresse, aucun titre, aucun texte d'offre.
"""
import os, random, re, sys, time
from collections import Counter, defaultdict
import requests
from commun import load, S, detail_text, bad_page, start_of, start_asap, months_of, data_dates, efc_official, BAD_PAGE
from veille import reread_url

PER_HOST = int(os.environ.get('DIAG_PER_HOST', '15'))
HOSTS_MAX = int(os.environ.get('DIAG_HOSTS', '16'))
random.seed(int(os.environ.get('DIAG_SEED', '7')))


def host_of(u):
    return (re.match(r'https?://([^/]+)', u or '') or [None, '?'])[1].lower()


def raw_status(u):
    """Code HTTP brut et nature de la réponse (sans lever d'erreur)."""
    try:
        r = S.get(u, timeout=20, allow_redirects=True)
        t = r.text or ''
        kind = 'blocage' if BAD_PAGE.search(t[:3000]) else ('court' if len(t) < 2000 else 'contenu')
        if host_of(r.url) != host_of(u): kind += '+redirige'
        return r.status_code, kind
    except requests.RequestException as e:
        return 0, type(e).__name__


def main():
    doc = load('offers.json') or {}
    offers = doc.get('offers') or {}
    if isinstance(offers, dict): offers = list(offers.values())
    inc = [o for o in offers if o.get('status') == 'open' and str(o.get('url', '')).startswith('http')
           and (not (o.get('start') or o.get('start_asap')) or not o.get('months'))]
    by = defaultdict(list)
    for o in inc: by[host_of(o['url'])].append(o)
    hosts = sorted(by, key=lambda h: -len(by[h]))[:HOSTS_MAX]
    print(f'diag : {len(inc)} offres incomplètes, {len(by)} sites ; test de {PER_HOST} fiches sur les {len(hosts)} sites les plus fréquents', flush=True)
    print('diag : site | incomplètes | testées | lecture tracker ok | début trouvé | durée trouvée | date cachée | codes HTTP bruts | erreurs', flush=True)
    tot = Counter()
    for h in hosts:
        sample = random.sample(by[h], min(PER_HOST, len(by[h])))
        c, codes, errs, keys = Counter(), Counter(), Counter(), Counter()
        for o in sample:
            code, kind = raw_status(o['url'])
            codes[f'{code}:{kind}'] += 1
            k = reread_url(o) or ['page', o['url']]
            keys[k[0]] += 1
            try:
                txt = detail_text(k[0], k[1])[0] or ''
                if bad_page(txt): errs['page de blocage'] += 1; continue
                c['ok'] += 1
                if start_of(txt, loose=False) or start_asap(txt): c['debut'] += 1
                if months_of(txt): c['duree'] += 1
                if 'Date de début :' in txt: c['cachee'] += 1
            except requests.HTTPError as e:
                errs[f'HTTP {e.response.status_code if e.response is not None else "?"}'] += 1
            except Exception as e:
                errs[type(e).__name__ + (': ' + str(e)[:40] if isinstance(e, RuntimeError) else '')] += 1
            time.sleep(0.3)
        n = len(sample)
        tot.update({'n': n, **c})
        print(f"diag : {h} | {len(by[h])} | {n} | {c['ok']} | {c['debut']} | {c['duree']} | {c['cachee']} | "
              f"{', '.join(f'{k}×{v}' for k, v in codes.most_common())} | {', '.join(f'{k}×{v}' for k, v in errs.most_common()) or '-'} | chemin {dict(keys)}", flush=True)
    # eFC : l'API de recherche et l'API de candidature (lien officiel) répondent-elles depuis le serveur ?
    efc = [o for o in inc if 'efinancialcareers' in o['url']]
    if efc:
        s = random.sample(efc, min(10, len(efc)))
        ok_api, ok_off = 0, 0
        for o in s:
            k = reread_url(o)
            if k and k[0] == 'efc':
                try:
                    if len(detail_text('efc', k[1])[0] or '') > 300: ok_api += 1
                except Exception: pass
        print(f'diag : eFC, chemin API (description complète) : {ok_api}/{len(s)} lues', flush=True)
    print(f"diag : TOTAL testées {tot['n']} | lues {tot['ok']} | début {tot['debut']} | durée {tot['duree']}", flush=True)


if __name__ == '__main__':
    sys.exit(main())
