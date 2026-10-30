#!/usr/bin/env python3
"""
briques communes du robot de veille (veille.py) et de la découverte (decouverte.py).

- filtres « stage » et enrichissement (contrat, domaine, durée, début, date limite, pays)
- plateformes de recrutement : détection + lecture directe (Workday, Greenhouse, Lever,
  SmartRecruiters, Ashby, Recruitee, Personio, Workable, Pinpoint, BambooHR, Breezy,
  Eightfold, Oracle Recruiting, Welcome to the Jungle)
- extraction générique : offres trouvées dans n'importe quelle réponse JSON d'un site
  (API cachée), dans une page HTML (liens d'offres, JSON-LD) ou dans un texte « Nous rejoindre »
"""
import json, re, os, html, copy, datetime as dt
from urllib.parse import urljoin, urlparse, urlencode, parse_qsl, urlunparse, quote
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.environ.get('DATA_DIR') or os.path.join(ROOT, 'data')
NOW = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
NOW_ISO = NOW.isoformat().replace('+00:00', 'Z')
UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36'
S = requests.Session()
S.headers.update({'User-Agent': UA, 'Accept-Language': 'fr-FR,fr;q=0.9,en;q=0.8'})
from urllib3.util.retry import Retry
# nouvel essai automatique quand une plateforme répond « trop de requêtes » ou plante un instant :
# sans ça, une lecture coupée en route faisait croire que des offres avaient disparu
_retry = Retry(total=3, connect=2, read=2, status=3, backoff_factor=1.5, status_forcelist=(429, 500, 502, 503, 504),
               allowed_methods=None, respect_retry_after_header=False, raise_on_status=False)
_ad = requests.adapters.HTTPAdapter(pool_connections=64, pool_maxsize=64, max_retries=_retry)
S.mount('https://', _ad); S.mount('http://', _ad)
TIMEOUT = 15


def load(name, default=None):
    try:
        with open(os.path.join(DATA, name), encoding='utf-8') as f: return json.load(f)
    except Exception:
        return default


def save(name, obj, pretty=False):
    with open(os.path.join(DATA, name), 'w', encoding='utf-8') as f:
        if pretty: json.dump(obj, f, ensure_ascii=False, indent=1)
        else: json.dump(obj, f, ensure_ascii=False, separators=(',', ':'))


def host_of(u):
    try: return urlparse(u or '').netloc.lower().split(':')[0].replace('www.', '')
    except Exception: return ''


def site_of(c):
    """Domaine principal de la structure (le site, pas la plateforme de recrutement)."""
    return c.get('site') or ('https://' + host_of(c.get('url')) + '/' if c.get('url') else '')


# ---------------------------------------------------------------- filtres
INTERN = re.compile(r"(\bintern\b|\binterns\b|internship|\bstage\b|\bstages\b|stagiaire|off[\s-]?cycle|summer (?:analyst|associate|intern|internship|programme|program)|"
                    r"praktik|werkstudent|\btrainee\b|apprenti|alternan|volontariat international|graduate|spring (?:week|insight)|"
                    r"insight (?:week|programme|program)|industrial placement|placement year|stagiair|tirocinio|pr[aá]cticas|becario|working student)", re.I)
INTERN_COMMIT = re.compile(r"intern|stage|stagiaire|apprenti|alternan|trainee|praktik|werkstudent|working student|\bvie\b|graduate", re.I)
EXCLUDE = re.compile(r"(senior|director|directeur|head of|vice president|\bvp\b|managing|manager(?! intern)|principal\b|partner\b|lead\b|chief|expert confirm)", re.I)

DOMAINS = [
    ('M&A / Corporate Finance', r"m&a|mergers|fusions|acquisitions|corporate finance|financial advisory|advisory|investment banking|banque d.affaires|\bibd\b|\becm\b|\bdcm\b|leveraged|lev ?fin|restructuring|capital markets origination"),
    ('Private Equity', r"private equity|capital[- ]investissement|\blbo\b|buy[- ]?out|growth equity|investissement non cot|fund investment|co-?invest"),
    ('Venture Capital', r"venture|\bvc\b|capital[- ]risque|seed|early[- ]stage"),
    ('Dette privée / Crédit', r"private debt|dette priv|private credit|direct lending|credit analyst|analyste cr[ée]dit|mezzanine|structured finance|financements structur|leveraged loan"),
    ('Infrastructure', r"infrastructure|infra\b|energy transition|renewable|project finance|financement de projet"),
    ('Real Estate', r"real estate|immobilier|property|reit|asset management immobilier"),
    ('Asset Management', r"asset management|gestion d.actifs|portfolio manag|fund manag|gestion de portefeuille|multi[- ]asset|fixed income|equity fund|investment specialist|\bam\b|ucits"),
    ('Wealth / Banque privée', r"wealth|banque priv|private bank|gestion de patrimoine|family office|cgp\b|ing[ée]nierie patrimoniale"),
    ('Marchés / Sales & Trading', r"sales|trading|trader|markets|march[ée]s|structur(ing|eur)|derivatives|d[ée]riv[ée]s|fx\b|rates|commodit|global markets"),
    ('Quant', r"quant|quantitative|data scien|machine learning|algorithm|developer|d[ée]veloppeur|software|engineer|ing[ée]nieur"),
    ('Equity Research', r"research|recherche actions|equity analyst|analyste financier|sell[- ]side"),
    ('Risque / Conformité', r"risk|risque|compliance|conformit|kyc|aml|lcb|contr[ôo]le permanent|inspection|audit interne|regulat"),
    ('Audit / Transaction Services', r"transaction services|due diligence|valuation|[ée]valuation|audit|expertise comptable|financial due|\bts\b|deal advisory|forensic"),
    ('Corporate Banking / Financement', r"corporate banking|coverage|financement|lending|credit|cr[ée]dit|trade finance|cash management|transaction banking"),
    ("Finance d'entreprise / Trésorerie", r"tr[ée]sorerie|treasury|contr[ôo]le de gestion|controlling|fp&a|comptab|accounting|financial analyst|analyste financier|corporate development|investor relations|relations investisseurs|consolidation|direction financi"),
    ('Conseil / Stratégie', r"consult|conseil|strat[ée]gie|strategy"),
]
DOMAINS = [(n, re.compile(p, re.I)) for n, p in DOMAINS]
FAM_DOMAIN = {'bb': 'M&A / Corporate Finance', 'eb': 'M&A / Corporate Finance', 'bfr': 'M&A / Corporate Finance', 'ts': 'Audit / Transaction Services',
              'pelarge': 'Private Equity', 'pemid': 'Private Equity', 'pesmall': 'Private Equity', 'debt': 'Dette privée / Crédit', 'vc': 'Venture Capital',
              'fo': 'Private Equity', 'am': 'Asset Management', 'trading': 'Marchés / Sales & Trading', 'wealth': 'Wealth / Banque privée',
              're': 'Real Estate', 'corp': "Finance d'entreprise / Trésorerie", 'consulting': 'Conseil / Stratégie', 'fintech': "Finance d'entreprise / Trésorerie",
              'rating': 'Risque / Conformité', 'fb': 'Corporate Banking / Financement'}
MONTHS = {'janvier': 1, 'january': 1, 'jan': 1, 'janv': 1, 'février': 2, 'fevrier': 2, 'february': 2, 'feb': 2, 'févr': 2, 'fevr': 2, 'mars': 3, 'march': 3, 'mar': 3,
          'avril': 4, 'april': 4, 'apr': 4, 'avr': 4, 'mai': 5, 'may': 5, 'juin': 6, 'june': 6, 'jun': 6, 'juillet': 7, 'july': 7, 'jul': 7, 'juil': 7,
          'août': 8, 'aout': 8, 'august': 8, 'aug': 8, 'septembre': 9, 'september': 9, 'sept': 9, 'sep': 9, 'octobre': 10, 'october': 10, 'oct': 10,
          'novembre': 11, 'november': 11, 'nov': 11, 'décembre': 12, 'decembre': 12, 'december': 12, 'dec': 12, 'déc': 12}
