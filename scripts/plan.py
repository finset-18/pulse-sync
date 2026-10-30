#!/usr/bin/env python3
"""Décide ce que fait le passage en cours (écrit render=0|1 et extra=0|1 dans GITHUB_OUTPUT) :
- render : ouvrir les sites JavaScript dans un navigateur (toutes les RENDER_EVERY_H heures)
- extra  : agrégateurs + sociétés hors liste (toutes les EXTRA_EVERY_H heures)"""
import json, os, sys, datetime as dt

DATA = os.environ.get('DATA_DIR') or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')
NOW = dt.datetime.now(dt.timezone.utc)


def age_h(ts):
    try:
        return (NOW - dt.datetime.fromisoformat(ts.replace('Z', '+00:00'))).total_seconds() / 3600
    except Exception:
        return 1e9


try:
    with open(os.path.join(DATA, 'state.json'), encoding='utf-8') as f:
        st = json.load(f)
except Exception:
    st = {}
force_r = os.environ.get('FORCE_RENDER') == 'true'
force_x = os.environ.get('FORCE_EXTRA') == 'true'
render = force_r or age_h(st.get('last_render')) >= float(os.environ.get('RENDER_EVERY_H', '3'))
extra = force_x or age_h(st.get('last_extra')) >= float(os.environ.get('EXTRA_EVERY_H', '2'))
# un seul gros passage à la fois : le rendu passe en premier, le passage étendu attend le suivant (5 min)
if render and extra and not (force_r and force_x) and age_h(st.get('last_extra')) < 6:
    extra = False
out = os.environ.get('GITHUB_OUTPUT')
lines = f"render={'1' if render else '0'}\nextra={'1' if extra else '0'}\n"
if out:
    with open(out, 'a') as f:
        f.write(lines)
sys.stdout.write(lines)
