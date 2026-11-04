#!/usr/bin/env python3
"""
Taxonomie unique des offres, portée du site (app/fincv.html) : metier_of() rend les mêmes libellés que TK_metier.
Aussi : front office, type de structure déduit (famx), langues demandées, e-mail de recrutement publié dans l'offre.
Rien n'est deviné au-delà de ce qui est écrit : sans indice, le champ reste vide.
"""
import re, unicodedata

# Généré depuis app/fincv.html (TK_CORPDEV, TK_METIERS, TK_NONFIN, TK_FINWORD, TK_FAM_METIER, TK_CITIES) : mêmes regex, à garder alignées avec le site.
# re.A : \b se comporte comme en JavaScript (les lettres accentuées ne sont pas des caractères de mot).
CORPDEV = re.compile(r"corporate development|corp\.? ?dev\b|m\s?&\s?a corporate|corporate m\s?&\s?a|direction (des |de la )?(fusions|acquisitions|strat[ée]gie)|group (strategy|m\s?&\s?a)|strat[ée]gie (et|&) (m\s?&\s?a|acquisitions|d[ée]veloppement)", re.A)
METIERS = [
    ('M&A', re.compile(r"\bm\s?&\s?a\b|\bm and a\b|merger|fusions?[- ]acquisitions?|\bm&a advisory|\bcession|sell[- ]side advisory", re.A)),
    ('Investment Banking (IBD / coverage)', re.compile(r"\bibd\b|investment bank|banque d'affaires|banque de financement et d'investissement|\bcib\b|corporate finance|origination|coverage|financial sponsors?|\bfig\b|global banking|\bib (summer|analyst|intern|off[- ]cycle|stage)|summer analyst.*\bib\b", re.A)),
    ('ECM / DCM / LevFin', re.compile(r"\bcapital markets\b(?!.*(sales|trading))|\becm\b|\bdcm\b|equity capital markets|debt capital markets|leveraged finance|acquisition finance|\blevfin\b|debt advisory|syndicat|high yield|primary markets|march[ée]s primaires", re.A)),
    ('Financements structurés / Corporate banking', re.compile(r"structured finance|project finance|real estate finance|asset finance|export finance|financements? (structur|de projet|d'actifs|immobilier)|corporate banking|transaction banking|trade finance|cash management|lending|commercial banking|financements?\b|financing", re.A)),
    ('Private Equity', re.compile(r"private equity|\bbuy[- ]?out|\blbo\b|capital[- ]investissement|capital[- ]d[ée]veloppement|growth equity|investment team|direct investment|co-?investment|fund of funds|fonds de fonds|secondaries|portfolio (company|management team)|operating partner|investissement (non )?cot[ée]", re.A)),
    ('Venture Capital', re.compile(r"venture|\bvc\b|startup|start-up|seed", re.A)),
    ('Dette privée / Infrastructure', re.compile(r"private (debt|credit)|dette priv[ée]e|direct lending|infrastructure|\binfra\b|mezzanine|unitranche", re.A)),
    ('Transaction Services / Évaluation', re.compile(r"transaction services|due diligence|\btas\b|valuation|[ée]valuation|restructur|turnaround|financial advisory|deal advisory|modelli?ng", re.A)),
    ('Audit', re.compile(r"\baudit\b|commissariat aux comptes|\bcac\b|audit & assurance|assurance (associate|graduate|intern|apprentice)", re.A)),
    ('Marchés / Sales & Trading', re.compile(r"\bsales\b|trading|trader|markets?\b|march[ée]s|structur(ing|eur)|derivati|d[ée]riv[ée]s|fixed income|equities|\bfx\b|forex|commodit|\brates?\b|xva|execution|salle des march", re.A)),
    ('Quant / Data', re.compile(r"quant|quantitative|data scien|machine learning|\bai\b|\bia\b|algorithm|strat[ée]giste quant|systematic", re.A)),
    ('Recherche / Crédit', re.compile(r"research|recherche|analyste cr[ée]dit|credit analy|credit research|rating|notation|economist|[ée]conomist", re.A)),
    ('Asset Management', re.compile(r"asset management|gestion d'actifs|portfolio manag|gestion de portefeuille|fund manag|multi[- ]asset|investment (analyst|office)|\bam\b|allocation|\besg\b|\bisr\b|sustainab", re.A)),
    ('Banque privée / Patrimoine', re.compile(r"wealth|private bank|banque priv[ée]e|patrimoin|family office|gestion de fortune|client advisor|relationship manager", re.A)),
    ('Risque / Conformité', re.compile(r"\brisk|risque|compliance|conformit|\bkyc\b|\baml\b|lcb-?ft|regulatory|r[ée]glementaire|contr[ôo]le permanent|inspection|internal control|financial crime|contr[ôo]le interne", re.A)),
    ('Immobilier', re.compile(r"real estate|immobili|\breim\b|property|asset manager immobilier|fonci[èe]re|\breit\b|investissement immobilier", re.A)),
    ('Assurance / Actuariat', re.compile(r"actuar|insurance|\bassurance\b|r[ée]assurance|reinsurance|souscript|underwrit", re.A)),
    ('Corporate development / Stratégie', re.compile(r"corporate development|corp\.? ?dev|strat[ée]gie|strategy|business development|strategic|m&a corporate|d[ée]veloppement corporate", re.A)),
    ("Finance d'entreprise", re.compile(r"contr[ôo]le de gestion|controll|contr[ôo]leur|financial analyst|analyste financ|finance trainee|investor relations|relations investisseurs|capital expenditure|performance financ|functions - finance|fp&a|financial planning|tr[ée]sor|treasury|comptab|accounting|accountant|consolidation|reporting|fiscal|\btax\b|finance (intern|stagiaire|analyst|manager)|business analyst finance|direction financi|financ(e|ial) (department|team|operations)|cost|budget|performance analyst|pricing", re.A)),
    ('Opérations / Middle-office', re.compile(r"operations|middle[- ]office|back[- ]office|fund admin|settlement|onboarding|client service|reconciliation", re.A)),
]
T_NONFIN = re.compile(r"marketing|communication|\brh\b|human resources|ressources humaines|talent acquisition|recrut|recruit|\bit\b|developer|d[ée]veloppeur|engineer|ing[ée]nieur|software|cyber|devops|cloud|design|achats|procurement|supply chain|logisti|legal|juridique|leasing|facilities|hospitality|hotels?|sales representative|vente|commercial (?!banking)|product manager|ux\b|graphi|seo|brand|evenementiel|event|people|payroll|paie|office manager|assistant", re.A)
T_FINWORD = re.compile(r"financ|invest|m\s?&\s?a|quant|trading|credit|cr[ée]dit|audit|risk|risque|compliance|treasury|tr[ée]sor|banking|valuation|equity|fund|fonds|wealth|asset|capital", re.A)
FAM_METIER = { 'pelarge': 'Private Equity', 'pemid': 'Private Equity', 'pesmall': 'Private Equity', 'vc': 'Venture Capital', 'debt': 'Dette privée / Infrastructure', 'ts': 'Transaction Services / Évaluation', 'eb': 'M&A', 'bfr': 'M&A', 'am': 'Asset Management', 're': 'Immobilier', 'consulting': 'Conseil en stratégie', 'wealth': 'Banque privée / Patrimoine', 'trading': 'Marchés / Sales & Trading', 'rating': 'Recherche / Crédit', 'fo': 'Private Equity' }
CITIES = [
    ('Paris', re.compile(r"paris|neuilly|levallois|boulogne|la d[ée]fense|courbevoie|puteaux|nanterre|issy|ile-de-france|[iî]le-de-france|fra\b", re.A)),
    ('Londres', re.compile(r"london|londres", re.A)),
    ('Luxembourg', re.compile(r"luxembourg", re.A)),
    ('Genève', re.compile(r"gen[eè]v|geneva", re.A)),
    ('Zurich', re.compile(r"z[uü]rich", re.A)),
    ('Francfort', re.compile(r"frankfurt|francfort", re.A)),
    ('Munich', re.compile(r"m[uü]nchen|munich", re.A)),
    ('Bruxelles', re.compile(r"brussels|bruxelles", re.A)),
    ('Amsterdam', re.compile(r"amsterdam", re.A)),
    ('Madrid', re.compile(r"madrid", re.A)),
    ('Milan', re.compile(r"milan|milano", re.A)),
    ('Lyon', re.compile(r"lyon", re.A)),
    ('Lille', re.compile(r"lille", re.A)),
    ('Marseille', re.compile(r"marseille", re.A)),
    ('Toulouse', re.compile(r"toulouse", re.A)),
    ('New York', re.compile(r"new york|\bny\b|nyc", re.A)),
    ('Dubaï', re.compile(r"duba", re.A)),
    ('Singapour', re.compile(r"singapore|singapour", re.A)),
    ('Hong Kong', re.compile(r"hong kong", re.A)),
]


