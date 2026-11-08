#!/usr/bin/env python3
"""Veille hebdo : sociétés financières françaises créées ces 12 derniers mois.

Sources (gratuites, sans clé) :
  1. BODACC (annonces-commerciales, OpenDataSoft) : avis de création, filtrés par
     expressions exactes dans l'objet social, puis reclassés localement par regex.
  2. AMF GECO (liste des SGP, CSV data.gouv) : SGP agréées depuis < 12 mois (+ site web).
  3. API Recherche d'entreprises : enrichissement par SIREN (date de création, NAF,
     ville, état administratif). Pas de filtre par date côté API -> pas utilisée
     comme source primaire.

Écrit data/nouvelles.json ({updated, items}), lu par le site (module Tracker, carte « Nouvelles structures »).
Le journal (dépôt public) ne montre que des comptes.
Usage : python nouvelles.py [--days 365] [--geco chemin.csv] [--out chemin.json]
"""
import argparse, csv, datetime as dt, io, json, os, re, sys, time, unicodedata
from urllib.parse import quote_plus
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
BODACC = "https://bodacc-datadila.opendatasoft.com/api/explore/v2.1/catalog/datasets/annonces-commerciales/records"
RECH = "https://recherche-entreprises.api.gouv.fr/search"
GECO_DATASET = "https://www.data.gouv.fr/api/1/datasets/651427eaf6eab90fa3db2da3/"  # liste des SGP agréées (AMF), dernier export
DATA = os.environ.get("DATA_DIR") or os.path.join(HERE, "..", "data")
KNOWN = os.path.join(DATA, "structures.json")
UA = {"User-Agent": "finset-veille/1.0 (nouvelles structures)"}
S = requests.Session(); S.headers.update(UA)

# Expressions envoyées à BODACC (opérateur `like` = expression exacte, insensible à la casse)
PHRASES = [
    "fusions et acquisitions", "fusions-acquisitions", "haut de bilan", "corporate finance",
    "private equity", "capital-investissement", "capital-risque", "venture capital",
    "family office", "gestion de portefeuille pour compte de tiers",
    "gestion de portefeuille pour le compte de tiers", "gestion collective",
    "gestion de fortune", "conseil en investissements financiers", "levée de fonds",
    "cession et transmission d'entreprises",
]

# Reclassement local (ordre = priorité)
RX = [
    ("Family office", r"family[\s-]office"),
    ("M&A", r"fusions?[\s-]+(et[\s-]+)?(d')?acquisitions?|\bm&a\b|haut de bilan|corporate finance|"
            r"cessions? (et|ou) (de )?transmissions? d'entreprises|"
            r"lev[ée]es? de fonds"),
    ("VC", r"capital[\s-]risque|venture[\s-]capital|\bventures?\b"),
    ("PE", r"private[\s-]equity|capital[\s-]investissement|\blbo\b"),
    ("Gestion", r"gestion (de portefeuilles? )?pour (le )?compte de tiers|gestion collective|"
                r"gestion de fortune|asset management|gestion d'actifs|"
                r"conseil en investissements? financiers?"),
]
# Signes de « vrai » prestataire / gérant (et pas un véhicule patrimonial)
PRO = r"conseil|accompagnement|compte de tiers|investisseurs|fonds d'investissement|gestion de fonds|" \
      r"assistance|prestations?|intermédiation|ingénierie financière|agr[ée]ment|autorité des marchés"
# Bruit fréquent
NOISE_ACT = r"associ[ée] commandit|assistance du fonds|marchand de biens|location meubl|restauration|" \
            r"conciergerie|courtage en assurance|courtier en assurance|association|fundraising|" \
            r"trading .*compte propre|prop firm|bâtiment|travaux"
NOISE_NAME = r"\bholding\b|\bsci\b|\bgp\b|\bslp\b|\bfcpr?\b|\bfpci\b|\d+\s*$"
# NAF SIRENE acceptés pour les lignes BODACC (vérifiés après enrichissement)
NAF_OK = {"66.30Z", "66.19B", "66.19A", "66.12Z", "64.30Z", "64.99Z", "70.22Z", "66.11Z"}
HOLDING_START = r"^\W*(la |le |l'|toutes? )?(prise|acquisition|d[ée]tention|souscription|gestion d'un portefeuille|investissement pour compte propre)"


def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\b(sas|sasu|sarl|sa|eurl|sca|scs|snc|selarl|france|group|groupe|the|et|and|&)\b", " ", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def links(nom, ville):
    return ("https://www.linkedin.com/search/results/companies/?keywords=" + quote_plus(nom),
            "https://www.google.com/search?q=" + quote_plus(f'"{nom}" {ville or ""} finance'.strip()))


def get(url, params, tries=4):
    for i in range(tries):
        try:
            r = S.get(url, params=params, timeout=30)
            if r.status_code == 429:
                time.sleep(2 * (i + 1)); continue
            r.raise_for_status(); return r
        except requests.RequestException as e:
            if i == tries - 1: raise
            time.sleep(1.5 * (i + 1))


