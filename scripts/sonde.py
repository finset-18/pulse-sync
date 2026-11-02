import json, re, collections, requests
UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36'
H = {'User-Agent': UA, 'Accept-Language': 'fr-FR,fr;q=0.9,en;q=0.8', 'Accept': '*/*'}
def p(*a): print(*a, flush=True)
def c(lst, k):
    return collections.Counter(json.dumps(x.get(k), ensure_ascii=False)[:50] for x in lst).most_common(12)
for q in ('region=UK&industry=Finance&season=2027&type=summer-internships', 'region=UK&industry=Finance&season=2027&type=off-cycle-internships', 'region=EU&industry=Finance&season=2027&type=summer-internships', 'region=UK&industry=Finance&season=2027&type=spring-weeks', 'region=US&industry=Finance&season=2027&type=summer-internships'):
    try:
        r = requests.get('https://api.the-trackr.com/programmes?' + q, headers=H, timeout=40)
        P = r.json().get('programmes') if r.status_code == 200 else []
        p('TRACKR', q, r.status_code, len(P or []))
        if P:
            p(' keys', list(P[0].keys()))
            for k in ('region', 'industry', 'season', 'type', 'category', 'locations', 'opportunityType'):
                if k in P[0]: p(' ', k, c(P, k))
            ex = [x for x in P if re.search('goldman|morgan|lazard', json.dumps(x), re.I)][:2] or P[:1]
            for e in ex: p(' EX', json.dumps(e, ensure_ascii=False)[:600])
    except Exception as e: p('TRACKR ERR', q, str(e)[:100])
try:
    r = requests.post('https://api-higher.gs.com/gateway/api/v1/graphql', headers={**H, 'Origin': 'https://higher.gs.com', 'Referer': 'https://higher.gs.com/'}, json={"operationName": "GetRoles", "variables": {"searchQueryInput": {"page": {"pageSize": 100, "pageNumber": 0}, "sort": {"sortStrategy": "RELEVANCE", "sortOrder": "DESC"}, "filters": [], "experiences": ["EARLY_CAREER"], "searchTerm": ""}}, "query": "query GetRoles($searchQueryInput: RoleSearchQueryInput!) { roleSearch(searchQueryInput: $searchQueryInput) { totalCount items { roleId jobTitle locations { city country } } } }"}, timeout=30)
    p('GS', r.status_code, r.text[:700])
except Exception as e: p('GS ERR', e)
r = requests.get('https://careers.bankofamerica.com/services/jobssearchservlet?start=0&rows=3&search=getAllJobs&keywords=intern', headers=H, timeout=30)
d = r.json(); p('BOFA', list(d.keys()), d.get('totalMatches'), json.dumps((d.get('jobsList') or [{}])[0])[:500])
r = requests.post('https://csekhvms53-dsn.algolia.net/1/indexes/wttj_jobs_production_fr/query?x-algolia-application-id=CSEKHVMS53&x-algolia-api-key=4bd8f6215d0cc52b26430765769e65a0', json={"query": "finance", "hitsPerPage": 1, "facetFilters": [["contract_type:internship"]]}, headers={**H, 'Referer': 'https://www.welcometothejungle.com/', 'Origin': 'https://www.welcometothejungle.com'}, timeout=30)
d = r.json(); p('WTTJ', d.get('nbHits'), d.get('nbPages'), list(d['hits'][0].keys())); h = d['hits'][0]; p(json.dumps({k: h.get(k) for k in ('name', 'slug', 'contract_type', 'published_at', 'offices', 'organization')}, ensure_ascii=False)[:600])
for n, u in (('sg', 'https://careers.societegenerale.com/rechercher?refinementList%5BjobType%5D%5B0%5D=INTERNSHIP'), ('ca', 'https://groupecreditagricole.jobs/fr/nos-offres/'), ('amundi', 'https://jobs.amundi.com/offre-de-emploi/liste-offres.aspx'), ('bcg', 'https://careers.bcg.com/global/en/search-results?keywords=intern'), ('evercore', 'https://www.evercore.com/careers/')):
    t = requests.get(u, headers=H, timeout=30).text
    ls = re.findall(r'href="([^"]*(?:job|offre|emploi|career)[^"]*)"', t)
    p(n, len(t), len(ls), ls[20:24], re.findall(r'(algolia[^"\s]{0,40}|eagerLoadRefineSearch|phApp|myworkdayjobs[^"\s]{0,40}|greenhouse[^"\s]{0,40}|tal\.net|oraclecloud[^"\s]{0,40})', t)[:5])
r = requests.get('https://www.efinancialcareers.fr', headers=H, timeout=20); p('efc home', r.status_code)
r = requests.get('https://job-search-api.efinancialcareers.com/v1/efc/jobs/search?q=stage&countryCode2=FR&page=30&pageSize=100&language=en', headers=H, timeout=25); d = r.json(); p('EFC', r.status_code, len(d.get('data') or []), str(d.get('meta'))[:200])