MONTH_RE = '|'.join(sorted(MONTHS, key=len, reverse=True))
NUMW = {'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10, 'twelve': 12,
        'un': 1, 'deux': 2, 'trois': 3, 'quatre': 4, 'cinq': 5, 'sept': 7, 'huit': 8, 'neuf': 9, 'dix': 10, 'douze': 12}
COUNTRIES = [('France', r"france|paris|lyon|lille|marseille|bordeaux|nantes|toulouse|rennes|strasbourg|nice|la d[ée]fense|neuilly|levallois|boulogne|courbevoie|puteaux|nanterre"),
             ('Royaume-Uni', r"united kingdom|\buk\b|london|londres|manchester|edinburgh|[ée]dimbourg|birmingham|leeds|glasgow|bristol|england"),
             ('Luxembourg', r"luxembourg"), ('Suisse', r"switzerland|suisse|zurich|z[üu]rich|geneva|gen[èe]ve|lausanne|basel|b[âa]le|lugano|zug|zoug"),
             ('Belgique', r"belgium|belgique|brussels|bruxelles|antwerp|anvers|gent|gand|li[èe]ge"), ('Allemagne', r"germany|allemagne|frankfurt|francfort|munich|m[üu]nchen|berlin|hamburg|hambourg|d[üu]sseldorf|cologne|stuttgart"),
             ('Pays-Bas', r"netherlands|pays-bas|amsterdam|rotterdam|utrecht|the hague|la haye"), ('Espagne', r"spain|espagne|madrid|barcelona|barcelone"),
             ('Italie', r"italy|italie|milan|milano|rome|roma"), ('États-Unis', r"united states|\busa\b|new york|boston|chicago|san francisco|houston|\bny\b"),
             ('Monaco', r"monaco"), ('Irlande', r"ireland|irlande|dublin"), ('Hong Kong', r"hong kong"), ('Singapour', r"singapore|singapour"), ('Dubaï', r"dubai|duba[ïi]")]
COUNTRIES = [(n, re.compile(p, re.I)) for n, p in COUNTRIES]


def clean(s):
    s = html.unescape(re.sub(r'<[^>]+>', ' ', str(s or '')))
    return re.sub(r'\s+', ' ', s).strip()


GENERIC = {'internship', 'internships', 'intern', 'interns', 'stage', 'stages', 'graduate', 'graduates', 'programme', 'programmes', 'program', 'programs',
           'student', 'students', 'trainee', 'trainees', 'scheme', 'schemes', 'opportunities', 'our', 'nos', 'early', 'careers', 'career', 'summer', 'spring',
           'insight', 'week', 'weeks', '&', 'and', 'et', 'les', 'offres', 'de', 'des', 'for', 'roles', 'jobs', 'alternance', 'alternances', 'vie', 'off-cycle', 'the', 'and/or'}
NAVLIKE = re.compile(r"^(search|find|explore|discover|view|see|browse|learn|apply now|our |nos |d[ée]couvr|rejoign|voir|toutes? les|all |more |en savoir|read)", re.I)
FIN_TITLE = re.compile(r"financ|m&a|merger|fusion|acquisition|treasury|tr[ée]sor|contr[ôo]le de gestion|controll|audit|comptab|account|fp&a|invest|corporate dev|strat[ée]g|strategy|business analyst|risk|risque|tax|fiscal|credit|cr[ée]dit|pricing|valuation|[ée]valuation|economi|[ée]conom|banking|bank|capital|consolidation|reporting financ|private equity|venture|asset|wealth|patrimoin|fund|fonds|trading|market|march[ée]s|actuar|quant|deal|transaction", re.I)


VIE_RE = re.compile(r"\bV\.?\s?I\.?\s?E\b")   # en majuscules seulement (« vie privée » n'est pas un VIE)
# « stage » au sens d'étape d'investissement, ou « graduate » au sens de diplômé : pas des stages
NOT_INTERN = re.compile(r"\b(?:early|late|later|growth|seed|multi|all|every|any|different|various|across|venture|investment|development|clinical|final|next|first|second)[- ](?:stages?)\b|"
                        r"\bstages? of\b|graduate[sd]? (?:of|from|de|du)\b|\bgraduated\b|undergraduate degree|graduate degree", re.I)


def is_intern(title):
    t = NOT_INTERN.sub(' ', title or '')
    if not (INTERN.search(t) or VIE_RE.search(t)) or EXCLUDE.search(re.sub(INTERN, '', t)) or NAVLIKE.search(t.strip()): return False
    words = [w.lower() for w in re.findall(r"[\w&/'-]+", t)]
    if len(words) <= 4 and all(w in GENERIC or w.isdigit() for w in words): return False
    return True


def keep_offer(x):
    """Une offre brute est-elle un stage (titre explicite, ou type de contrat donné par la plateforme) ?"""
    t = clean(x.get('title'))
    if not t or not x.get('url'): return False
    if is_intern(t): return True
    com = x.get('commit') or ''
    return bool(com and INTERN_COMMIT.search(com) and not EXCLUDE.search(t) and not NAVLIKE.search(t))


def contract_of(t):
    t = (t or '').lower()
    if re.search(r'alternan|apprenti|work[- ]study|werkstudent|working student', t): return 'Alternance'
    if re.search(r'\bv\.?\s?i\.?\s?e\b|volontariat international', t): return 'VIE'
    if re.search(r'summer', t): return 'Summer'
    if re.search(r'off[\s-]?cycle', t): return 'Off-cycle'
    if re.search(r'spring|insight', t): return 'Spring / Insight'
    if re.search(r'graduate', t) and not re.search(r'intern|stage', t): return 'Graduate'
    return 'Stage'


def domain_of(title, desc, fam):
    for n, rx in DOMAINS:
        if rx.search(title or ''): return n
    head = (desc or '')[:1500]
    for n, rx in DOMAINS:
        if rx.search(head): return n
    return FAM_DOMAIN.get(fam, 'Autre')


WEEKS_SKIP = re.compile(r"^\s*(?:of\s+)?(?:paid\s+)?(?:leave|vacation|holiday|annual|notice|cong[ée]s|de cong|de vacances|pr[ée]avis)", re.I)

def months_of(text):
    t = (text or '').lower()
    t = re.sub(r'\((\d{1,2})\)', r'\1', t)  # « six (6) mois »
    m = re.search(r'(\d{1,2})\s*(?:-|à|a|to|/)\s*(\d{1,2})\s*(?:mois|months?)', t)
    if m: return int(m.group(2))
    m = re.search(r'(\d{1,2})\s*[- ]?(?:mois|months?|month)\b', t)
    if m and 1 <= int(m.group(1)) <= 24: return int(m.group(1))
    m = re.search(r'\b(' + '|'.join(NUMW) + r')[\s-](?:mois|months?|month)\b', t)
    if m: return NUMW[m.group(1)]
    # durée en semaines (summer, spring week…) : 10 semaines ≈ 2 mois
    for m in re.finditer(r'\b(\d{1,2}|' + '|'.join(NUMW) + r')[\s-](?:weeks?|semaines?)\b', t):
        n = int(m.group(1)) if m.group(1).isdigit() else NUMW[m.group(1)]
        if 3 <= n <= 30 and not WEEKS_SKIP.search(t[m.end():m.end() + 25]): return max(1, round(n / 4.33))
    return None


def ymd(y, mo, d=1):
    try: return dt.date(int(y), int(mo), int(d)).isoformat()
    except Exception: return None


def find_date(t):
    m = re.search(r'(\d{1,2})[/.-](\d{1,2})[/.-](20\d{2})', t)
    if m: return ymd(m.group(3), m.group(2), m.group(1))
    m = re.search(r'(20\d{2})-(\d{2})-(\d{2})', t)
    if m: return ymd(m.group(1), m.group(2), m.group(3))
    m = re.search(r'(\d{1,2})(?:er|st|nd|rd|th)?\s+(' + MONTH_RE + r')\.?\s+(20\d{2})', t)
    if m: return ymd(m.group(3), MONTHS[m.group(2)], m.group(1))
    m = re.search(r'(' + MONTH_RE + r')\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(20\d{2})', t)
    if m: return ymd(m.group(3), MONTHS[m.group(1)], m.group(2))
    return None