# ---------------------------------------------------------------- BODACC
def bodacc(since):
    likes = " OR ".join(f'listeetablissements like "%{p}%"' for p in PHRASES)
    where = f'familleavis="creation" AND dateparution>="{since}" AND ({likes})'
    sel = "id,dateparution,commercant,ville,registre,listepersonnes,listeetablissements,acte,url_complete"
    out, off = [], 0
    while True:
        r = get(BODACC, {"where": where, "select": sel, "limit": 100, "offset": off,
                         "order_by": "dateparution desc"}).json()
        res = r.get("results", []); out += res; off += 100
        if not res or off >= min(r.get("total_count", 0), 9900): break
        time.sleep(0.25)
    return out


def parse_bodacc(x):
    try: pers = json.loads(x["listepersonnes"] or "{}").get("personne", {})
    except Exception: pers = {}
    if isinstance(pers, list): pers = pers[0] if pers else {}
    try: et = json.loads(x["listeetablissements"] or "{}").get("etablissement", {})
    except Exception: et = {}
    ets = et if isinstance(et, list) else [et]
    act = " ".join((e.get("activite") or "") for e in ets)
    try: acte = json.loads(x["acte"] or "{}")
    except Exception: acte = {}
    reg = x.get("registre") or []
    siren = re.sub(r"\D", "", reg[0]) if reg else ""
    nom = pers.get("denomination") or x.get("commercant") or ""
    return dict(nom=nom, siren=siren, type=pers.get("typePersonne"), activite=act,
                ville=x.get("ville"), date_immat=acte.get("dateImmatriculation") or x["dateparution"],
                cat_creation=(acte.get("creation") or {}).get("categorieCreation", ""),
                url=x.get("url_complete"), nomcom=pers.get("nomCommercial") or "")


def classify(nom, act):
    t = (nom + " | " + act).lower()
    for cat, rx in RX:
        m = re.search(rx, t)
        if m: return cat, m.group(0)
    return None, None


def keep_bodacc(p):
    """Renvoie (categorie, raison_rejet)."""
    if p["type"] == "pp": return None, "personne physique"
    if "transfert" in p["cat_creation"].lower(): return None, "transfert de siège"
    cat, hit = classify(p["nom"], p["activite"])
    if not cat: return None, "aucun mot-clé fort"
    a = p["activite"].lower()
    if re.search(NOISE_NAME, p["nom"].lower()): return None, "nom de véhicule/holding"
    if re.search(NOISE_ACT, a): return None, "activité bruit"
    weak = cat == "M&A" and re.match(r"lev|cession", hit) or (cat == "Gestion" and "conseil en invest" in hit)
    if not re.search(PRO, a): return None, "pas d'activité de service/gestion pour tiers"
    if re.search(HOLDING_START, a) and not re.search(r"compte de tiers|conseil", a[:400]):
        return None, "objet de holding"
    if re.search(r"(propriété|détention) et (la )?gestion d'un portefeuille|acquisition, la souscription, la gestion et la cession de toutes parts", a) and "tiers" not in a:
        return None, "objet de holding"
    if re.search(r"pour (son )?(propre )?compte propre|pour son propre compte", a) and "tiers" not in a:
        return None, "investissement pour compte propre"
    if weak:
        # CIF / levée de fonds seuls : on exige un vocabulaire entreprise/finance en plus
        if not re.search(r"entreprises|dirigeants|financ|patrimoine|capital", a):
            return None, "signal faible"
        if re.search(r"courtage|courtier|assurance|immobilier", a) and cat == "Gestion":
            cat = "Gestion"  # CGP : on garde mais on le signale
    return cat, None


# ---------------------------------------------------------------- GECO
def geco(path, since):
    if path and os.path.exists(path):
        txt = open(path, encoding="utf-8-sig").read()
    else:
        res = get(GECO_DATASET, {}).json().get("resources") or []
        url = next(r["url"] for r in sorted(res, key=lambda r: r.get("last_modified") or "", reverse=True) if (r.get("format") or "").lower() == "csv")
        txt = get(url, {}).content.decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(txt), delimiter=";"))
    by = {}
    for r in rows:
        if r.get("date_debut_autorisation", "") >= since and r.get("statut") == "Vivant":
            by.setdefault(r["no_amf"], []).append(r)
    out = []
    for amf, rs in by.items():
        r0 = rs[0]; acts = " ".join(x["libelle_activite"] for x in rs)
        nom = r0["entite_nom"].strip(); n = nom.lower()
        if "family office" in n: cat = "Family office"
        elif re.search(r"ventures?|\bvc\b|founders", n) or ".vc" in r0["site_internet"]: cat = "VC"
        elif "4 - Instruments financiers non admis" in acts and "1 - Instruments négociés" not in acts: cat = "PE"
        else: cat = "Gestion"
        site = r0["site_internet"].strip()
        if site and not site.startswith("http"): site = "https://" + site
        out.append(dict(nom=nom, siren=re.sub(r"\D", "", r0["no_registre_national"]), cat=cat,
                        site=site or None, date_agrement=r0["date_debut_autorisation"], no_amf=amf))
    return out


