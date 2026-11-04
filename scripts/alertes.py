#!/usr/bin/env python3
"""
Alertes par e-mail, lancé après chaque passage du robot.

Variables : ALERTS_URL, ALERTS_SECRET, RESEND_API_KEY, ALERTS_FROM. Sans elles, le script ne fait rien et ne plante pas.
Recherches lues sur GET /api/alertes/export ; chaque recherche = {id, nom, filtres, mode: "instant"|"recap", actif}.
Une offre n'est jamais envoyée deux fois à une même adresse (data/alertes_envoyees.json).
Mode recap : un seul mail groupé par jour, à partir de ALERTS_RECAP_HOUR (heure UTC).
"""
import hashlib, html, json, os, re, sys, datetime as dt
import requests
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from commun import load, save
from taxonomie import metier_of, city_of, norm

RESEND = 'https://api.resend.com/emails'
SENT = 'alertes_envoyees.json'
FIRST_DAYS = 1      # une recherche toute neuve reçoit les offres du dernier jour, pas tout l'existant
WINDOW_H = int(os.environ.get('ALERTS_WINDOW_H', '72'))  # offres détectées depuis moins de 72 h
POSTED_D = int(os.environ.get('ALERTS_POSTED_D', '14'))  # offre publiée depuis longtemps, détectée seulement maintenant : pas une nouveauté
RECAP_HOUR = int(os.environ.get('ALERTS_RECAP_HOUR', '7'))
MAX_PER_MAIL = int(os.environ.get('ALERTS_MAX_PER_MAIL', '40'))
EMAIL_OK = re.compile(r"[^@\s<>\"']+@[^@\s<>\"']+\.[^@\s<>\"']+$")


RENAMED = {'country': {'Dubaï': 'Émirats arabes unis', 'Dubai': 'Émirats arabes unis'}}  # libellés changés depuis l'enregistrement de la recherche


class ExportError(Exception):
    pass


def parse_ts(v):
    """Date en UTC ; une date sans fuseau est lue comme UTC, une date illisible donne None."""
    try:
        d = dt.datetime.fromisoformat(str(v).replace('Z', '+00:00'))
        return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    except Exception:
        return None


def fetch_export(url, secret):
    url = url.rstrip('/')
    if not url.endswith('/api/alertes/export'): url += '/api/alertes/export'
    r = requests.get(url, headers={'Authorization': 'Bearer ' + secret}, timeout=20, allow_redirects=False)
    if 300 <= r.status_code < 400:
        raise ExportError(f"export redirigé vers {str(r.headers.get('location'))[:80]} : l'adresse est sans doute protégée (Cloudflare Access), à exclure pour /api/alertes/export")
    if r.status_code in (401, 403): raise ExportError(f'export refusé ({r.status_code}) : vérifier ALERTS_SECRET côté GitHub et côté Cloudflare')
    if r.status_code == 503: raise ExportError('export indisponible (503) : ALERTS_SECRET absent côté Cloudflare')
    if r.status_code != 200: raise ExportError(f'export en erreur ({r.status_code})')
    try:
        js = r.json()
    except Exception:
        raise ExportError(f"export illisible : réponse non JSON ({str(r.headers.get('content-type'))[:40]}), probablement une page de connexion")
    if not isinstance(js, (dict, list)) or (isinstance(js, dict) and not js.get('comptes') and 'comptes' not in js):
        raise ExportError('export inattendu : champ « comptes » absent')
    return js


def users_of(js):
    """{e-mail: [recherches]}. Le Worker renvoie {comptes: [{email, recherches}]} ; les autres formes restent acceptées."""
    if isinstance(js, dict):
        for k in ('comptes', 'alerts', 'alertes', 'users', 'items', 'data'):
            if isinstance(js.get(k), (dict, list)): return users_of(js[k])
        return {str(k).replace('alerts:', '', 1): v for k, v in js.items() if isinstance(v, list)}
    out = {}
    for u in js if isinstance(js, list) else []:
        if not isinstance(u, dict): continue
        mail = u.get('email') or str(u.get('key') or '').replace('alerts:', '', 1)
        lst = next((u[k] for k in ('alerts', 'alertes', 'recherches', 'searches', 'value') if isinstance(u.get(k), list)), None)
        if mail and lst is not None: out[mail] = lst
    return out


def vals(v):
    """Filtre à choix simple (ancien état) ou multiple : toujours une liste sans valeurs vides."""
    if isinstance(v, (list, tuple, set)): return [x for x in v if x not in (None, '')]
    return [] if v in (None, '', False) else [v]


