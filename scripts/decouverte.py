#!/usr/bin/env python3
"""
découverte : pour chaque structure, un vrai navigateur (Chromium) ouvre son site et
trouve la meilleure façon de lire ses offres. Le résultat (« recette ») sert ensuite au robot
de veille, qui n'a plus besoin de navigateur pour la plupart des structures.

Méthodes possibles, de la plus rapide à la plus lourde :
  ats    plateforme de recrutement connue (Workday, Greenhouse, Oracle…), lue par son API
  api    API cachée du site (JSON que la page appelle pour afficher ses offres), rejouée sans navigateur
  html   page qui liste ses offres directement dans son code HTML
  render page qui n'affiche ses offres qu'avec JavaScript : ouverte par un navigateur une fois par jour
  watch  page « Nous rejoindre » sans liste d'offres : le robot surveille les stages annoncés dans le texte
  wttj   page entreprise sur Welcome to the Jungle

Variables : SHARD / SHARDS (lots en parallèle), MODE=all|auto, CONC (pages en parallèle), BUDGET_MIN.
Écrit data/recettes-part-<SHARD>.json (fusionné ensuite dans data/recettes.json par fusion.py).
"""
import asyncio, json, os, re, sys, time, traceback
from collections import Counter
from urllib.parse import urljoin, urlparse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from commun import *  # noqa
from commun import _first, _norm

SHARD = int(os.environ.get('SHARD', '0')); SHARDS = int(os.environ.get('SHARDS', '1'))
MODE = os.environ.get('MODE', 'auto')
CONC = int(os.environ.get('CONC', '6'))
BUDGET = float(os.environ.get('BUDGET_MIN', '100')) * 60
ONLY = [x for x in os.environ.get('ONLY', '').split(',') if x]
RECHECK_DAYS = 21
T0 = time.time()

LIST_WORDS = re.compile(r"offres?|jobs?\b|job openings|openings|positions?|opportunit|vacanc|postes|internships?|\bstages?\b|students?|[ée]tudiants?|graduates?|"
                        r"early[- ]careers?|campus|universit|see all|view all|voir (toutes )?les|current|open roles|nos offres|postuler|apply|search jobs|"
                        r"rechercher|programmes?|jeunes dipl", re.I)
CAREER_WORDS = re.compile(r"carri[eè]re|careers?|rejoign|join\b|nous rejoindre|recrut|recruit|emplois?|travailler|work (with|at|for) us|talents?|karriere|"
                          r"people|working at|life at|vie chez|join the team|hiring", re.I)
SKIP_HREF = re.compile(r"linkedin|facebook|twitter|x\.com|instagram|youtube|glassdoor|indeed|mailto:|tel:|javascript:|/login|signin|sign-in|privacy|cookie|"
                       r"mentions-legales|legal|/press|/news|/media|/blog|\.(jpg|png|gif|zip|docx?)$", re.I)
COMMON_PATHS = ['/careers', '/carrieres', '/nous-rejoindre', '/rejoignez-nous', '/recrutement', '/join-us', '/jobs', '/emplois', '/en/careers',
                '/fr/carrieres', '/fr/nous-rejoindre', '/fr/recrutement', '/careers/', '/join', '/work-with-us', '/en/join-us', '/carriere', '/talents']
BLOCK_TITLE = re.compile(r"access denied|just a moment|attention required|captcha|are you a robot|request blocked|forbidden|pardon our interruption", re.I)
HDR_KEEP = re.compile(r'^(content-type|accept|x-requested-with|x-csrf-token|csrf-token|x-api-key|apikey|x-algolia-[\w-]+|x-[\w-]*(?:site|locale|tenant|app|lang)[\w-]*|referer|origin|authorization)$', re.I)