def _start_of(text, loose=True):
    t = (text or '').lower()
    kw = r'(?:d[ée]but|start(?:ing)?(?: date)?|starting|commen\w+|à partir d[eu]|from|beginning|disponibilit[ée])'
    m = re.search(kw + r'[^.\n]{0,40}?(' + MONTH_RE + r')\.?\s*(20\d{2})', t)
    if m: return f"{m.group(2)}-{MONTHS[m.group(1)]:02d}"
    m = re.search(kw + r'[^.\n]{0,40}?(\d{1,2})/(20\d{2})', t)
    if m and 1 <= int(m.group(1)) <= 12: return f"{m.group(2)}-{int(m.group(1)):02d}"
    m = re.search(kw + r'[^.\n]{0,30}?(\d{1,2})[/.](\d{1,2})[/.](20\d{2})', t)
    if m and 1 <= int(m.group(2)) <= 12: return f"{m.group(3)}-{int(m.group(2)):02d}"
    if not loose: return None
    m = re.search(r'\b(' + MONTH_RE + r')\.?\s+(20\d{2})', t)
    if m: return f"{m.group(2)}-{MONTHS[m.group(1)]:02d}"
    return None


def start_of(text, loose=True):
    r = _start_of(text, loose)
    return r if r and r >= NOW.strftime('%Y-%m') else None


def deadline_of(text):
    t = (text or '').lower()
    for m in re.finditer(r"date limite|deadline|closing date|apply (?:by|before)|candidatures? (?:jusqu|avant)|applications? (?:close|closing|must be received|will close)|cl[ôo]ture des candidatures|postuler avant|valable jusqu", t):
        d = find_date(t[m.end():m.end() + 70])
        if d and d >= NOW.date().isoformat(): return d
    return None


def country_of(loc):
    for n, rx in COUNTRIES:
        if rx.search(loc or ''): return n
    return ''


def posted_from_text(s):
    s = (s or '').lower()
    if 'today' in s or "aujourd" in s: return NOW_ISO
    if 'yesterday' in s or 'hier' in s: return (NOW - dt.timedelta(days=1)).isoformat().replace('+00:00', 'Z')
    m = re.search(r'(\d+)\+?\s*(day|jour)', s)
    if m: return (NOW - dt.timedelta(days=int(m.group(1)))).isoformat().replace('+00:00', 'Z')
    return None