def matches(o, f, sizes=None):
    """Mêmes règles que TK_match sur le site (filtres enregistrés par TK_snap ; un choix simple d'un ancien état reste accepté)."""
    f = f if isinstance(f, dict) else {}
    na = f.get('inclNA') is not False  # « inclure les non précisées »
    m = o.get('metier') or metier_of(o)
    q = norm(f.get('q'))
    cities = o.get('cities') or []
    if q and q not in norm(' '.join([str(o.get(k) or '') for k in ('company', 'title', 'location')] + [m] + cities)): return False
    ms = vals(f.get('metiers'))
    if ms and m not in ms: return False
    if m == 'Hors finance' and 'Hors finance' not in ms and not f.get('nonFin'): return False
    w = [RENAMED['country'].get(x, x) for x in vals(f.get('country'))]
    if w and o.get('country') not in w: return False
    w = vals(f.get('city'))
    if w and not ({city_of({'location': c, 'country': o.get('country')}) for c in cities} if cities else {city_of(o)}) & set(w): return False
    for k, cur in (('contract', o.get('contract')), ('fam', o.get('fam') or o.get('famx')), ('size', (sizes or {}).get(o.get('company_id')))):
        w = vals(f.get(k))
        if w and cur not in w: return False
    lg = vals(f.get('langs')) + vals(f.get('lang'))
    if lg and (set(lg).isdisjoint(o.get('langs') or []) if o.get('langs') else not na): return False
    if f.get('front') and not o.get('front'): return False
    mo, d = o.get('months'), f.get('dur')
    if d == 'na':
        if mo: return False
    elif d:
        if not mo:
            if not na: return False
        elif (d == 'le3' and mo > 3) or (d == '4_5' and not 4 <= mo <= 5) or (d == '6' and mo != 6) or (d == 'gt6' and mo <= 6): return False
    st = o.get('start')
    if f.get('start') == 'na':
        if st: return False
    elif f.get('start') and (st != f['start'] if st else not na): return False
    if f.get('startFrom') and (st < f['startFrom'] if st else not na): return False
    if f.get('withDl') and not o.get('deadline'): return False
    return True


def fresh(o, now, since):
    """Offre ouverte, vraiment nouvelle, ni doublon ni simple première lecture d'une source."""
    if o.get('status') != 'open' or o.get('dup_of') or o.get('initial'): return False
    seen = parse_ts(o.get('first_seen'))
    if not seen or seen < since or seen < now - dt.timedelta(hours=WINDOW_H): return False
    p = parse_ts(o.get('posted'))
    return not (p and p < now - dt.timedelta(days=POSTED_D))


def wanted(o, now, since, f, sizes):
    """Une ligne invalide est passée, elle n'arrête pas les autres."""
    try: return fresh(o, now, since) and matches(o, f, sizes)
    except Exception as e:
        print('alertes : ligne ignorée', str(o.get('id'))[:20], str(e)[:80])
        return False