async def launch(pw):
    return await pw.chromium.launch(headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'])


async def _route(route):
    try:
        if route.request.resource_type in ('image', 'media', 'font'): await route.abort()
        else: await route.continue_()
    except Exception:
        pass


async def new_ctx(browser):
    ctx = await browser.new_context(user_agent=UA, locale='fr-FR', viewport={'width': 1366, 'height': 900}, ignore_https_errors=True,
                                    extra_http_headers={'Accept-Language': 'fr-FR,fr;q=0.9,en;q=0.8'})
    await ctx.route('**/*', _route)
    return ctx


def log(*a):
    print(f'[{time.time() - T0:6.0f}s]', *a, flush=True)


def stable_id_tpl(ids, hrefs):
    """Modèle d'adresse des fiches (ex. https://site/jobs/{id}) déduit des liens de la page."""
    cnt = Counter()
    for i in ids[:40]:
        s = str(i)
        if len(s) < 3: continue
        for h in hrefs:
            if s in h and not h.endswith(s + s):
                cnt[h.replace(s, '{id}', 1)] += 1
                break
    if not cnt: return None
    t, n = cnt.most_common(1)[0]
    return t if (n >= 2 or len(ids) == 1) else None


def spec_from_request(req, ctype):
    body = req.post_data
    if body and 'json' in (ctype or '').lower():
        try: body = json.loads(body)
        except Exception: pass
    elif body and body.strip().startswith(('{', '[')):
        try: body = json.loads(body)
        except Exception: pass
    h = {}
    try:
        for k, v in (req.headers or {}).items():
            if HDR_KEEP.match(k) and len(v) < 2000: h[k] = v
    except Exception:
        pass
    return {'method': req.method, 'url': req.url, 'body': body, 'headers': h}


class Visit:
    def __init__(self):
        self.urls = []          # toutes les requêtes (pour repérer les plateformes)
        self.jsons = []         # (spec, texte) des réponses JSON
        self.status = None
        self.final = None
        self.html = ''
        self.frames = []
        self.title = ''


async def visit(ctx, url, timeout=25000):
    v = Visit()
    page = await ctx.new_page()
    pending = []

    page.on('pageerror', lambda e: None)

    async def on_response(resp):
        try:
            req = resp.request
            v.urls.append(req.url)
            if req.resource_type not in ('xhr', 'fetch') or resp.status != 200: return
            ct = (resp.headers or {}).get('content-type', '')
            if not re.search(r'json|javascript|text/plain', ct, re.I): return
            txt = await resp.text()
            if 50 < len(txt) < 4_000_000:
                v.jsons.append((spec_from_request(req, req.headers.get('content-type', '')), txt))
        except Exception:
            pass

    page.on('response', lambda r: pending.append(asyncio.ensure_future(on_response(r))))
    page.on('framenavigated', lambda f: v.urls.append(f.url))
    try:
        resp = await page.goto(url, wait_until='domcontentloaded', timeout=timeout)
        v.status = resp.status if resp else None
        try: await page.wait_for_load_state('networkidle', timeout=8000)
        except Exception: pass
        try:
            await page.mouse.wheel(0, 4000); await page.wait_for_timeout(1500)
            await page.mouse.wheel(0, 8000); await page.wait_for_timeout(1000)
        except Exception: pass
        v.final = page.url
        v.title = (await page.title()) or ''
        v.html = await page.content()
        for fr in page.frames[1:6]:
            try:
                v.urls.append(fr.url)
                v.frames.append((fr.url, await fr.content()))
            except Exception:
                pass
        if pending: await asyncio.wait(pending, timeout=5)
    finally:
        try: await page.close()
        except Exception: pass
    return v


def static_get(url):
    try:
        r = S.get(url, timeout=12)
        if r.status_code >= 400: return None, None
        fix_enc(r)
        return r.text[:3_000_000], r.url
    except Exception:
        return None, None


def check_ats(c, k, v):
    try:
        res = FETCHERS[k](c, v)
        return isinstance(res, list)
    except Exception:
        return False


def try_api(c, spec, text, page_url, hrefs):
    """Une réponse JSON contient-elle une liste d'offres rejouable sans navigateur ? Renvoie (recette, rejouable)."""
    try: js = parse_json(text)
    except Exception: return None, False
    path, score = best_job_list(js)
    frags = html_in_json(js)
    nfrag = sum(len(page_jobs(c, f, page_url)) + count_joblinks(f, page_url) for f in frags[:5]) if frags else 0
    if path is None and not nfrag: return None, False
    rec = {'m': 'api', 'req': spec, 'path': path, 'base': page_url, 'html': bool(nfrag and path is None), 'score': round(score, 1)}
    if path is not None:
        items = get_path(js, path)
        mapped = [map_item(d, page_url) for d in items[:50]]
        if sum(1 for o in mapped if o['url']) < 0.5 * max(1, len(mapped)):
            # pas d'adresse dans le JSON : on la reconstruit à partir des liens de la page
            ik = None
            for d in items[:5]:
                ik = _first(d, ID_KEYS)[0] or _first(d, ['slug', 'urlTitle', 'seoName'])[0]
                if ik: break
            vals = [d.get(ik) for d in items if ik and d.get(ik) not in (None, '')]
            tpl = stable_id_tpl(vals, hrefs)
            if not tpl:
                for alt in ('slug', 'urlTitle', 'seoName', 'externalPath', 'jobSlug'):
                    vals = [d.get(alt) for d in items if isinstance(d.get(alt), str)]
                    tpl = stable_id_tpl(vals, hrefs) if vals else None
                    if tpl: ik = alt; break
            if not tpl: return None, False
            rec['tpl'], rec['tkey'] = tpl, ik
        n_items = len(items)
    else:
        n_items = nfrag
    kw = find_param(spec, KW_KEYS)
    if kw: rec['kw'] = kw
    pl = find_param(spec, PAGE_KEYS)
    if pl:
        rec['page'] = {'loc': pl, 'step': 1}
    else:
        ol = find_param(spec, OFFSET_KEYS)
        if ol:
            sl = find_param(spec, SIZE_KEYS)
            try: step = int(get_param(spec, sl)) if sl else n_items
            except Exception: step = n_items
            if step: rec['page'] = {'loc': ol, 'step': step}
    rec['n'] = n_items
    # rejouable sans navigateur ?
    try:
        js2 = parse_json(send(spec).text)
        ok = len(items_from(js2, dict(rec, _c=c), page_url)) >= 1
    except Exception:
        ok = False
    return rec, ok


def analyse(c, v, rec):
    """Méthodes trouvées sur une page visitée."""
    found = []
    blob = '\n'.join(v.urls) + '\n' + (v.html or '') + '\n'.join(h for _, h in v.frames)
    for k, val in detect_ats(blob).items():
        if not ats_ok(c, k, val): continue
        if check_ats(c, k, val):
            found.append({'m': 'ats', 'k': k, 'v': list(val) if isinstance(val, tuple) else val})
    if found: return found, True
    hrefs = [urljoin(v.final or '', h) for h, _ in A_RE.findall(v.html or '')]
    for fu, fh in v.frames: hrefs += [urljoin(fu, h) for h, _ in A_RE.findall(fh or '')]
    best = None
    for spec, txt in v.jsons:
        if re.search(r'google|analytics|doubleclick|hotjar|onetrust|cookie|consent|sentry|segment|intercom|hubspot|facebook|linkedin|gtm|matomo|clarity', spec['url'], re.I):
            continue
        r, ok = try_api(c, spec, txt, v.final or spec['url'], hrefs)
        if r and (best is None or (ok, r.get('score', 0)) > (best[1], best[0].get('score', 0))): best = (r, ok)
    if best and best[1]:
        return [best[0]], True
    pages = [(v.final, v.html)] + list(v.frames)
    n_rend = sum(count_joblinks(h, u) for u, h in pages)
    st_html, st_url = static_get(v.final) if v.final else (None, None)
    n_stat = count_joblinks(st_html, st_url) + len(jsonld_jobs(st_html or '', st_url)) if st_html else 0
    if st_html and (n_stat >= 2 or page_jobs(c, st_html, st_url)):
        return [{'m': 'html', 'url': st_url}], True
    for fu, fh in v.frames:
        fs, fsu = static_get(fu)
        if fs and (count_joblinks(fs, fsu) >= 2 or page_jobs(c, fs, fsu)):
            return [{'m': 'html', 'url': fsu}], True
    if n_rend >= 2 or any(page_jobs(c, h, u) for u, h in pages):
        r = {'m': 'render', 'url': v.final}
        if best: r['api'] = {k: best[0][k] for k in ('req', 'path', 'base', 'tpl', 'tkey', 'html') if k in best[0]}
        return [r], True
    if best and best[0].get('n', 0) >= 1:
        r = {'m': 'render', 'url': v.final, 'api': {k: best[0][k] for k in ('req', 'path', 'base', 'tpl', 'tkey', 'html') if k in best[0]}}
        return [r], True
    # page carrières sans liste : surveiller les stages annoncés dans le texte
    path = urlparse(v.final or '').path
    m_st = mentions(st_html or '', st_url) if st_html else []
    m_re = mentions(v.html or '', v.final)
    careerish = bool(CAREER_WORDS.search(v.title + ' ' + path) or LIST_WORDS.search(path))
    if careerish or m_st or m_re:
        if st_html and (len(m_st) >= len(m_re)) and len(clean(st_html)) > 0.5 * len(clean(v.html or '')):
            return [{'m': 'watch', 'url': st_url, 'render': False, 'seen': len(m_st), 'career': careerish}], False
        return [{'m': 'watch', 'url': v.final, 'render': True, 'seen': len(m_re), 'career': careerish}], False
    return [], False


def follow_links(c, v, seen, limit=3):
    """Liens à suivre depuis une page : listes d'offres d'abord, puis pages carrières."""
    out = []
    root = host_of(site_of(c)) or host_of(v.final)
    for href, txt in A_RE.findall(v.html or ''):
        u = urljoin(v.final or '', html.unescape(href.strip())).split('#')[0]
        if not u.startswith('http') or u in seen or SKIP_HREF.search(u): continue
        t = clean(txt)[:80]
        uh = host_of(u)
        same = uh == root or uh.endswith('.' + root) or root.endswith('.' + uh)
        ats = bool(detect_ats(u) or ATS_HOSTS.search(uh))
        if not (same or ats): continue
        s = 0
        if LIST_WORDS.search(t) or LIST_WORDS.search(urlparse(u).path): s += 3
        if CAREER_WORDS.search(t) or CAREER_WORDS.search(urlparse(u).path): s += 2
        if ats: s += 4
        if s: out.append((s, u))
    out.sort(key=lambda x: -x[0])
    res = []
    for s, u in out:
        if u not in res: res.append(u)
        if len(res) >= limit: break
    return res


def wttj_lookup(c):
    """Page entreprise Welcome to the Jungle de la structure, si elle existe (vérifiée par le site web)."""
    name = c.get('name', '')
    dom = host_of(site_of(c))
    cands = []
    for s in (slugify(name), slugify(re.sub(r'\b(sas|sa|sarl|group|groupe|partners|capital|& co|and co)\b', ' ', name, flags=re.I)),
              slugify(name.split('(')[0]), slugify(dom.split('.')[0] if dom else ''), slugify(name).replace('-', '')):
        if s and s not in cands and len(s) >= 3: cands.append(s)
    for s in cands[:5]:
        org = wttj_org(s)
        if not org: continue
        web = ' '.join(str(org.get(k) or '') for k in ('website', 'website_url', 'url', 'websites'))
        oname = _norm(org.get('name') or '')
        if (dom and dom.split('.')[0] in web.lower()) or (oname and oname == _norm(name)):
            try:
                j_wttj(c, s)
                return s
            except Exception:
                return None
    return None


async def discover(browser, c, seeds):
    rec = {'id': c['id'], 'checked': NOW_ISO, 'start': c.get('url'), 'methods': [], 'careers': None, 'visits': 0, 'blocked': False, 'err': ''}
    # 1. plateforme déjà connue : on vérifie qu'elle répond, pas besoin de navigateur
    for k, val in (seeds.get('ats') or {}).items():
        val = tuple(val) if isinstance(val, list) else val
        if ats_ok(c, k, val) and check_ats(c, k, val):
            rec['methods'].append({'m': 'ats', 'k': k, 'v': list(val) if isinstance(val, tuple) else val})
            rec['careers'] = c.get('url')
            return rec
    ctx = await new_ctx(browser)
    try:
        queue = [u for u in [c.get('url')] + list(seeds.get('more') or []) if u]
        seen, weak = set(), []
        while queue and rec['visits'] < 5:
            u = queue.pop(0)
            if u in seen: continue
            seen.add(u)
            try:
                v = await visit(ctx, u)
            except Exception as e:
                rec['err'] = str(e)[:120]
                continue
            rec['visits'] += 1
            seen.add(v.final)
            if (v.status and v.status in (401, 403, 429, 503)) or BLOCK_TITLE.search(v.title or ''):
                rec['blocked'] = True   # le site refuse les robots : on n'insiste pas, il reste dans Alertes
                break
            meth, strong = await asyncio.to_thread(analyse, c, v, rec)
            if meth and (CAREER_WORDS.search(v.final or '') or LIST_WORDS.search(v.final or '') or strong): rec['careers'] = rec['careers'] or v.final
            if strong:
                rec['methods'] = meth
                rec['careers'] = rec['careers'] or v.final
                break
            weak += meth
            nxt = follow_links(c, v, seen)
            queue = nxt + queue if rec['visits'] <= 2 else queue
        if not rec['methods'] and not rec['blocked'] and not any(CAREER_WORDS.search(x) for x in seen if x):
            # 2. pas de lien carrières visible : on essaie les adresses habituelles
            root = 'https://' + (host_of(site_of(c)) or host_of(c.get('url')))
            for p in COMMON_PATHS:
                h, fu = await asyncio.to_thread(static_get, root + p)
                if h and fu and CAREER_WORDS.search(fu + ' ' + clean(h)[:3000]) and host_of(fu) == host_of(root):
                    try:
                        v = await visit(ctx, fu); rec['visits'] += 1
                        meth, strong = await asyncio.to_thread(analyse, c, v, rec)
                        rec['careers'] = rec['careers'] or v.final
                        if strong: rec['methods'] = meth
                        else: weak += meth
                    except Exception:
                        pass
                    break
        if not rec['methods'] and weak:
            weak.sort(key=lambda m: (m.get('seen', 0), m.get('career', False), not m.get('render')), reverse=True)
            rec['methods'] = weak[:1]
            rec['careers'] = rec['careers'] or (weak[0]['url'] if weak[0].get('career') else None)
    finally:
        await ctx.close()
    # 3. Welcome to the Jungle, en complément (beaucoup de boutiques et de fonds y publient)
    if not any(m['m'] in ('ats', 'api', 'html') for m in rec['methods']):
        s = await asyncio.to_thread(wttj_lookup, c)
        if s: rec['methods'].append({'m': 'wttj', 'slug': s})
    return rec


def todo_list(structs, recettes, revoir):
    out = []
    for i, c in enumerate(sorted(structs, key=lambda x: x['id'])):
        if i % SHARDS != SHARD: continue
        if ONLY and c['id'] not in ONLY: continue
        r = recettes.get(c['id'])
        if MODE == 'all' or ONLY or not r: out.append((0, c)); continue
        if MODE == 'retry':   # deuxième essai : sites en erreur (délai, coupure) ou sans méthode ; les sites qui bloquent les robots restent en Alertes
            if not r.get('blocked') and (r.get('err') or not r.get('methods')): out.append((0, c))
            continue
        age = (NOW - dt.datetime.fromisoformat(r['checked'].replace('Z', '+00:00'))).days
        ms = {m['m'] for m in r.get('methods', [])}
        if c['id'] in revoir: out.append((1, c))
        elif (not ms or ms <= {'watch', 'wttj'}) and age >= 14: out.append((2, c))   # rien d'automatique : on regarde toutes les 2 semaines
        elif age >= 30: out.append((3, c))
    out.sort(key=lambda x: x[0])
    return [c for _, c in out]


async def main():
    from playwright.async_api import async_playwright
    structs = load('structures.json') or []
    companies = {c['id']: c for c in (load('companies.json') or [])}
    recettes = load('recettes.json') or {}
    revoir = set(load('a_revoir.json') or [])
    todo = todo_list(structs, recettes, revoir)
    log(f'lot {SHARD + 1}/{SHARDS} : {len(todo)} structures à découvrir (mode {MODE})')
    part, done = {}, 0
    out_path = f'recettes-part-{SHARD}.json'
    sem = asyncio.Semaphore(CONC)
    async with async_playwright() as pw:
        browser = await launch(pw)

        async def one(c):
            nonlocal done
            if time.time() - T0 > BUDGET: return
            async with sem:
                if time.time() - T0 > BUDGET: return
                seeds = companies.get(c['id'], {})
                seeds = {'ats': seeds.get('ats') or {}, 'more': [seeds.get('url2')] if seeds.get('url2') else []}
                try:
                    r = await asyncio.wait_for(discover(browser, c, seeds), timeout=150)
                except Exception as e:
                    r = {'id': c['id'], 'checked': NOW_ISO, 'start': c.get('url'), 'methods': [], 'err': ('délai dépassé' if isinstance(e, asyncio.TimeoutError) else str(e)[:120])}
                part[c['id']] = r
                done += 1
                ms = ','.join(m['m'] + (':' + m['k'] if m.get('k') else '') for m in r['methods']) or '-'
                log(f"{done:4d} {c['id'][:28]:28s} {ms:18s} {('BLOQUÉ ' if r.get('blocked') else '') + (r.get('err') or '')[:60]}")
                if done % 40 == 0: save(out_path, part)
        async def garde():
            # filet de sécurité : un site qui ne répond plus ne doit pas faire perdre tout le lot
            while True:
                await asyncio.sleep(30)
                if time.time() - T0 > BUDGET + 480:
                    save(out_path, part); log(f'arrêt de sécurité : {len(part)} structures enregistrées')
                    os._exit(0)
        asyncio.ensure_future(garde())
        await asyncio.gather(*(one(c) for c in todo))
        await browser.close()
    save(out_path, part)
    cnt = Counter((r['methods'][0]['m'] if r['methods'] else 'aucune') for r in part.values())
    log('bilan du lot :', dict(cnt), f'- {len(todo) - len(part)} non traitées (temps écoulé)')


if __name__ == '__main__':
    asyncio.run(main())
