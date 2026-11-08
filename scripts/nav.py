#!/usr/bin/env python3
"""
Lecture des fiches au navigateur (job « nav ») : eFinancialCareers, Welcome to the Jungle et Indeed bloquent ou tronquent
la lecture sans navigateur, alors que la date de début et la durée sont écrites sur la page (« Date prévue de prise de fonction »,
« à partir de Janvier 2027 »…). Ce job ouvre ces fiches dans Chromium sans écran, comme un visiteur.

Quand la page est bloquée ou ne donne pas la date, il suit le lien officiel de l'employeur (bouton « Postuler » vers Workday,
Greenhouse, Lever…, lien direct donné par Indeed, ou lien officiel d'eFinancialCareers) et lit cette fiche à la place.

Il ne touche pas aux offres : il écrit data/dates_nav.json, que le job sync reprend à son passage suivant.
Les dates ne sont gardées que si elles sont écrites (mêmes règles que le robot). Rien du texte des offres n'est affiché (dépôt public).
"""
import asyncio, json, os, re, sys, time
from commun import (load, save, NOW, NOW_ISO, start_with_title, months_with_title, start_asap, date_ctx, data_dates,
                    bad_page, direct_ok, efc_official, get, clean)

V = 1  # version de la lecture : la monter relit tout
VIAS = ('efc', 'wttj_search', 'wttj', 'indeed')
BUDGET_S = float(os.environ.get('NAV_BUDGET_S', '2700'))
CONC = int(os.environ.get('NAV_CONC', '4'))
RETRY_DAYS = 7  # une fiche lue sans date est relue au bout d'une semaine (l'employeur complète parfois l'annonce)
UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) '
      'Chrome/130.0.0.0 Safari/537.36')
BLOCK = re.compile(r"just a moment|verify you are human|v[ée]rifi(?:ez|cation) que vous [êe]tes|captcha|access denied|"
                   r"request blocked|security check|unusual traffic|enable javascript and cookies", re.I)


def written(o):
    s_ok = (o.get('start') and o.get('start_src') not in ('programme', 'saison')) or o.get('start_asap')
    m_ok = o.get('months') and o.get('months_src') != 'programme'
    return bool(s_ok), bool(m_ok)


def efc_link(o):
    """Lien officiel de l'employeur pour une offre eFinancialCareers (son API de recherche donne l'identifiant de candidature)."""
    m = re.search(r'\.id(\d+)', o.get('url') or '')
    if not m: return ''
    jid, title = m.group(1), o.get('title') or ''
    for q in dict.fromkeys([title.split(' - ')[0][:80], title[:80]]):
        try:
            js = get('https://job-search-api.efinancialcareers.com/v1/efc/jobs/search',
                     params={'q': q, 'page': 1, 'pageSize': 50, 'language': 'en'}).json()
        except Exception:
            return ''
        for j in js.get('data') or []:
            if str(j.get('jobId')) == jid: return efc_official(j.get('id'))
    return ''


async def _route(route):
    try:
        if route.request.resource_type in ('image', 'media', 'font'): await route.abort()
        else: await route.continue_()
    except Exception:
        pass


async def read(ctx, url):
    """(texte, html, liens officiels) de la page ; texte None si elle est bloquée ou vide (les liens officiels restent utiles)."""
    page = await ctx.new_page()
    try:
        await page.goto(url, wait_until='domcontentloaded', timeout=30000)
        try: await page.wait_for_load_state('networkidle', timeout=6000)
        except Exception: pass
        for sel in ('#onetrust-accept-btn-handler', 'button:has-text("Tout refuser")', 'button:has-text("Refuser")',
                    'button:has-text("Reject all")', 'button:has-text("Accepter")'):
            try:
                b = page.locator(sel).first
                if await b.is_visible(timeout=300): await b.click(timeout=1000); break
            except Exception:
                pass
        for sel in ('button:has-text("Voir plus")', 'button:has-text("Show more")', 'button:has-text("Lire la suite")'):
            try:
                b = page.locator(sel).first
                if await b.is_visible(timeout=300): await b.click(timeout=1000)
            except Exception:
                pass
        text = clean(await page.inner_text('body'))
        html = await page.content()
        links = await page.eval_on_selector_all('a[href]', 'els => els.map(e => e.href)')
        off = [u for u in dict.fromkeys(links) if direct_ok(u)][:5]
        if bad_page(text) or (len(text) < 4000 and BLOCK.search(text[:2000])): return None, None, off
        return text, html, off
    finally:
        await page.close()


def judge(o, text, html):
    t = (o.get('title') or '') + ' . ' + text + data_dates(html)
    s = start_with_title(o.get('title') or '', t)
    return {'start': s, 'asap': 1 if not s and start_asap(t) else None,
            'months': months_with_title(o.get('title') or '', t), 'ctx': date_ctx(t, 1200)}