def iso(v):
    if v in (None, ''): return None
    try:
        if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit()):
            v = float(v); v = v / 1000 if v > 1e11 else v
            if v < 9e8: return None
            return dt.datetime.fromtimestamp(v, dt.timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
        s = str(v).strip().replace(' ', 'T')
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', s): s += 'T00:00:00'
        d = dt.datetime.fromisoformat(s.replace('Z', '+00:00'))
        if d.tzinfo is None: d = d.replace(tzinfo=dt.timezone.utc)
        if d > NOW + dt.timedelta(days=2): return None  # une date de publication ne peut pas être dans le futur
        return d.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
    except Exception:
        return None


def fix_enc(r):
    ct = r.headers.get('content-type', '').lower()
    if 'charset' not in ct and (r.encoding or '').lower() in ('iso-8859-1', 'latin-1', ''):
        m = re.search(rb'<meta[^>]+charset=["\']?([\w-]+)', r.content[:4000], re.I)
        r.encoding = m.group(1).decode() if m else 'utf-8'
    return r


def get(url, **kw):
    kw.setdefault('timeout', TIMEOUT)
    r = S.get(url, **kw); r.raise_for_status(); return fix_enc(r)


def post(url, js, **kw):
    kw.setdefault('timeout', TIMEOUT)
    r = S.post(url, json=js, **kw); r.raise_for_status(); return r


# ---------------------------------------------------------------- plateformes
ATS_RE = {
    'workday': re.compile(r'https?://([\w-]+)\.(wd\d+)\.(myworkdayjobs|myworkdaysite)\.com/((?:[a-z]{2}-[A-Z]{2}/)?(?:recruiting/[\w-]+/)?[\w-]+)', re.I),
    'workdaysite': re.compile(r'https?://(wd\d+)\.myworkdaysite\.com/(?:[a-z]{2}-[A-Z]{2}/)?recruiting/([\w-]+)/([\w-]+)', re.I),
    'greenhouse': re.compile(r'(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board(?:/js)?\?for=)?([\w-]+)', re.I),
    'lever': re.compile(r'jobs\.(eu\.)?lever\.co/([\w.-]+)', re.I),
    'smartrecruiters': re.compile(r'(?:careers|jobs)\.smartrecruiters\.com/([\w-]+)', re.I),
    'ashby': re.compile(r'jobs\.ashbyhq\.com/([\w.%-]+)', re.I),
    'recruitee': re.compile(r'([\w-]+)\.recruitee\.com', re.I),
    'personio': re.compile(r'([\w-]+)\.jobs\.personio\.(?:de|com)', re.I),
    'workable': re.compile(r'apply\.workable\.com/([\w-]+)', re.I),
    'pinpoint': re.compile(r'([\w-]+)\.pinpointhq\.com', re.I),
    'bamboohr': re.compile(r'([\w-]+)\.bamboohr\.com', re.I),
    'breezy': re.compile(r'([\w-]+)\.breezy\.hr', re.I),
    'eightfold': re.compile(r'([\w-]+)\.eightfold\.ai', re.I),
    'oracle': re.compile(r'https?://([\w-]+\.fa(?:\.[\w-]+)?\.oraclecloud\.com)/hcmUI/CandidateExperience/[a-z]{2}(?:-[A-Z]{2})?/sites/([\w-]+)', re.I),
    'oracleapi': re.compile(r'https?://([\w-]+\.fa(?:\.[\w-]+)?\.oraclecloud\.com)/hcmRestApi/resources/[\w.-]+/recruitingCEJobRequisitions[^"\'\s<>]*?siteNumber=([\w-]+)', re.I),
    'wttj': re.compile(r'welcometothejungle\.com/(?:fr|en|es|cs|sk)/companies/([\w-]+)', re.I),
}
# plateformes reconnues mais lues comme des pages (liste d'offres en HTML ou via l'API captée par la découverte)
ATS_HOSTS = re.compile(r'(teamtailor\.com|icims\.com|jobvite\.com|avature\.net|taleo\.net|successfactors\.(?:com|eu)|sapsf\.(?:com|eu)|talent-soft\.com|talentsoft\.com|'
                       r'digitalrecruiters\.com|csod\.com|cornerstoneondemand|flatchr\.io|jobaffinity\.fr|taleez\.com|join\.com|softgarden\.io|hrmos|'
                       r'welcomekit\.co|jobteaser\.com|myworkdayjobs\.com|greenhouse\.io|lever\.co|smartrecruiters\.com|recruitee\.com|personio\.|'
                       r'oraclecloud\.com|eightfold\.ai|phenompeople\.com|radancy|tbcdn|ultipro\.com|ukg\.|hirebridge|bullhorn|jobs\.net|beetween\.com|'
                       r'werecruit\.io|kenjo\.io|factorial|zohorecruit|manatal|ats\.rippling|dover\.com|homerun\.co|jobylon|varbi|emply|hr-manager\.net)', re.I)
BAD_SLUG = {'www', 'app', 'api', 'embed', 'js', 'static', 'assets', 'cdn', 'login', 'careers', 'jobs', 'cookie-policy-scripts', 'widget', 'widgets', 'boards'}


def detect_ats(text):
    found = {}
    for k, rx in ATS_RE.items():
        for m in rx.finditer(text or ''):
            if k == 'workdaysite':
                found.setdefault('workday', (f'{m.group(1)}.myworkdaysite.com', m.group(2), m.group(3))); break
            if k == 'workday':
                host = f'{m.group(1)}.{m.group(2)}.{m.group(3)}.com'
                segs = [s for s in m.group(4).split('/') if s and not re.fullmatch(r'[a-z]{2}-[A-Z]{2}', s)]
                if segs and segs[0] == 'recruiting' and len(segs) >= 3: tenant, site = segs[1], segs[2]
                else: tenant, site = m.group(1), (segs[0] if segs else '')
                if site and site.lower() not in ('login', 'userhome', 'job', 'details', 'wday'): found.setdefault(k, (host, tenant, site)); break
            elif k in ('oracle', 'oracleapi'):
                found.setdefault('oracle', (m.group(1).lower(), m.group(2))); break
            elif k == 'lever':
                if m.group(2).lower() not in BAD_SLUG: found.setdefault(k, (m.group(2), bool(m.group(1)))); break
            else:
                slug = m.group(1)
                if slug.lower() not in BAD_SLUG: found.setdefault(k, slug); break
    return found


def _norm(x): return re.sub(r'[^a-z0-9]', '', (x or '').lower())

STOP_TOK = {'capital', 'partners', 'partner', 'group', 'groupe', 'finance', 'financial', 'invest', 'investment', 'investments', 'ventures', 'venture', 'asset',
            'management', 'the', 'and', 'des', 'bank', 'banque', 'conseil', 'advisory', 'advisors', 'associes', 'associés', 'corporate', 'private', 'equity',
            'gestion', 'company', 'holding', 'international', 'global', 'france', 'paris', 'london', 'securities', 'fund', 'funds', 'family', 'office'}


def name_tokens(c):
    host = host_of(site_of(c) or c.get('url'))
    toks = {_norm(host.split('.')[0])} | {_norm(w) for w in re.split(r"[\s&()/,.'’-]+", c.get('name', '')) if len(_norm(w)) >= 3}
    full = _norm(c.get('name', ''))
    if len(full) >= 4: toks.add(full)
    return {t for t in toks if len(t) >= 3 and t not in STOP_TOK}


def relevant(c, slug):
    """Le compte de la plateforme appartient-il bien à la structure (et pas à une participation d'un fonds) ?"""
    s = _norm(slug)
    if not s: return False
    return any(t in s or s in t for t in name_tokens(c))


def ats_ok(c, k, v):
    if k in ('workday', 'oracle'): return True
    return relevant(c, v[0] if k == 'lever' else v)


class Partial(list):
    """Offres d'une lecture incomplète (une requête a échoué en route) : les absentes ne sont pas comptées comme fermées."""
    partial = True

def j_workday(c, a):
    host, tenant, site = a
    base = f'https://{host}/wday/cxs/{tenant}/{site}'
    out, seen, okq, errs = [], set(), 0, 0
    for q in ['intern', 'stage', 'summer', 'off-cycle', 'trainee', 'praktikum', 'alternance', 'graduate', 'VIE']:
        for off in range(0, 200, 20):
            try: js = post(base + '/jobs', {'appliedFacets': {}, 'limit': 20, 'offset': off, 'searchText': q}).json(); okq += 1
            except Exception: errs += 1; break
            posts = js.get('jobPostings') or []
            for p in posts:
                path = p.get('externalPath') or ''
                if not path or path in seen: continue
                seen.add(path)
                out.append({'title': p.get('title'), 'url': f'https://{host}/{site}{path}', 'location': p.get('locationsText') or '',
                            'posted': posted_from_text(p.get('postedOn')), 'ext': path, 'detail': ('workday', base + path)})
            if len(posts) < 20: break
    if not okq: raise RuntimeError('workday injoignable')
    return Partial(out) if errs else out


def j_greenhouse(c, slug):
    try: js = get(f'https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true').json()
    except Exception: js = get(f'https://boards-api.eu.greenhouse.io/v1/boards/{slug}/jobs?content=true').json()  # comptes hébergés en Europe
    return [{'title': j.get('title'), 'url': j.get('absolute_url'), 'location': (j.get('location') or {}).get('name', ''),
             'posted': iso(j.get('first_published') or j.get('updated_at')), 'ext': str(j.get('id')), 'desc': clean(html.unescape(j.get('content') or ''))} for j in js.get('jobs', [])]


def j_lever(c, a):
    slug, eu = a
    js = get(f'https://api.{"eu." if eu else ""}lever.co/v0/postings/{slug}?mode=json').json()
    return [{'title': j.get('text'), 'url': j.get('hostedUrl'), 'location': (j.get('categories') or {}).get('location', ''),
             'posted': iso(j.get('createdAt')), 'ext': j.get('id'), 'desc': clean(j.get('descriptionPlain') or '') + ' ' + clean(' '.join(l.get('content', '') for l in j.get('lists', []))),
             'commit': (j.get('categories') or {}).get('commitment', '')} for j in js]


def j_smartrecruiters(c, cid):
    out = []
    for off in range(0, 1000, 100):
        js = get(f'https://api.smartrecruiters.com/v1/companies/{cid}/postings?limit=100&offset={off}').json()
        for j in js.get('content', []):
            loc = j.get('location') or {}
            out.append({'title': j.get('name'), 'url': f"https://jobs.smartrecruiters.com/{cid}/{j.get('id')}", 'location': ', '.join(x for x in [loc.get('city'), loc.get('country', '').upper()] if x),
                        'posted': iso(j.get('releasedDate')), 'ext': j.get('id'), 'commit': (j.get('typeOfEmployment') or {}).get('label', '') + ' ' + (j.get('experienceLevel') or {}).get('label', ''),
                        'detail': ('sr', f"https://api.smartrecruiters.com/v1/companies/{cid}/postings/{j.get('id')}")})
        if len(js.get('content', [])) < 100: break
    return out


def j_ashby(c, slug):
    js = get(f'https://api.ashbyhq.com/posting-api/job-board/{slug}').json()
    return [{'title': j.get('title'), 'url': j.get('jobUrl'), 'location': j.get('location') or '', 'posted': iso(j.get('publishedAt')),
             'ext': j.get('id'), 'desc': clean(j.get('descriptionPlain') or j.get('descriptionHtml') or ''), 'commit': j.get('employmentType', '')} for j in js.get('jobs', [])]


def j_recruitee(c, slug):
    js = get(f'https://{slug}.recruitee.com/api/offers/').json()
    return [{'title': j.get('title'), 'url': j.get('careers_url'), 'location': ', '.join(x for x in [j.get('city'), j.get('country')] if x) or j.get('location', ''),
             'posted': iso(j.get('published_at') or j.get('created_at')), 'ext': str(j.get('id')), 'desc': clean((j.get('description') or '') + ' ' + (j.get('requirements') or '')),
             'commit': j.get('employment_type_code') or '', 'deadline': (j.get('close_at') or '')[:10] or None} for j in js.get('offers', [])]


def j_personio(c, a):
    slug = a
    x = get(f'https://{slug}.jobs.personio.de/xml?language=fr').text
    out = []
    for p in re.findall(r'<position>(.*?)</position>', x, re.S):
        g = lambda tag: clean((re.search(rf'<{tag}>(.*?)</{tag}>', p, re.S) or [None, ''])[1].replace('<![CDATA[', '').replace(']]>', ''))
        pid = g('id')
        out.append({'title': g('name'), 'url': f'https://{slug}.jobs.personio.de/job/{pid}', 'location': g('office'), 'posted': iso(g('createdAt')), 'ext': pid,
                    'desc': clean(' '.join(re.findall(r'<value>(.*?)</value>', p, re.S)).replace('<![CDATA[', '').replace(']]>', '')), 'commit': g('employmentType') + ' ' + g('schedule')})
    return out


def j_workable(c, slug):
    js = post(f'https://apply.workable.com/api/v3/accounts/{slug}/jobs', {'query': '', 'location': [], 'department': [], 'worktype': [], 'remote': []}).json()
    return [{'title': j.get('title'), 'url': f"https://apply.workable.com/{slug}/j/{j.get('shortcode')}/", 'location': ', '.join(x for x in [(j.get('location') or {}).get('city'), (j.get('location') or {}).get('country')] if x),
             'posted': iso(j.get('published')), 'ext': j.get('shortcode'), 'commit': j.get('type') or ''} for j in js.get('results', [])]


def j_pinpoint(c, slug):
    js = get(f'https://{slug}.pinpointhq.com/postings.json').json()
    return [{'title': j.get('title'), 'url': j.get('url'), 'location': (j.get('location') or {}).get('name', ''), 'posted': None, 'ext': str(j.get('id')),
             'desc': clean(j.get('description') or ''), 'commit': j.get('employment_type') or '', 'deadline': (j.get('deadline_at') or '')[:10] or None} for j in js.get('data', [])]


def j_bamboohr(c, slug):
    js = get(f'https://{slug}.bamboohr.com/careers/list').json()
    return [{'title': j.get('jobOpeningName'), 'url': f"https://{slug}.bamboohr.com/careers/{j.get('id')}", 'location': ', '.join(x for x in [(j.get('location') or {}).get('city'), (j.get('location') or {}).get('state')] if x),
             'posted': None, 'ext': str(j.get('id')), 'commit': j.get('employmentStatusLabel') or ''} for j in js.get('result', [])]


def j_breezy(c, slug):
    js = get(f'https://{slug}.breezy.hr/json').json()
    return [{'title': j.get('name'), 'url': j.get('url'), 'location': (j.get('location') or {}).get('name', ''), 'posted': iso(j.get('published_date')), 'ext': j.get('id'),
             'commit': (j.get('type') or {}).get('name', '') if isinstance(j.get('type'), dict) else str(j.get('type') or '')} for j in js]


def j_eightfold(c, slug):
    dom = host_of(site_of(c))
    out, okq, errs = [], 0, 0
    for q in ['intern', 'stage', 'summer', 'analyst program']:
        try: js = get(f'https://{slug}.eightfold.ai/api/apply/v2/jobs', params={'domain': dom, 'start': 0, 'num': 100, 'query': q}).json(); okq += 1
        except Exception: errs += 1; continue
        for j in js.get('positions', []):
            out.append({'title': j.get('name'), 'url': j.get('canonicalPositionUrl') or f"https://{slug}.eightfold.ai/careers?pid={j.get('id')}", 'location': j.get('location', ''),
                        'posted': iso(j.get('t_create')), 'ext': str(j.get('id'))})
    if not okq: raise RuntimeError('eightfold injoignable')
    return Partial(out) if errs else out


KW_SEARCH = ['intern', 'internship', 'stage', 'stagiaire', 'summer', 'off-cycle', 'graduate', 'trainee', 'alternance', 'praktikum', 'VIE']
API_KW = ['intern', 'stage', 'stagiaire', 'summer', 'graduate', 'alternance', 'off-cycle', 'trainee']


def j_oracle(c, a):
    host, site = a
    out, seen, okq, errs = [], set(), 0, 0
    for q in KW_SEARCH:
        u = (f'https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true&expand=requisitionList.secondaryLocations'
             f'&finder=findReqs;siteNumber={site},facetsList=LOCATIONS,limit=200,keyword=%22{quote(q)}%22,sortBy=POSTING_DATES_DESC')
        try: js = get(u, headers={'Accept': 'application/json'}).json(); okq += 1
        except Exception: errs += 1; continue
        for it in js.get('items', []) or []:
            for r in it.get('requisitionList', []) or []:
                rid = str(r.get('Id') or '')
                if not rid or rid in seen: continue
                seen.add(rid)
                out.append({'title': r.get('Title'), 'url': f'https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{rid}', 'location': r.get('PrimaryLocation') or '',
                            'posted': iso(r.get('PostedDate')), 'ext': rid, 'desc': clean(r.get('ShortDescriptionStr') or ''), 'commit': r.get('JobSchedule') or '',
                        'detail': ('oracle', f'{host}|{site}|{rid}')})
    if not okq: raise RuntimeError('oracle injoignable')
    return Partial(out) if errs else out


def j_wttj(c, slug):
    out, okq = [], 0
    for page in range(1, 8):
        r = S.get(f'https://api.welcometothejungle.com/api/v1/organizations/{slug}/jobs', params={'per_page': 100, 'page': page}, timeout=TIMEOUT,
                  headers={'Accept': 'application/json', 'Referer': 'https://www.welcometothejungle.com/'})
        r.raise_for_status(); js = r.json(); okq += 1
        jobs = js.get('jobs') if isinstance(js, dict) else js
        jobs = jobs or []
        for j in jobs:
            o = j.get('office') or {}
            ct = j.get('contract_type') or ''
            if isinstance(ct, dict): ct = ct.get('en') or ct.get('fr') or ''
            out.append({'title': j.get('name') or j.get('title'), 'url': f"https://www.welcometothejungle.com/fr/companies/{slug}/jobs/{j.get('slug')}",
                        'location': ', '.join(x for x in [o.get('city'), o.get('country') if isinstance(o.get('country'), str) else ''] if x),
                        'posted': iso(j.get('published_at')), 'ext': j.get('slug') or str(j.get('id')), 'commit': str(ct),
                        'desc': clean(j.get('description') or j.get('profile') or '')[:3000]})
        if len(jobs) < 100: break
    if not okq: raise RuntimeError('wttj injoignable')
    return out


FETCHERS = {'workday': j_workday, 'greenhouse': j_greenhouse, 'lever': j_lever, 'smartrecruiters': j_smartrecruiters, 'ashby': j_ashby, 'recruitee': j_recruitee,
            'personio': j_personio, 'workable': j_workable, 'pinpoint': j_pinpoint, 'bamboohr': j_bamboohr, 'breezy': j_breezy, 'eightfold': j_eightfold,
            'oracle': j_oracle, 'wttj': j_wttj}


def wttj_org(slug):
    """Fiche d'une entreprise sur Welcome to the Jungle (None si inconnue)."""
    try:
        r = S.get(f'https://api.welcometothejungle.com/api/v1/organizations/{slug}', timeout=TIMEOUT,
                  headers={'Accept': 'application/json', 'Referer': 'https://www.welcometothejungle.com/'})
        if r.status_code != 200: return None
        js = r.json()
        return js.get('organization') or js
    except Exception:
        return None


def slugify(s):
    s = html.unescape(s or '').lower()
    for a, b in (('àâä', 'a'), ('éèêë', 'e'), ('îï', 'i'), ('ôö', 'o'), ('ùûü', 'u'), ('ç', 'c')):
        for ch in a: s = s.replace(ch, b)
    s = re.sub(r'\([^)]*\)', ' ', s).replace('&', ' ')
    return re.sub(r'[^a-z0-9]+', '-', s).strip('-')


# ---------------------------------------------------------------- pages carrières
JOBLINK = re.compile(r'(/jobs?/|/job[-_]|/offres?[/-]|/offre-|/emplois?/|/postes?/|/vacanc|/vacatures?/|/positions?/|/opportunit|/careers?/[^/]+/[^/]+|/carrieres?/[^/]+/[^/]+|'
                     r'/stages?/|/annonce|/stellen|/karriere/[^/]+|/apply/|/requisition|jobid=|job_id=|jobreq|/o/|/j/|/jobdetail|/job-detail|/details?/|reqid|/posting|/vacature|\.pdf$)', re.I)
A_RE = re.compile(r'<a\b[^>]*?href\s*=\s*["\']([^"\'#]+)["\'][^>]*>(.*?)</a>', re.I | re.S)


def jsonld_jobs(htmltext, base):
    out = []
    for blk in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', htmltext or '', re.S | re.I):
        try: data = json.loads(blk.strip())
        except Exception: continue
        items = data if isinstance(data, list) else data.get('@graph', [data]) if isinstance(data, dict) else []
        for it in items:
            if isinstance(it, dict) and it.get('@type') in ('JobPosting', ['JobPosting']):
                loc = it.get('jobLocation') or {}
                if isinstance(loc, list): loc = loc[0] if loc else {}
                addr = (loc.get('address') or {}) if isinstance(loc, dict) else {}
                if not isinstance(addr, dict): addr = {}
                out.append({'title': clean(it.get('title')), 'url': urljoin(base, it.get('url') or base), 'posted': iso(it.get('datePosted')),
                            'deadline': (str(it.get('validThrough') or '')[:10] or None), 'location': ', '.join(x for x in [addr.get('addressLocality'), addr.get('addressCountry') if isinstance(addr.get('addressCountry'), str) else ''] if x),
                            'desc': clean(it.get('description'))[:4000], 'commit': str(it.get('employmentType') or ''), 'ext': it.get('url') or clean(it.get('title'))})
    return out


def page_jobs(c, htmltext, base, hosts=()):
    """Liens d'offres de stage trouvés dans une page (HTML statique ou rendu par le navigateur)."""
    out, seen = jsonld_jobs(htmltext, base), set()
    own_hosts = {host_of(base), host_of(site_of(c))} | {h for h in hosts if h}
    own_hosts = {h for h in own_hosts if h}
    for href, txt in A_RE.findall(htmltext or ''):
        t = clean(txt)
        if not (6 <= len(t) <= 180): continue
        u = urljoin(base, html.unescape(href.strip()))
        if not u.startswith('http') or u in seen: continue
        uh = host_of(u)
        own = any(uh == h or uh.endswith('.' + h) or h.endswith('.' + uh) for h in own_hosts)
        atsm = detect_ats(u)
        if not own and not (atsm and all(ats_ok(c, k, v) for k, v in atsm.items())) and not ATS_HOSTS.search(uh): continue
        pq = urlparse(u).path + '?' + urlparse(u).query
        if not (JOBLINK.search(pq) or atsm): continue
        if not is_intern(t) and not (is_intern(urlparse(u).path.replace('-', ' ').replace('_', ' ')) and len(t) > 12): continue
        if re.fullmatch(r'(postuler|apply|voir l.offre|en savoir plus|read more|learn more|d[ée]couvrir|view job|candidater|details?)', t, re.I): continue
        if not is_job_url(u): continue
        seen.add(u)
        out.append({'title': t, 'url': u, 'location': '', 'posted': None, 'ext': u, 'detail': ('page', u)})
    return out


CATEGORY = re.compile(r'^(?:early[-_]?careers?|graduates?|graduate[-_](?:jobs|programmes?|programs?|schemes?)|internships?|stages?|apprenticeships?|apprentices?|'
                      r'alternances?|students?|students[-_]and[-_]graduates|etudiants?|jeunes[-_]diplomes|programmes?|programs?|offres?|jobs?|careers?|carrieres?|'
                      r'vacatures?|vacancies|opportunities|opportunites|postes?|emplois?|join[-_]us|nous[-_]rejoindre|summer[-_]internships?|spring[-_]weeks?)$', re.I)
SEARCHY = re.compile(r'(?:^|[?&])(?:f%5B|f\[|filter|facet|search|keywords?|q|page|sort|category|categories|department)=', re.I)


def is_job_url(u):
    """Adresse d'une fiche d'offre (et pas d'une page de catégorie, d'une recherche ou d'une liste)."""
    p = urlparse(u)
    segs = [x for x in p.path.split('/') if x]
    last = (segs[-1] if segs else '').lower()
    last = re.sub(r'\.(html?|aspx?|php)$', '', last)
    if re.search(r'(?:^|[?&])(?:id|jobid|job_id|reqid|req_id|gh_jid|pid|jid|offerid|offer_id|vacancyid|ref)=\w', p.query, re.I): return True
    if SEARCHY.search(p.query): return False
    if not last or CATEGORY.match(last): return False
    if u.lower().endswith('.pdf'): return True
    return bool(re.search(r'\d', last)) or len(re.split(r'[-_]+', last)) >= 2


def count_joblinks(htmltext, base):
    """Nombre de liens qui ressemblent à des fiches d'offres (stage ou non) : indique qu'une page liste des offres."""
    n, seen = 0, set()
    for href, txt in A_RE.findall(htmltext or ''):
        u = urljoin(base, html.unescape(href.strip()))
        if u in seen or not u.startswith('http'): continue
        seen.add(u)
        t = clean(txt)
        if 6 <= len(t) <= 180 and (JOBLINK.search(urlparse(u).path + '?' + urlparse(u).query) or detect_ats(u)): n += 1
    return n


ROLE = re.compile(r"analyst|analyste|associate|assistant|m&a|private equity|capital[- ]investissement|investissement|investment|financ|audit|transaction|venture|"
                  r"fusions|acquisitions|corporate|banking|dette|debt|credit|cr[ée]dit|valuation|[ée]valuation|asset|gestion|trading|market|conseil|advisory|"
                  r"fund|fonds|equity|immobilier|real estate|infrastructure|business|stratég|strategy|risk|risque|compliance", re.I)
BLOCK_RE = re.compile(r'<(h[1-5]|li|a|strong|b|p|dt|td|span|div|button)\b([^>]*)>(.*?)</\1>', re.I | re.S)


BLOG = re.compile(r"[’']s\b|\b(?:closes|raises|announces?|launch(?:es)?|blog|post|news|article|interview|portrait|t[ée]moignage|retour d.exp|meet\b|day in the life|"
                  r"a day at|weeks? at|my |our story|we invest|we are|nous sommes|respecte|cookies?|privacy|is a graduate|congrat|welcome)", re.I)


def foreign(c, url):
    """L'offre est-elle publiée sur le compte d'une AUTRE entreprise (ex. job board des participations d'un fonds) ?"""
    found = detect_ats(url or '')
    m = (re.search(r'welcomekit\.co/companies/([\w-]+)', url or '', re.I) or re.search(r'([\w-]+)\.teamtailor\.com', url or '', re.I)
         or re.search(r'([\w-]+)\.welcomekit\.co', url or '', re.I))
    if m: found['wk'] = m.group(1)
    for k, v in found.items():
        if k in ('workday', 'oracle'): continue
        slug = v[0] if k == 'lever' else v
        if isinstance(slug, str) and not relevant(c, slug): return True
    return False


def mentions(htmltext, base):
    """Stages annoncés dans le texte d'une page « Nous rejoindre » (sans fiche d'offre à part).
    Ne garde que des intitulés courts qui nomment un poste (ex. « Stage Analyste M&A – janvier 2027 »)."""
    out, seen = [], set()
    body = re.sub(r'(?is)<(script|style|noscript|svg|nav|footer|header)[^>]*>.*?</\1>', ' ', htmltext or '')
    for tag, attrs, inner in BLOCK_RE.findall(body):
        if tag.lower() in ('div', 'span') and re.search(r'<(div|p|li|h\d|ul|table)\b', inner, re.I): continue
        t = clean(inner)
        if not (12 <= len(t) <= 140) or (t.endswith(('.', '?', ':')) and len(t) > 80): continue
        if len(t.split()) > 16 or not is_intern(t) or not ROLE.search(t): continue
        if BLOG.search(t): continue
        k = _norm(t)
        if k in seen: continue
        seen.add(k)
        href = None
        m2 = re.search(r'href\s*=\s*["\']([^"\'#]+)', attrs, re.I) if tag.lower() == 'a' else None
        m2 = m2 or re.search(r'<a\b[^>]*href\s*=\s*["\']([^"\'#]+)', inner, re.I)
        if m2:
            u = urljoin(base, html.unescape(m2.group(1)))
            if u.startswith('http'): href = u
        out.append({'title': t, 'url': href or base, 'location': '', 'posted': None, 'ext': 'm:' + k[:80]})
    return out


# ---------------------------------------------------------------- API cachées (réponses JSON d'un site)
TITLE_KEYS = ['title', 'jobTitle', 'job_title', 'postingTitle', 'positionTitle', 'requisitionTitle', 'displayJobTitle', 'jobName', 'job_name', 'jobPostingTitle',
              'PositionTitle', 'Title', 'titre', 'intitule', 'intitulePoste', 'libelle', 'name', 'text', 'label', 'position', 'role', 'designation']
URL_KEYS = ['url', 'jobUrl', 'job_url', 'absolute_url', 'hostedUrl', 'applyUrl', 'apply_url', 'jobDetailUrl', 'detailUrl', 'detailsUrl', 'canonicalPositionUrl',
            'externalUrl', 'externalPath', 'PositionURI', 'link', 'href', 'permalink', 'jobLink', 'job_link', 'urlPath', 'Url', 'Link', 'path', 'slug', 'seoUrl', 'friendlyUrl']
ID_KEYS = ['jobId', 'job_id', 'jobID', 'JobId', 'reqId', 'requisitionId', 'jobReqId', 'jobPostingId', 'postingId', 'PositionID', 'jobSeqNo', 'referenceNumber',
           'reference', 'ref', 'shortcode', 'externalId', 'uuid', 'code', 'objectID', 'id', 'Id', 'ID']
LOC_KEYS = ['locationsText', 'location', 'locations', 'locationName', 'primaryLocation', 'PrimaryLocation', 'PositionLocation', 'jobLocation', 'city', 'cityState',
            'office', 'address', 'country', 'ville', 'lieu', 'Location', 'multi_location', 'workplace']
DATE_KEYS = ['postedDate', 'datePosted', 'posted', 'postedOn', 'publishedAt', 'published_at', 'publicationDate', 'PostedDate', 'PublicationStartDate',
             'first_published', 'releasedDate', 'dateCreated', 'created_at', 'createdAt', 'creationDate', 'posted_date', 'publishDate', 'date', 'updated_at']
DESC_KEYS = ['description', 'shortDescription', 'ShortDescriptionStr', 'descriptionTeaser', 'summary', 'jobSummary', 'teaser', 'excerpt', 'descriptionPlain', 'content']
DEADLINE_KEYS = ['validThrough', 'closingDate', 'expirationDate', 'applicationDeadline', 'deadline', 'endDate', 'expiryDate', 'close_at']
COMMIT_KEYS = ['employmentType', 'contractType', 'contract_type', 'jobType', 'typeOfEmployment', 'type', 'jobSchedule', 'workType', 'contrat', 'typeContrat', 'category']
KW_KEYS = ['keyword', 'keywords', 'keyWords', 'searchText', 'searchTerm', 'searchKeyword', 'search_query', 'query', 'q', 'search', 'text', 'term', 'kw', 'what', 'motsCles', 'motcle']
PAGE_KEYS = ['page', 'pageNumber', 'page_number', 'pageIndex', 'pageNo', 'currentPage', 'pg', 'p']
OFFSET_KEYS = ['offset', 'start', 'from', 'skip', 'startIndex', 'startrow', 'first', 'begin']
SIZE_KEYS = ['limit', 'size', 'pageSize', 'page_size', 'per_page', 'perPage', 'rows', 'num', 'count', 'hitsPerPage', 'max', 'top', 'results_per_page', 'take', 'resultsPerPage']


def _first(d, keys):
    lk = {str(k).lower(): k for k in d}
    for k in keys:
        rk = lk.get(k.lower())
        if rk is not None and d[rk] not in (None, '', [], {}): return rk, d[rk]
    return None, None


def _text(v):
    if isinstance(v, str): return clean(v)
    if isinstance(v, (int, float)) and not isinstance(v, bool): return str(v)
    if isinstance(v, dict):
        for k in ('name', 'label', 'displayName', 'text', 'value', 'city', 'title', 'formatted', 'LocationName', 'fr', 'en'):
            if isinstance(v.get(k), str) and v.get(k): return clean(v[k])
        return clean(' '.join(str(x) for x in v.values() if isinstance(x, str)))[:120]
    if isinstance(v, list): return ', '.join(dict.fromkeys(filter(None, (_text(x) for x in v[:3]))))
    return ''


def _title(d):
    k, v = _first(d, TITLE_KEYS)
    if k is None: return None, ''
    t = _text(v)
    if not (3 <= len(t) <= 250) or re.match(r'https?://', t): return None, ''
    return k, t


def get_path(obj, path):
    """Éléments au bout d'un chemin ('*' = tous les éléments d'une liste)."""
    cur = [obj]
    for p in path:
        nxt = []
        for x in cur:
            if p == '*':
                if isinstance(x, list): nxt.extend(x)
            elif isinstance(x, dict) and p in x: nxt.append(x[p])
            elif isinstance(x, list) and isinstance(p, int) and -len(x) <= p < len(x): nxt.append(x[p])
        cur = nxt
    out = []
    for x in cur:
        if isinstance(x, list): out.extend(y for y in x if isinstance(y, dict))
        elif isinstance(x, dict): out.append(x)
    return out


def _walk_lists(obj, path=(), depth=0, out=None):
    if out is None: out = []
    if depth > 9: return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)): _walk_lists(v, path + (k,), depth + 1, out)
    elif isinstance(obj, list):
        if any(isinstance(x, dict) for x in obj): out.append(path)
        for x in obj[:3]:
            if isinstance(x, (dict, list)): _walk_lists(x, path + ('*',), depth + 1, out)
    return out


