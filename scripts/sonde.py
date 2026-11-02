#!/usr/bin/env python3
"""Sonde : teste depuis GitHub Actions les sources candidates (statut, taille, debut de reponse). N'ecrit rien."""
import json, re, sys, os
import requests
UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36'
H = {'User-Agent': UA, 'Accept-Language': 'fr-FR,fr;q=0.9,en;q=0.8', 'Accept': '*/*'}
GS = {"operationName": "GetRoles", "variables": {"searchQueryInput": {"page": {"pageSize": 5, "pageNumber": 0}, "sort": {"sortStrategy": "RELEVANCE", "sortOrder": "DESC"}, "filters": [], "experiences": ["EARLY_CAREER", "PROFESSIONAL"], "searchTerm": "intern"}},
      "query": "query GetRoles($searchQueryInput: RoleSearchQueryInput!) { roleSearch(searchQueryInput: $searchQueryInput) { totalCount items { roleId jobTitle locations { city country } } } }"}
T = [
 ('trackr', 'GET', 'https://api.the-trackr.com/programmes?region=UK&industry=Finance&season=2027&type=summer-internships', None, {}),
 ('trackr2', 'GET', 'https://app.the-trackr.com/uk-finance/summer-internships', None, {}),
 ('efc', 'GET', 'https://job-search-api.efinancialcareers.com/v1/efc/jobs/search?q=intern&countryCode2=FR&page=1&pageSize=5&language=en', None, {}),
 ('efc2', 'GET', 'https://www.efinancialcareers.fr/jobs/in-france', None, {}),
 ('wttj', 'POST', 'https://csekhvms53-dsn.algolia.net/1/indexes/wttj_jobs_production_fr/query?x-algolia-application-id=CSEKHVMS53&x-algolia-api-key=4bd8f6215d0cc52b26430765769e65a0', {"query": "finance", "hitsPerPage": 3, "facetFilters": [["contract_type:internship"]]}, {'Referer': 'https://www.welcometothejungle.com/', 'Origin': 'https://www.welcometothejungle.com'}),
 ('apec', 'POST', 'https://www.apec.fr/cms/webservices/rechercheOffre', {"motsCles": "stage finance", "pagination": {"range": 5, "startIndex": 0}, "typeClient": "CADRE", "sorts": [{"type": "DATE", "direction": "DESCENDING"}], "activeFiltre": True}, {}),
 ('hiringcafe', 'POST', 'https://hiring.cafe/api/search-jobs', {"size": 3, "page": 0, "searchState": {"searchQuery": "investment banking intern"}}, {}),
 ('muse', 'GET', 'https://www.themuse.com/api/public/jobs?level=Internship&category=Accounting%20and%20Finance&page=0', None, {}),
 ('arbeitnow', 'GET', 'https://www.arbeitnow.com/api/job-board-api', None, {}),
 ('goldman', 'POST', 'https://api-higher.gs.com/gateway/api/v1/graphql', GS, {'Origin': 'https://higher.gs.com', 'Referer': 'https://higher.gs.com/'}),
 ('bofa', 'GET', 'https://careers.bankofamerica.com/services/jobssearchservlet?start=0&rows=5&search=getAllJobs&keywords=intern', None, {}),
 ('mckinsey', 'GET', 'https://mckapi.mckinsey.com/api/jobsearch?pageSize=5&start=1&q=intern', None, {}),
 ('bain', 'GET', 'https://www.bain.com/en/api/jobsearch/keyword/get?start=0&results=5&filters=&searchValue=intern', None, {}),
 ('janestreet', 'GET', 'https://www.janestreet.com/jobs/main.json', None, {}),
 ('bnp', 'GET', 'https://group.bnpparibas/emploi-carriere/toutes-offres-emploi/stage', None, {}),
 ('sg', 'GET', 'https://careers.societegenerale.com/rechercher?refinementList%5BjobType%5D%5B0%5D=INTERNSHIP', None, {}),
 ('natixis', 'GET', 'https://recrutement.groupebpce.com/nos-offres', None, {}),
 ('ca', 'GET', 'https://groupecreditagricole.jobs/fr/nos-offres/', None, {}),
 ('amundi', 'GET', 'https://jobs.amundi.com/offre-de-emploi/liste-offres.aspx', None, {}),
 ('citadel', 'GET', 'https://www.citadel.com/careers/open-opportunities/students/internships/', None, {}),
 ('twosigma', 'GET', 'https://careers.twosigma.com/careers/SearchJobs', None, {}),
 ('bcg', 'GET', 'https://careers.bcg.com/global/en/search-results?keywords=intern', None, {}),
 ('evercore', 'GET', 'https://www.evercore.com/careers/', None, {}),
 ('simplify', 'GET', 'https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json', None, {}),
]
for n, m, u, b, h in T:
    try:
        r = requests.request(m, u, json=b, headers={**H, **h}, timeout=25, allow_redirects=True)
        t = re.sub(r'\s+', ' ', r.text)
        print(f'{n}: {r.status_code} {len(r.text)} {r.url[:80]} :: {t[:220]}', flush=True)
    except Exception as e:
        print(f'{n}: ERR {str(e)[:120]}', flush=True)
TAL = 'morganstanley evercore rothschildandco rothschild jefferies nomura nomuracampus mizuho lazard ubs macquarie moelis pjt pjtpartners houlihanlokey perellaweinberg centerview williamblair baird rbc rbccm bmo citi hsbc barclays ardian blackrock blackstone kkr carlyle bnpparibas natixis sgcib santander bbva ing abnamro rabobank mufg smbc nikko standardchartered lloyds natwest deutschebank commerzbank bankofamerica wellsfargo stifel numis peelhunt investec schroders fidelity pimco mandg aviva lgim abrdn janus ninetyone bailliegifford ruffer man maninvestments marshallwace brevanhoward millennium citadel oaktree ares apollo'.split()
for c in TAL:
    u = f'https://{c}.tal.net/candidate/jobboard/vacancy/1/adv/'
    try:
        r = requests.get(u, headers=H, timeout=12, allow_redirects=True)
        links = len(re.findall(r'/opp/\d+', r.text))
        ti = re.search(r'<title>(.*?)</title>', r.text, re.S)
        print(f'tal {c}: {r.status_code} {len(r.text)} links={links} {r.url[:70]} :: {(ti.group(1).strip() if ti else "")[:60]}', flush=True)
    except Exception as e:
        print(f'tal {c}: ERR {type(e).__name__}', flush=True)
try:
    from jobspy import scrape_jobs
    for s in ('indeed', 'google', 'glassdoor'):
        try:
            d = scrape_jobs(site_name=[s], search_term='stage M&A', location='Paris', results_wanted=10, hours_old=500, country_indeed='france', google_search_term='stage M&A Paris since last month', verbose=0)
            print('jobspy', s, len(d), flush=True)
        except Exception as e:
            print('jobspy', s, 'ERR', str(e)[:160], flush=True)
except Exception as e:
    print('jobspy import', e)
print('FT secret :', bool(os.environ.get('FT_CLIENT_ID')))