async def one(browser, sem, o, res, stats, t0):
    async with sem:
        if time.time() - t0 > BUDGET_S: return
        ctx = await browser.new_context(user_agent=UA, locale='fr-FR', viewport={'width': 1366, 'height': 900},
                                        extra_http_headers={'Accept-Language': 'fr-FR,fr;q=0.9,en;q=0.8'})
        await ctx.route('**/*', _route)
        out = {'v': V, 'at': NOW_ISO}
        try:
            got, src = None, None
            try:
                page = await asyncio.wait_for(read(ctx, o['url']), timeout=60)
            except Exception:
                page = None
            official = page[2] if page else []
            if page and page[0]:
                got, src = judge(o, page[0], page[1]), 'page'
            else:
                stats['blocked'] += 1
            s_ok, m_ok = written(o)
            need = lambda g: not g or (not (g['start'] or g['asap']) and not s_ok) or (not g['months'] and not m_ok)
            if need(got):  # lien officiel de l'employeur : page bloquée, ou date absente de la copie de l'agrégateur
                if o.get('direct'): official = [o['direct']] + official
                if o.get('via') == 'efc' and not official:
                    u = await asyncio.get_running_loop().run_in_executor(None, efc_link, o)
                    if u: official = [u]
                for u in list(dict.fromkeys(official))[:2]:
                    try:
                        p2 = await asyncio.wait_for(read(ctx, u), timeout=60)
                    except Exception:
                        p2 = None
                    if not p2 or not p2[0]: continue
                    g2 = judge(o, p2[0], p2[1])
                    if not got:
                        got, src = g2, 'officiel'
                    else:  # complète ce qui manque, sans écraser ce que la première page a donné
                        for k in ('start', 'asap', 'months'):
                            if not got[k] and g2[k]: got[k] = g2[k]; src = 'page+officiel'
                        got['ctx'] = (got['ctx'] + ' … ' + g2['ctx'])[:1200]
                    stats['official'] += 1
                    if not need(got): break
            if got:
                out['ok'] = 1; out['src'] = src; out['ctx'] = got['ctx']
                if got['start']: out['start'] = got['start']; stats['start'] += 1
                elif got['asap']: out['asap'] = 1; stats['start'] += 1
                if got['months']: out['months'] = got['months']; stats['months'] += 1
            stats['read'] += 1
        except Exception:
            stats['fail'] += 1
        finally:
            res[o['id']] = out
            await ctx.close()
            if stats['read'] % 40 == 0: save('dates_nav.json', res)


async def main_async():
    from playwright.async_api import async_playwright
    doc = load('offers.json') or {}
    offers = doc.get('offers') or {}
    if isinstance(offers, list): offers = {o['id']: o for o in offers if isinstance(o, dict) and o.get('id')}
    res = load('dates_nav.json', {}) or {}
    res = {k: v for k, v in res.items() if k in offers and offers[k].get('status') == 'open'}  # offres fermées : oubliées
    limit = (NOW.timestamp() - RETRY_DAYS * 86400)
    todo = []
    for oid, o in offers.items():
        if o.get('status') != 'open' or o.get('via') not in VIAS or not (o.get('url') or '').startswith('http'): continue
        s_ok, m_ok = written(o)
        if s_ok and m_ok: continue
        r = res.get(oid) or {}
        if r.get('v') == V:
            try: fresh = time.mktime(time.strptime(r.get('at', '')[:19], '%Y-%m-%dT%H:%M:%S')) > limit
            except Exception: fresh = False
            if fresh: continue
        todo.append(o)
    todo.sort(key=lambda o: o.get('first_seen') or '', reverse=True)  # les plus récentes d'abord
    print(f'nav : {len(todo)} fiches à lire', flush=True)
    stats = dict(read=0, blocked=0, official=0, start=0, months=0, fail=0)
    t0 = time.time()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=['--no-sandbox', '--disable-dev-shm-usage',
                                                                '--disable-blink-features=AutomationControlled'])
        sem = asyncio.Semaphore(CONC)
        await asyncio.gather(*(one(browser, sem, o, res, stats, t0) for o in todo))
        await browser.close()
    save('dates_nav.json', res)
    by = {}
    for o in todo:
        r = res.get(o['id']) or {}
        if r.get('at') != NOW_ISO: continue
        b = by.setdefault(o['via'], [0, 0, 0])
        b[0] += 1; b[1] += 1 if r.get('ok') else 0; b[2] += 1 if (r.get('start') or r.get('asap')) else 0
    print(f"nav : {stats['read']} fiches lues en {int(time.time() - t0)} s, bloquées {stats['blocked']}, "
          f"liens officiels suivis {stats['official']}, début trouvé {stats['start']}, durée trouvée {stats['months']}, pannes {stats['fail']}", flush=True)
    print('nav par site (lues / page lisible / début trouvé) : ' + ', '.join(f'{k} {v[0]}/{v[1]}/{v[2]}' for k, v in sorted(by.items())), flush=True)
    return 0 if stats['read'] or not todo else 1


if __name__ == '__main__':
    sys.exit(asyncio.run(main_async()))
