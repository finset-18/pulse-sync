#!/usr/bin/env python3
"""
Estimations de début et de durée pour les offres qui ne les écrivent pas. Jamais mélangées aux valeurs écrites :
elles vont dans des champs à part (start_est, months_est) avec leur origine et leur taux d'accord, et le site les affiche « estimé ».

Deux estimateurs, dans cet ordre, calculés à chaque passage sur les offres ouvertes qui écrivent leur date :
- structure   : la même structure, pour le même contrat et le même pays, écrit la même valeur sur ses autres offres (au moins 2, accord >= 60 %)
- comparables : durée seulement ; les offres de même contrat et de même pays écrivent la même durée (au moins 30, accord >= 75 %).
                Pour le début, cet estimateur a été mesuré puis écarté (58 % de justesse en novembre 2026) : seul « structure » date un début.
Rien n'est codé en dur (pas de calendrier « les stages commencent en janvier ») : l'estimation suit ce que les offres écrivent, saison après saison.
La justesse est mesurée à chaque passage en retirant chaque offre datée de son groupe (LAST, repris dans meta.json).
Essai manuel sans rien écrire : DATA_DIR=… python scripts/estime.py
"""
import re
from collections import Counter, defaultdict

MIN_STRUCT, SHARE_STRUCT = 2, 0.60
MIN_COMP, SHARE_COMP = 30, 0.75
EST_KEYS = ('start_est', 'start_est_src', 'start_est_p', 'months_est', 'months_est_src', 'months_est_p')
LAST = {}

_ym = lambda s: int(s[:4]) * 12 + int(s[5:7])
_who = lambda o: o.get('company_id') or o.get('company') or ''
_zone = lambda o: o.get('country') or '?'
K_STRUCT = lambda o: (_who(o), o.get('contract'), _zone(o))  # avec le pays : 81 % de débuts justes au lieu de 76 % (novembre 2026)
K_MONTHS = lambda o: (o.get('contract'), _zone(o))


def _written_start(o): return o.get('start') if o.get('start') and o.get('start_src') not in ('programme', 'saison') else None
def _written_months(o): return o.get('months') if o.get('months') and o.get('months_src') != 'programme' else None


def _mode(values, minn, share, minus=None):
    """Valeur majoritaire d'un groupe (en retirant une valeur pour la mesure), ou None si le groupe est trop petit ou trop partagé."""
    c = Counter(values)
    if minus is not None: c[minus] -= 1
    n = sum(c.values())
    if n < minn: return None
    v, k = c.most_common(1)[0]
    return (v, round(100 * k / n)) if k / n >= share else None


def _groups(offers, val, keys):
    g = [defaultdict(list) for _ in keys]
    for o in offers:
        v = val(o)
        if v is None: continue
        for d, kf in zip(g, keys):
            if kf: d[kf(o)].append(v)
    return g


def _guess(o, groups, keys, minus=None):
    for (d, kf, src, minn, share) in ((groups[0], keys[0], 'structure', MIN_STRUCT, SHARE_STRUCT), (groups[1], keys[1], 'comparables', MIN_COMP, SHARE_COMP)):
        if kf is None or (src == 'structure' and not _who(o)): continue
        r = _mode(d.get(kf(o), ()), minn, share, minus)
        if r: return r[0], src, r[1]
    return None


def _score(offers, val, groups, keys, near):
    """Justesse mesurée : chaque offre datée est retirée de ses groupes, puis estimée comme si elle n'avait pas de date."""
    out = {}
    for o in offers:
        v = val(o)
        if v is None: continue
        g = _guess(o, groups, keys, minus=v)
        if not g: continue
        s = out.setdefault(g[1], [0, 0, 0])
        s[0] += 1; s[1] += g[0] == v; s[2] += near(g[0], v)
    return {k: {'n': n, 'exact_pct': round(100 * e / n), 'proche_pct': round(100 * p / n)} for k, (n, e, p) in out.items()}


def estimer(offers, now_ym):
    """Pose ou retire les champs d'estimation sur les offres ouvertes. Renvoie le bilan (aussi gardé dans LAST)."""
    op = [o for o in offers if o.get('status') == 'open' and not o.get('dup_of')]
    ks, km = (K_STRUCT, None), (K_STRUCT, K_MONTHS)
    gs, gm = _groups(op, _written_start, ks), _groups(op, _written_months, km)
    n_s = n_m = 0
    for o in offers:
        for k in EST_KEYS: o.pop(k, None)
    for o in op:
        if not (o.get('start') or o.get('start_asap')):
            g = _guess(o, gs, ks)
            ys = set(re.findall(r'\b(20\d{2})\b', o.get('title') or ''))
            # jamais une date passée, jamais une année qui contredit celle du titre (« Stage 2027 »)
            if g and g[0] >= now_ym and (len(ys) != 1 or g[0][:4] in ys):
                o['start_est'], o['start_est_src'], o['start_est_p'] = g; n_s += 1
        if not o.get('months') and o.get('contract') != 'Graduate':  # un graduate programme est une embauche : pas de durée
            g = _guess(o, gm, km)
            if g: o['months_est'], o['months_est_src'], o['months_est_p'] = g; n_m += 1
    n = len(op) or 1
    has_s = lambda o: bool(o.get('start') or o.get('start_asap'))
    LAST.clear()
    LAST.update({'debut_estimes': n_s, 'duree_estimees': n_m,
                 'debut_ecrit_ou_estime_pct': round(100 * sum(1 for o in op if has_s(o) or o.get('start_est')) / n, 1),
                 'duree_ecrite_ou_estimee_pct': round(100 * sum(1 for o in op if o.get('months') or o.get('months_est')) / n, 1),
                 'par_origine': {'debut': dict(Counter(o['start_est_src'] for o in op if o.get('start_est'))), 'duree': dict(Counter(o['months_est_src'] for o in op if o.get('months_est')))},
                 'justesse_debut': _score(op, _written_start, gs, ks, lambda a, b: abs(_ym(a) - _ym(b)) <= 1),
                 'justesse_duree': _score(op, _written_months, gm, km, lambda a, b: abs(a - b) <= 1)})
    return LAST


if __name__ == '__main__':
    import json
    from commun import load, NOW
    doc = load('offers.json') or {}
    offers = doc.get('offers') or []
    if isinstance(offers, dict): offers = list(offers.values())
    print(json.dumps(estimer(offers, NOW.strftime('%Y-%m')), ensure_ascii=False, indent=1))
