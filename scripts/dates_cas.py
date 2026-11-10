#!/usr/bin/env python3
"""
Cas de non-régression des règles de dates et du filtre « stage » (manuel, sans réseau) : python scripts/dates_cas.py
Chaque cas vient d'une formulation réelle d'offre. Les dates attendues supposent un lancement avant décembre 2026 ;
passé ce mois, les cas datés « 2026-12 » sont ignorés.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import commun as c  # noqa: E402

CUR = c.NOW.strftime('%Y-%m')
DEBUT = [  # (titre, texte, début attendu)
    ('Stage Pôle Transaction Services H/F', "un stage idéalement de 6 mois à partir d'avril 2027, un(e) stagiaire", '2027-04'),
    ('Stage', 'à compter d’octobre 2027 pour 6 mois', '2027-10'),
    ('Stage', 'à partir de fin janvier 2027', '2027-01'),
    ('Intern', 'from mid-January 2027 for a period of six months', '2027-01'),
    ('Stage césure', 'rentrée de janvier 2027', '2027-01'),
    ('Auditeur stagiaire de césure', 'Ce stage de césure de 6 mois, à partir de septembre 2026 ou de 3 à 6 mois en janvier 2027', '2027-01'),
    ('Stage', 'Stage à pourvoir en juillet 2026 ou janvier 2027', '2027-01'),
    ('Stage', 'Date de début prévue : Septembre 2026 et Janvier 2027', '2027-01'),
    ('Quant Research Internship', 'The role 10-week summer programme (21st June - 27th August 2027) 09:00-17:30', '2027-06'),
    ('Stage', 'du 5 janvier au 30 juin 2027', '2027-01'),
    ('Stage - Janvier - Juriste Financier H/F', '', f"{c._year_for(1)}-01"),
    ('Stage 6 mois Janv. 27', '', '2027-01'),
    ("Internship Jan '27", '', '2027-01'),
    ('Intern - Investment & Fund Operations (from Dec 26/Jan 27)', '', '2026-12'),
    ('Quantum Photonics Research Intern (H1/2027)', '', '2027-01'),
    ('2027 Investment Banking Off Cycle Internship Programme Paris (July start)', '', '2027-07'),
    ('Banking, Placement Analyst, Stockholm - Sweden, 2027 - January and July Intake', '', '2027-01'),
    # ce qui ne doit PAS donner de début
    ('Graduate Trainee Accountant, Practice, Mayo', '', None),
    ('Stage - Mars - M&A', '', None),
    ('Analyst Intern - May', '', None),
    ('Stage', 'commencing September 2026 and will end in April 2027', None),
    ('Intern', 'Applications open from January 2027', None),
    ('Intern', 'You must graduate from June 2027 onwards', None),
    ('Intern', 'Apply by 27 Nov 26', None),
    ('Stage', 'Date limite de candidature : fin janvier 2027', None),
]
OFFRE = [  # (intitulé, gardé comme stage ?)
    ('Early Careers Internship Program Structured Solutions Sales (100%)', True),
    ('Early Career Intern - Institutional Sales', True),
    ('Banking, Investment Banking, Placement Analyst, Frankfurt - Germany 2027', True),
    ('2027 Private Equity Infrastructure & Real Assets Co-op Analyst', True),
    ('Student Worker - Credit Ratings Analyst', True),
    ('Early Careers', False), ('Early Careers Internship Programme', False), ('Early careers internship opportunities', False),
    ('Teller Part-Time (20 Hours) - Co-Op City Branch', False), ('Careers - Internships at Goldman Sachs', False),
]
PAGE = [  # (texte du lien tel qu'il est écrit dans la page, intitulé attendu)
    ('Stagiair Corporate Finance Amsterdam Read More', 'Stagiair Corporate Finance Amsterdam'),
    ('Stage M&A - Paris En savoir plus →', 'Stage M&A - Paris'),
    ('Praktikant (m/w/d) Mergers & Acquisitions Jetzt bewerben', 'Praktikant (m/w/d) Mergers & Acquisitions'),
]
bad = 0
for t, want in PAGE:
    h = f'<a href="/careers/jobs/offre-1/">{t}</a>'
    got = [x['title'] for x in c.page_jobs({'id': 'x', 'name': 'X', 'url': 'https://x.example'}, h, 'https://x.example/careers/')]
    if got != [want]: bad += 1; print(f'PAGE   attendu {[want]}, obtenu {got}')
for t, x, want in DEBUT:
    if want and want < CUR: continue
    got = c.start_with_title(t, x)
    if got != want: bad += 1; print(f'DÉBUT  attendu {want}, obtenu {got} : {t} | {x}')
for t, want in OFFRE:
    got = c.keep_offer({'title': t, 'url': 'x'})
    if got != want: bad += 1; print(f'FILTRE attendu {want}, obtenu {got} : {t}')
print(f'{len(DEBUT) + len(OFFRE) + len(PAGE)} cas, {bad} en échec')
sys.exit(1 if bad else 0)