def best_job_list(js):
    """Chemin de la liste d'offres la plus probable dans une réponse JSON, avec sa note (0 si aucune)."""
    best, best_score = None, 0.0
    for path in set(_walk_lists(js)):
        items = get_path(js, path)
        if not items: continue
        tk, n_t, n_x, lens, n_job = None, 0, 0, [], 0
        for d in items[:200]:
            k, t = _title(d)
            if k is None: continue
            n_t += 1; tk = tk or k; lens.append(len(t))
            if _first(d, URL_KEYS)[0] or _first(d, ID_KEYS)[0]: n_x += 1
            if _first(d, LOC_KEYS)[0] or _first(d, DATE_KEYS)[0]: n_x += 0.5
            if INTERN.search(t) or ROLE.search(t) or re.search(r'engineer|manager|developer|sales|director|officer|specialist|consultant|head|lead', t, re.I): n_job += 1
        n = min(len(items), 200)
        if n_t < max(1, 0.6 * n): continue
        avg = sum(lens) / len(lens)
        if tk and tk.lower() in ('name', 'text', 'label', 'value') and (avg < 10 or n_x < 0.5 * n_t): continue
        if n_job < 0.3 * n_t: continue
        score = n_t * (1 + n_x / max(n_t, 1)) * (1 + n_job / max(n_t, 1))
        if score > best_score or (score == best_score and best is not None and len(path) < len(best)): best, best_score = path, score
    return (list(best) if best is not None else None), best_score