# ---------------------------------------------------------------- Enrichissement
def enrich(siren=None, nom=None):
    p = {"per_page": 5, "minimal": "true", "include": "siege"}
    if siren: p["q"] = siren
    else: p.update(q=nom, section_activite_principale="K")
    try: res = get(RECH, p).json().get("results", [])
    except Exception: return {}
    time.sleep(0.16)  # ~6 req/s (limite annoncée : 7 req/s)
    if not res: return {}
    r = res[0]
    if not siren and norm(r.get("nom_complet")) != norm(nom) and norm(nom) not in norm(r.get("nom_complet")):
        return {}
    sg = r.get("siege") or {}
    return dict(siren=r["siren"], date_creation=r.get("date_creation"), naf=r.get("activite_principale"),
                ville=sg.get("libelle_commune"), etat=r.get("etat_administratif"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--geco", default="")
    ap.add_argument("--out", default=os.path.join(DATA, "nouvelles.json"))
    a = ap.parse_args()
    t0 = time.time()
    since = (dt.date.today() - dt.timedelta(days=a.days)).isoformat()

    known = {}
    try:
        for s in json.load(open(KNOWN)): known[norm(s.get("name"))] = s.get("id")
    except Exception as e: print("structures.json illisible :", e, file=sys.stderr)

    def is_known(nom):
        n = norm(nom)
        if n in known: return known[n]
        for k, v in known.items():
            if len(k) >= 6 and len(n) >= 6 and (n.startswith(k + " ") or k.startswith(n + " ")): return v
        return None

    rows, rejects = {}, {}
    raw = bodacc(since)
    print(f"BODACC : {len(raw)} avis bruts", file=sys.stderr)
    for x in raw:
        p = parse_bodacc(x)
        cat, why = keep_bodacc(p)
        if not cat:
            rejects[why] = rejects.get(why, 0) + 1; continue
        key = p["siren"] or norm(p["nom"])
        if key in rows: continue
        rows[key] = dict(nom=p["nom"], siren=p["siren"], date_creation=p["date_immat"], ville=p["ville"],
                         categorie=cat, source="BODACC", site=None, activite=p["activite"][:300],
                         bodacc_url=p["url"])

    g = geco(a.geco, since)
    print(f"GECO : {len(g)} SGP agréées depuis {since}", file=sys.stderr)
    for s in g:
        e = enrich(s["siren"]) if s["siren"] else enrich(nom=s["nom"])
        key = e.get("siren") or s["siren"] or norm(s["nom"])
        base = rows.get(key, {})
        rows[key] = dict(nom=s["nom"], siren=e.get("siren") or s["siren"] or None,
                         date_creation=e.get("date_creation"), ville=e.get("ville"), categorie=s["cat"],
                         source="AMF GECO" + ("+BODACC" if base else ""), site=s["site"],
                         activite="SGP agréée AMF " + s["no_amf"], date_agrement=s["date_agrement"],
                         naf=e.get("naf"))

    # Enrichissement SIRENE des lignes BODACC (vraie date de création, NAF, état)
    for k, r in rows.items():
        if r["source"] != "BODACC" or not r["siren"]: continue
        if time.time() - t0 > 270: break
        e = enrich(r["siren"])
        if e:
            r["date_creation"] = e.get("date_creation") or r["date_creation"]
            r["naf"] = e.get("naf"); r["etat"] = e.get("etat")
            r["ville"] = e.get("ville") or r["ville"]
            if r["naf"] and r["naf"] not in NAF_OK:
                r["etat"] = "NAF_HORS_PERIMETRE"

    out = []
    for r in rows.values():
        if r.get("etat") == "C": continue  # cessée
        if r.get("etat") == "NAF_HORS_PERIMETRE":
            rejects["NAF hors périmètre"] = rejects.get("NAF hors périmètre", 0) + 1; continue
        if r["source"] == "BODACC" and (r["date_creation"] or "") < since: continue
        r["linkedin_search"], r["google_search"] = links(r["nom"], r["ville"])
        r["connue"] = bool(is_known(r["nom"]))
        r.pop("activite", None)
        out.append(r)
    out.sort(key=lambda r: r.get("date_agrement") or r["date_creation"] or "", reverse=True)
    doc = {"updated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "days": a.days, "items": out}
    if out or not os.path.exists(a.out):  # source en panne : on garde la liste précédente
        json.dump(doc, open(a.out, "w"), ensure_ascii=False, indent=1)
    from collections import Counter
    print(json.dumps(dict(total=len(out), par_categorie=Counter(r["categorie"] for r in out),
                          par_source=Counter(r["source"] for r in out), rejets=rejects,
                          deja_connus=sum(1 for r in out if r["connue"]),
                          duree_s=round(time.time() - t0)), ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