def norm(s):
    """Comme OF_norm du site : sans accents, en minuscules."""
    s = unicodedata.normalize('NFD', str(s or ''))
    return re.sub(r'[̀-ͯ]', '', s).lower().strip()


METIER_ORDER = [k for k, _ in METIERS] + ['Conseil en stratégie', 'Programme général / spontanée', 'Autre', 'Hors finance']


def metier_of(o):
    """Métier d'une offre (traduction ligne à ligne de TK_metier). Sans « fam », le type déduit (famx) sert à sa place."""
    t = norm(o.get('title'))
    fam = o.get('fam') or o.get('famx') or ''
    if fam == 're' and not re.search(r'audit|risk|risque|compliance|comptab|accounting|tax|fiscal', t, re.A): return 'Immobilier'
    if fam == 'corp' and re.search(r'\bm\s?&\s?a\b|acquisition|strat[ée]g|strategy|corporate development|corporate finance|investment', t, re.A):
        return 'Corporate development / Stratégie'
    if CORPDEV.search(t): return 'Corporate development / Stratégie'
    if fam == 'ts' and re.search(r'corporate finance|lead advisory|m\s?&\s?a', t, re.A): return 'M&A'
    if fam == 'consulting' and re.search(r'strat[ée]g|strategy|consult', t, re.A): return 'Conseil en stratégie'
    if T_NONFIN.search(t) and not T_FINWORD.search(t): return 'Hors finance'
    for k, rx in METIERS:
        if rx.search(t): return k
    if re.search(r'\bfinance\b|financial|financier|financi[eè]re', t, re.A): return "Finance d'entreprise"
    if fam in FAM_METIER: return FAM_METIER[fam]
    if re.search(r'summer|intern|stage|stagiaire|graduate|students?|apprenti|alternan|trainee|spontan|open application|praktik|programme|program', t, re.A):
        return 'Programme général / spontanée'
    return 'Autre'