def map_item(d, base, tpl=None, tkey=None):
    _, title = _title(d)
    uk, uv = _first(d, URL_KEYS)
    ik, iv = _first(d, ID_KEYS)
    url = None
    if tpl and tkey and d.get(tkey) not in (None, ''):
        url = tpl.replace('{id}', str(d.get(tkey)))
    elif isinstance(uv, str) and uv:
        if uv.startswith('http'): url = uv
        elif uv.startswith('/') or '/' in uv: url = urljoin(base, uv)
    if not url and tpl and iv not in (None, ''): url = tpl.replace('{id}', str(iv))
    lk, lv = _first(d, LOC_KEYS)
    dk, dv = _first(d, DATE_KEYS)
    sk, sv = _first(d, DESC_KEYS)
    xk, xv = _first(d, DEADLINE_KEYS)
    ck, cv = _first(d, COMMIT_KEYS)
    dl = iso(xv) if xv else None
    return {'title': title, 'url': url, 'location': _text(lv)[:120] if lv else '', 'posted': iso(dv) if dv else None,
            'ext': str(iv) if iv not in (None, '') else (url or title), 'desc': _text(sv)[:3000] if sv else '',
            'deadline': dl[:10] if dl else None, 'commit': _text(cv)[:80] if cv else ''}