def report(**k):
    """État des alertes dans meta.json (lu par le site et le bilan du job). Le passage a déjà écrit ce fichier."""
    m = load('meta.json')
    if isinstance(m, dict):
        m['alertes'] = dict(k, at=dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z'))
        save('meta.json', m, pretty=True)


def line(o):
    lieu = ' / '.join(o.get('cities') or []) or o.get('location') or o.get('country') or ''
    return {'t': o.get('title') or '', 'c': o.get('company') or '', 'l': lieu, 'u': o.get('url') if re.match(r'https?://', str(o.get('url') or ''), re.I) else ''}


def plural(n): return f"{n} nouvelle{'s' if n > 1 else ''} offre{'s' if n > 1 else ''}"


def manage_url():
    """Page de gestion des alertes : ALERTS_MANAGE_URL, sinon le module du site à l'adresse de ALERTS_URL."""
    u = os.environ.get('ALERTS_MANAGE_URL')
    if u: return u
    m = re.match(r'https?://[^/]+', os.environ.get('ALERTS_URL') or '')
    return m.group(0) + '/app/fincv.html' if m else ''


def render(sections):
    """(html, texte) : une section par recherche. Tout ce qui vient des offres est échappé."""
    h, t = ['<div style="font-family:Arial,sans-serif;font-size:15px;color:#1a1a1a">'], []
    for nom, offers, more in sections:
        h.append(f'<h3 style="margin:18px 0 6px">{html.escape(nom)}</h3>')
        t.append(nom)
        for o in offers:
            x = line(o)
            title = html.escape(x['t'])
            a = f'<a href="{html.escape(x["u"], quote=True)}">{title}</a>' if x['u'] else title
            h.append(f'<p style="margin:6px 0">{a}<br><span style="color:#666">{html.escape(x["c"])}{" · " + html.escape(x["l"]) if x["l"] else ""}</span></p>')
            t.append(f"- {x['t']} · {x['c']}" + (f" · {x['l']}" if x['l'] else '') + (f"\n  {x['u']}" if x['u'] else ''))
        if more:
            h.append(f'<p style="margin:6px 0;color:#666">Et {more} autres dans le tracker.</p>')
            t.append(f'Et {more} autres dans le tracker.')
    u = manage_url()
    if u:
        h.append(f'<p style="margin:20px 0 0;font-size:13px;color:#666"><a href="{html.escape(u, quote=True)}">Gérer mes alertes</a></p>')
        t.append(f'\nGérer mes alertes : {u}')
    h.append('</div>')
    return ''.join(h), '\n'.join(t)


def send_mail(key, sender, to, subject, body_html, body_text, idem=None):
    h = {'Authorization': 'Bearer ' + key}
    if idem: h['Idempotency-Key'] = idem  # job coupé entre l'envoi et l'enregistrement : le même mail n'est pas envoyé deux fois
    r = requests.post(RESEND, headers=h, timeout=20,
                      json={'from': sender, 'to': [to], 'subject': subject, 'html': body_html, 'text': body_text})
    r.raise_for_status()


def run(now=None, export=None):
    url, secret, key, sender = (os.environ.get(k) for k in ('ALERTS_URL', 'ALERTS_SECRET', 'RESEND_API_KEY', 'ALERTS_FROM'))
    if not (url and secret and key and sender):
        print('alertes : pas configurées, rien à faire')
        report(configured=False)
        return 0
    now = now or dt.datetime.now(dt.timezone.utc)
    users = users_of(export if export is not None else fetch_export(url, secret))
    doc = load('offers-lite.json') or load('offers.json') or {}
    offers = [o for o in doc.get('offers') or [] if isinstance(o, dict) and o.get('id')]
    sizes = {c.get('id'): {'large': 'Grand nom', 'mid': 'Taille intermédiaire', 'small': 'Petite structure'}.get(c.get('size'), '') for c in load('structures.json') or []}
    log = load(SENT) or {}
    sent, recap, first = log.setdefault('sent', {}), log.setdefault('recap', {}), log.setdefault('first', {})
    n_mails, fails = 0, 0
    today = now.date().isoformat()

    def deliver(mail, subject, secs, is_recap=False):
        nonlocal n_mails, fails
        ids = [o['id'] for _, lst, _, in secs for o in lst]
        h, t = render([(n, lst[:MAX_PER_MAIL], max(0, len(lst) - MAX_PER_MAIL)) for n, lst, _ in secs])
        try:
            idem = hashlib.sha256((mail + '|' + ','.join(sorted(ids))).encode()).hexdigest()[:40]
            send_mail(key, sender, mail, subject, h, t, idem)
        except Exception as e:
            fails += 1
            print('alertes : envoi en échec', str(e)[:120])
            return False
        mine = sent.setdefault(mail, {})
        for i in ids: mine[i] = today
        if is_recap: recap[mail] = today
        save(SENT, log)  # noté tout de suite : un plantage plus loin ne renvoie pas ce mail
        n_mails += 1
        return True

    for mail, searches in users.items():
        if fails >= 3: break
        if not EMAIL_OK.match(str(mail)) or not isinstance(searches, list): continue
        got = sent.get(mail) or {}
        groups = {'instant': [], 'recap': []}
        for s in searches:
            if not isinstance(s, dict) or s.get('id') in (None, '') or s.get('actif') is False: continue
            fk = f"{mail}|{s['id']}"
            first.setdefault(fk, now.isoformat())
            since = (parse_ts(first[fk]) or now) - dt.timedelta(days=FIRST_DAYS)
            groups['recap' if s.get('mode') == 'recap' else 'instant'].append((s, since))
        taken = set(got)
        secs = []
        for s, since in groups['instant']:
            lst = [o for o in offers if o['id'] not in taken and wanted(o, now, since, s.get('filtres'), sizes)]
            taken |= {o['id'] for o in lst}
            if lst: secs.append((str(s.get('nom') or 'Alerte'), lst, None))
        if secs:
            n = sum(len(x[1]) for x in secs)
            deliver(mail, f'Finset : {plural(n)} pour « {secs[0][0]} »' if len(secs) == 1 else f'Finset : {plural(n)}', secs)
        if groups['recap'] and recap.get(mail) != today and now.hour >= RECAP_HOUR:
            secs = []
            for s, since in groups['recap']:
                lst = [o for o in offers if o['id'] not in taken and wanted(o, now, since, s.get('filtres'), sizes)]
                taken |= {o['id'] for o in lst}
                if lst: secs.append((str(s.get('nom') or 'Alerte'), lst, None))
            if secs:
                deliver(mail, f"Finset : récap du jour, {plural(sum(len(x[1]) for x in secs))}", secs, True)
            else:
                recap[mail] = today
    save(SENT, log)
    print(f'alertes : {n_mails} mails envoyés, {fails} échecs, {len(users)} comptes')
    report(configured=True, ok=not fails, mails=n_mails, echecs=fails, comptes=len(users))
    if fails: print(f'::warning title=alertes::{fails} envois en échec')
    return n_mails


def fail_text(e):
    return (str(e) if isinstance(e, ExportError) else f'{type(e).__name__}: {e}')[:200].replace('\n', ' ')


def fail_msg(e):
    return '::warning title=alertes en panne::' + fail_text(e)


if __name__ == '__main__':
    try:
        run()
    except Exception as e:  # une alerte ne doit jamais faire échouer le passage, mais la panne se voit
        print(fail_msg(e))
        try: report(configured=True, ok=False, err=fail_text(e))
        except Exception: pass
    sys.exit(0)