def city_of(o):
    """Comme TK_city du site (sert aux filtres des alertes)."""
    s = norm((o.get('location') or '') + ' ')
    for k, rx in CITIES:
        if rx.search(s): return k
    return 'Autre ville en France' if o.get('country') == 'France' else 'Autre ville'


# ---------------------------------------------------------------- front office
FRONT = {'M&A', 'Investment Banking (IBD / coverage)', 'ECM / DCM / LevFin', 'Financements structurés / Corporate banking', 'Private Equity', 'Venture Capital',
         'Dette privée / Infrastructure', 'Marchés / Sales & Trading', 'Asset Management', 'Banque privée / Patrimoine', 'Recherche / Crédit'}
NOT_FRONT = re.compile(r"middle[- ]office|back[- ]office|operations|\bops\b|support|fund (?:accounting|admin)|controll|contr[oô]l|compliance|conformit|\brisk|risque|\baudit|"
                       r"reporting|comptab|accounting|administrat|\bdata\b|\bit\b|\bhr\b|\brh\b", re.A)


def front_of(o, metier=None):
    """1 si le poste est front office, sinon None (le champ est alors absent)."""
    m = metier or o.get('metier') or metier_of(o)
    t = norm(o.get('title'))
    if m == 'Quant / Data': return 1 if re.search(r'quant|trad|research|recherche', t, re.A) and not NOT_FRONT.search(t) else None
    return 1 if m in FRONT and not NOT_FRONT.search(t) else None


# ---------------------------------------------------------------- type de structure
FAM_NAME = [('vc', r"\bventures?\b|venture capital|\bvc\b|capital[- ]risque|seed fund"), ('fo', r"family office"),
            ('wealth', r"wealth|patrimoine|gestion de fortune|private bank|banque privee"),
            ('re', r"real estate|immobili|\breim\b|\breit\b|fonciere|\bproperty\b"),
            ('debt', r"private debt|private credit|dette privee|direct lending|infrastructure|\binfra\b"),
            ('pemid', r"private equity|capital[- ]investissement|\bbuy[- ]?out"),
            ('am', r"asset manag|gestion d.actifs|investment manag|fund manag|\bgestion\b|\bam\b"),
            ('bfr', r"corporate finance|\bm&a\b|fusions|\badvisors?\b|\badvisory\b|banque d.affaires"),
            ('trading', r"\btrading\b|\bquant|securities|capital markets"), ('ts', r"\baudit\b|expertise comptable|accountants?\b"),
            ('consulting', r"consult|\bconseil\b"), ('fb', r"\bbanque\b|\bbank\b"), ('fintech', r"fintech|\bpayments?\b|neobank"),
            ('pemid', r"\bcapital\b|\bpartners\b|\binvest")]
FAM_NAME = [(k, re.compile(p, re.A)) for k, p in FAM_NAME]
FAM_TITLE = ('vc', 're', 'debt', 'pemid', 'am')