def html_in_json(js, out=None, depth=0):
    """Morceaux de HTML renvoyés dans du JSON (certaines plateformes renvoient la liste déjà mise en page)."""
    if out is None: out = []
    if depth > 8: return out
    if isinstance(js, str):
        if len(js) > 200 and '<a' in js and 'href' in js: out.append(js)
    elif isinstance(js, dict):
        for v in js.values(): html_in_json(v, out, depth + 1)
    elif isinstance(js, list):
        for v in js[:500]: html_in_json(v, out, depth + 1)
    return out


# -- réglage d'un paramètre dans une requête captée (adresse, corps JSON, formulaire, « params » à la Algolia)
def _qs(url): return parse_qsl(urlparse(url).query, keep_blank_values=True)


def find_param(spec, names):
    low = [n.lower() for n in names]
    for k, _ in _qs(spec['url']):
        if k.lower() in low: return ['q', k]
    body = spec.get('body')
    if body is None: return None
    if isinstance(body, (dict, list)):
        def walk(o, path, depth):
            if depth > 4: return None
            if isinstance(o, dict):
                for k, v in o.items():
                    if k.lower() in low and isinstance(v, (str, int, float, type(None))) and not isinstance(v, bool): return ['j', path + [k]]
                for k, v in o.items():
                    if k == 'params' and isinstance(v, str) and '=' in v:
                        for kk, _ in parse_qsl(v, keep_blank_values=True):
                            if kk.lower() in low: return ['jp', path + [k], kk]
                    if isinstance(v, (dict, list)):
                        r = walk(v, path + [k], depth + 1)
                        if r: return r
            elif isinstance(o, list):
                for i, v in enumerate(o[:3]):
                    r = walk(v, path + [i], depth + 1)
                    if r: return r
            return None
        return walk(body, [], 0)
    if isinstance(body, str) and '=' in body and not body.lstrip().startswith(('{', '[')):
        for k, _ in parse_qsl(body, keep_blank_values=True):
            if k.lower() in low: return ['f', k]
    return None


def _dig(o, path):
    for p in path: o = o[p]
    return o


def get_param(spec, loc):
    try:
        if loc[0] == 'q': return dict(_qs(spec['url'])).get(loc[1])
        if loc[0] == 'f': return dict(parse_qsl(spec['body'], keep_blank_values=True)).get(loc[1])
        if loc[0] == 'j': return _dig(spec['body'], loc[1])
        if loc[0] == 'jp': return dict(parse_qsl(_dig(spec['body'], loc[1]), keep_blank_values=True)).get(loc[2])
    except Exception:
        return None


def set_param(spec, loc, value):
    s = copy.deepcopy(spec)
    if loc[0] == 'q':
        u = urlparse(s['url']); q = [(k, (str(value) if k == loc[1] else v)) for k, v in _qs(s['url'])]
        s['url'] = urlunparse(u._replace(query=urlencode(q)))
    elif loc[0] == 'f':
        s['body'] = urlencode([(k, (str(value) if k == loc[1] else v)) for k, v in parse_qsl(s['body'], keep_blank_values=True)])
    elif loc[0] == 'j':
        par = _dig(s['body'], loc[1][:-1]); old = par[loc[1][-1]]
        par[loc[1][-1]] = (int(value) if isinstance(old, int) and str(value).lstrip('-').isdigit() else value)
    elif loc[0] == 'jp':
        par = _dig(s['body'], loc[1][:-1]); key = loc[1][-1]
        par[key] = urlencode([(k, (str(value) if k == loc[2] else v)) for k, v in parse_qsl(par[key], keep_blank_values=True)])
    return s


def send(spec, timeout=TIMEOUT):
    h = dict(spec.get('headers') or {})
    h.setdefault('Accept', 'application/json, text/plain, */*')
    body = spec.get('body')
    if spec.get('method', 'GET').upper() == 'GET':
        r = S.get(spec['url'], headers=h, timeout=timeout)
    elif isinstance(body, (dict, list)):
        r = S.request(spec['method'], spec['url'], json=body, headers=h, timeout=timeout)
    else:
        r = S.request(spec['method'], spec['url'], data=(body or '').encode('utf-8'), headers=h, timeout=timeout)
    r.raise_for_status()
    return r


def parse_json(text):
    t = (text or '').strip()
    if t.startswith(")]}'"): t = t.split('\n', 1)[-1]
    m = re.match(r'^[\w.$]+\((.*)\)\s*;?$', t, re.S)  # JSONP
    if m: t = m.group(1)
    return json.loads(t)


def items_from(js, rec, base):
    """Offres brutes extraites d'une réponse JSON selon la recette (chemin de la liste + modèle d'adresse)."""
    out = []
    if rec.get('path') is not None:
        for d in get_path(js, rec['path']):
            o = map_item(d, rec.get('base') or base, rec.get('tpl'), rec.get('tkey'))
            if o['title'] and o['url']: out.append(o)
    if rec.get('html'):
        for frag in html_in_json(js):
            out += page_jobs(rec.get('_c') or {}, frag, rec.get('base') or base, hosts=(host_of(base),))
    return out


def fetch_api(c, rec, max_pages=None):
    """Lit une API cachée découverte sur le site : recherche par mots-clés « stage » si l'API en accepte, sinon toutes les pages."""
    spec = rec['req']; base = rec.get('base') or spec['url']
    rec = dict(rec); rec['_c'] = c
    kw, pg = rec.get('kw'), rec.get('page')
    out, keys, ok, err = [], set(), [0], [0]

    def run(s, max_pages):
        start = 0
        if pg:
            try: start = int(get_param(s, pg['loc']) or 0)
            except Exception: start = 0
        first = None
        for i in range(max_pages):
            s2 = set_param(s, pg['loc'], start + i * (pg.get('step') or 1)) if (pg and i) else s
            try:
                js = parse_json(send(s2).text); ok[0] += 1
            except Exception:
                if i == 0: err[0] += 1
                break
            got = items_from(js, rec, base)
            if i == 0: first = tuple(sorted((o['url'] or o['ext']) for o in got))
            new = 0
            for o in got:
                k = o['url'] or o['ext']
                if k in keys: continue
                keys.add(k); out.append(o); new += 1
            if not pg or new == 0: break
        return first

    if kw:
        firsts = []
        for q in API_KW:
            firsts.append(run(set_param(spec, kw, q), max_pages or 3))
            # le site ignore le mot-clé (mêmes résultats pour deux mots différents) : on lit tout
            if len(firsts) == 2 and firsts[0] and firsts[0] == firsts[1]:
                run(spec, max_pages or 30); break
    else:
        run(spec, max_pages or 30)
    if not ok[0]: raise RuntimeError('api injoignable')
    return Partial(out) if err[0] else out


# ---------------------------------------------------------------- fiches détaillées
def detail_text(kind, url):
    if kind == 'oracle':
        host, site, rid = url.split('|')
        js = get(f'https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails?expand=all&onlyData=true'
                 f'&finder=ById;Id=%22{rid}%22,siteNumber={site}', headers={'Accept': 'application/json'}).json()
        it = (js.get('items') or [{}])[0]
        txt = clean(' '.join(it.get(k) or '' for k in ('ExternalDescriptionStr', 'ExternalResponsibilitiesStr', 'ExternalQualificationsStr', 'ShortDescriptionStr')))
        end = str(it.get('ExternalPostedEndDate') or '')[:10] or None
        return txt + ' ' + (it.get('ContractType') or '') + ' ' + (it.get('JobSchedule') or ''), end, it.get('PrimaryLocation') or '', None
    if kind == 'workday':
        js = get(url).json().get('jobPostingInfo', {})
        return clean(js.get('jobDescription')) + ' ' + (js.get('timeType') or ''), None, js.get('location') or '', iso(js.get('startDate'))
    if kind == 'sr':
        js = get(url).json(); sec = (js.get('jobAd') or {}).get('sections') or {}
        return clean(' '.join((v or {}).get('text', '') for v in sec.values())), None, '', None
    r = get(url); h = r.text
    lds = jsonld_jobs(h, url)
    if lds:
        l = lds[0]; return (l['desc'] or clean(h)[:6000]) + ' ' + l.get('commit', ''), l.get('deadline'), l.get('location', ''), l.get('posted')
    body = re.sub(r'(?is)<(script|style|nav|footer|header)[^>]*>.*?</\1>', ' ', h)
    return clean(body)[:8000], None, '', None