def famx_of(o):
    """Type de structure : celui de la liste, sinon déduit du nom, puis du titre pour les métiers sans ambiguïté."""
    if o.get('fam'): return o['fam']
    n = norm(o.get('company'))
    for k, rx in FAM_NAME:
        if rx.search(n): return k
    t = norm(o.get('title'))
    for k, rx in FAM_NAME[:-1]:
        if k in FAM_TITLE and rx.search(t): return k
    return ''


# ---------------------------------------------------------------- langues
LANGS = [('FR', r"francais|french|francophone"), ('EN', r"anglais|english|anglophone"), ('DE', r"allemand|\bgerman\b|deutsch"), ('ES', r"espagnol|spanish"),
         ('IT', r"italien|italian"), ('NL', r"neerlandais|dutch"), ('PT', r"portugais|portuguese"), ('ZH', r"mandarin|chinois|chinese"),
         ('JA', r"japonais|japanese"), ('AR', r"\barabe\b|arabic")]
LANGS = [(k, re.compile(p, re.A)) for k, p in LANGS]
LANG_CTX = re.compile(r"courant|bilingue|fluen|maitris|\bnative\b|proficien|\blangues?\b|\blanguages?\b|\bniveau\b|required|requis|exige|parle|written|spoken|ecrit|\boral\b|"
                      r"\bc1\b|\bc2\b|\bb2\b|toeic|toefl|ielts", re.A)
FR_WORDS = re.compile(r"\b(?:le|la|les|des|et|pour|dans|vous|nous|une|sur|avec)\b", re.A)
EN_WORDS = re.compile(r"\b(?:the|and|for|with|you|our|will|are|your|this)\b", re.A)


def langs_of(title, text=''):
    """Langues demandées dans l'offre ; à défaut, langue dans laquelle l'annonce est rédigée (texte assez long)."""
    t = norm((title or '') + ' . ' + (text or ''))[:12000]
    out = []
    for k, rx in LANGS:
        for m in rx.finditer(t):
            if LANG_CTX.search(t[max(0, m.start() - 50):m.end() + 50]):
                out.append(k); break
    if out or len(text or '') < 200: return out
    fr, en = len(FR_WORDS.findall(t)), len(EN_WORDS.findall(t))
    if fr >= 5 and fr >= 1.5 * en: return ['FR']
    if en >= 5 and en >= 1.5 * fr: return ['EN']
    return []


# ---------------------------------------------------------------- e-mail de recrutement
EMAIL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._%+-]*@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
MAIL_NO = re.compile(r"no-?reply|do-?not-?reply|ne-?pas-?repondre|\bdpo\b|privacy|rgpd|gdpr|data[._-]?protection|donnees|personal[._-]?data|webmaster|postmaster|abuse|unsubscribe|"
                     r"desabonnement|support|legal|juridique|compliance|ethics?|alert|press|presse|media|billing|facturation|\bsales\b", re.I)
MAIL_SOFT = re.compile(r"^(?:info|infos|contact|hello|bonjour|accueil|office|admin|secretariat|mail|general)$", re.I)
MAIL_DOM = re.compile(r"example\.|exemple\.|sentry|wixpress|domain\.com|email\.com|test\.com|\.(?:png|jpe?g|gif|svg|webp)$", re.I)
MAIL_RH = re.compile(r"recrut|\brh\b|\bhr\b|talent|career|carriere|\bjobs?\b|stage|candidat|apply|campus|people|intern", re.I)
MAIL_CTX = re.compile(r"candidat|\bcv\b|postul|apply|applications?|send|envoy|adress|resume|lettre|motivation|recrut|contact", re.I)


def contact_of(text):
    """E-mail de recrutement écrit dans l'offre (jamais une adresse de site : noreply, dpo, privacy…)."""
    t = text or ''
    best, score = '', 0
    for m in EMAIL.finditer(t):
        a = m.group(0).rstrip('.').lower()
        local, dom = a.split('@', 1)
        before = norm(t[max(0, m.start() - 80):m.start()])
        # le contexte « données personnelles » désigne une adresse de site, même si son nom a l'air normal
        if MAIL_NO.search(local) or MAIL_DOM.search(dom) or MAIL_DOM.search(a) or re.search(r"donnees personnelles|personal data|data protection|droit d.acces|droits? de rectification", before): continue
        ctx = bool(MAIL_CTX.search(before))
        if MAIL_SOFT.match(local) and not ctx: continue
        s = 3 if ctx and MAIL_RH.search(local) else 2 if ctx or MAIL_RH.search(local) else 1
        if s > score: best, score = a, s
    return best
