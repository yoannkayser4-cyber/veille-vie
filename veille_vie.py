#!/usr/bin/env python3
"""
Veille V.I.E. - envoie un e-mail a chaque nouvelle offre publiee sur
mon-vie-via.businessfrance.fr (Business France / Civiweb).

Fonctionnement :
  1. Interroge l'API JSON utilisee par le site (toutes les pages).
  2. Compare les offres a celles deja vues (fichier offres_vues.json).
  3. Envoie UN e-mail recapitulatif avec les nouveautes (via Gmail).
  4. Met a jour offres_vues.json (le workflow GitHub le sauvegarde ensuite).

Premier lancement : toutes les offres existantes sont memorisees SANS e-mail
(sinon tu recevrais ~900 offres d'un coup).

Modes :
  python veille_vie.py            -> fonctionnement normal
  python veille_vie.py --test     -> envoie les 5 offres les plus recentes
                                     (pour verifier que l'e-mail arrive),
                                     sans toucher au fichier d'etat
  python veille_vie.py --apercu   -> affiche les offres dans la console,
                                     sans e-mail ni sauvegarde

Aucune dependance externe : uniquement la bibliotheque standard Python.
"""

import html
import json
import os
import smtplib
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

# ---------------------------------------------------------------------------
# REGLAGES (modifiables)
# ---------------------------------------------------------------------------

# Filtres optionnels. Listes vides = aucune restriction (toutes les offres).
# Comparaison insensible a la casse, sur le texte indique.
MOTS_CLES = []          # ex. ["commercial", "business", "sales", "export"] (titre)
PAYS_EXCLUS = []        # ex. ["Allemagne", "Belgique", "Espagne"]
PAYS_INCLUS = []        # ex. ["Canada", "Singapour", "Australie"] (si rempli, seuls ces pays)

# Nombre maximum d'offres detaillees dans un e-mail (le reste est resume).
MAX_OFFRES_PAR_MAIL = 60

# ---------------------------------------------------------------------------
# PARAMETRES TECHNIQUES
# ---------------------------------------------------------------------------

API_URL = os.environ.get(
    "VIE_API_URL", "https://civiweb-api-prd.azurewebsites.net/api/Offers/search"
)
# Cle publique du site (identique pour tous les visiteurs, visible via F12).
# Si l'API repond 401 un jour, la relever a nouveau sur le site
# (F12 > Reseau > requete "search" > en-tete X-API-KEY) et la mettre dans
# le secret GitHub VIE_API_KEY.
API_KEY = os.environ.get("VIE_API_KEY") or "l+KwpoLPiXlsjxNT/NQ2iOFz8+iuygxAODs9FeAEWYM="
SITE = "https://mon-vie-via.businessfrance.fr"
TAILLE_PAGE = 100       # l'API refuse au-dela d'environ 100
MAX_PAGES = 40          # garde-fou (40 x 100 = 4 000 offres)
TIMEOUT = 30

FICHIER_ETAT = Path(os.environ.get("VIE_ETAT", "offres_vues.json"))
MAX_IDS_MEMORISES = 6000

GMAIL_ADRESSE = os.environ.get("GMAIL_ADRESSE", "").strip()
GMAIL_MOT_DE_PASSE = "".join(os.environ.get("GMAIL_MOT_DE_PASSE_APPLI", "").split())
DESTINATAIRE = (os.environ.get("DESTINATAIRE") or "").strip() or GMAIL_ADRESSE
SMTP_HOTE = os.environ.get("SMTP_HOTE", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))

PAYLOAD = {
    "limit": TAILLE_PAGE,
    "skip": 0,
    "query": "",
    "activitySectorId": [],
    "missionsTypesIds": [],
    "countriesIds": [],
    "studiesLevelId": [],
    "companiesSizes": [],
    "specializationsIds": [],
    "entreprisesIds": [],
    "missionStartDate": None,
    "gerographicZones": [],
    "countriesFilterOperator": "OR",
    "specializationsFilterOperator": "OR",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "X-API-KEY": API_KEY,
    "Origin": SITE,
    "Referer": SITE + "/",
}

# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def appel_api(skip):
    corps = json.dumps(dict(PAYLOAD, skip=skip)).encode("utf-8")
    req = urllib.request.Request(API_URL, data=corps, headers=HEADERS, method="POST")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as rep:
        data = json.loads(rep.read().decode("utf-8"))
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for cle in ("result", "results", "data", "offers", "items"):
            if isinstance(data.get(cle), list):
                return data[cle]
    return []


def toutes_les_offres():
    """L'API ne trie pas par date : on parcourt toutes les pages."""
    offres, ids = [], set()
    for page in range(MAX_PAGES):
        lot = appel_api(page * TAILLE_PAGE)
        nouveaux = 0
        for brute in lot:
            o = normaliser(brute)
            if o["id"] and o["id"] not in ids:
                ids.add(o["id"])
                offres.append(o)
                nouveaux += 1
        if len(lot) < TAILLE_PAGE or nouveaux == 0:
            break
    # id plus grand = offre plus recente
    offres.sort(key=lambda o: cle_id(o["id"]), reverse=True)
    return offres


def premier(d, *cles, defaut=""):
    for c in cles:
        v = d.get(c)
        if v not in (None, "", []):
            return v
    return defaut


def normaliser(b):
    oid = premier(b, "id", "offerId", "Id", defaut=None)
    duree = premier(b, "missionDuration", "duration")
    try:
        duree = f"{int(duree)} mois"
    except (TypeError, ValueError):
        duree = str(duree)
    indem = premier(b, "indemnite", "indemnity", "allowance")
    try:
        indem = f"{float(indem):,.0f} €/mois".replace(",", " ")
    except (TypeError, ValueError):
        indem = str(indem)
    return {
        "id": str(oid) if oid is not None else None,
        "titre": str(premier(b, "missionTitle", "title", "label", defaut="(sans titre)")),
        "entreprise": str(premier(b, "organizationName", "companyName", "company", defaut="?")),
        "ville": str(premier(b, "cityName", "city")),
        "pays": str(premier(b, "countryName", "country")),
        "duree": duree,
        "indemnite": indem,
        "debut": str(premier(b, "missionStartDate", "startDate"))[:10],
        "publiee": str(premier(b, "startBroadcastDate", "creationDate"))[:10],
        "limite": str(premier(b, "endBroadcastDate", "endDate"))[:10],
        "url": f"{SITE}/offres/{oid}",
    }


def cle_id(oid):
    try:
        return int(oid)
    except (TypeError, ValueError):
        return 0


def passe_filtres(o):
    titre, pays = o["titre"].lower(), o["pays"].lower()
    if MOTS_CLES and not any(m.lower() in titre for m in MOTS_CLES):
        return False
    if PAYS_INCLUS and not any(p.lower() == pays for p in PAYS_INCLUS):
        return False
    if any(p.lower() == pays for p in PAYS_EXCLUS):
        return False
    return True

# ---------------------------------------------------------------------------
# ETAT
# ---------------------------------------------------------------------------

def lire_etat():
    if not FICHIER_ETAT.exists():
        return None
    try:
        return set(json.loads(FICHIER_ETAT.read_text(encoding="utf-8")).get("vues", []))
    except (OSError, ValueError):
        return None


def ecrire_etat(ids):
    garder = sorted(ids, key=cle_id)[-MAX_IDS_MEMORISES:]
    FICHIER_ETAT.write_text(
        json.dumps({"maj": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "vues": garder}, indent=1),
        encoding="utf-8",
    )

# ---------------------------------------------------------------------------
# E-MAIL
# ---------------------------------------------------------------------------

def date_fr(iso):
    try:
        return datetime.strptime(iso, "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        return iso


def carte_html(o):
    e = html.escape
    lieu = ", ".join(x for x in (o["ville"], o["pays"]) if x)
    details = " · ".join(x for x in (o["duree"], o["indemnite"]) if x)
    dates = []
    if o["debut"]:
        dates.append(f"Début : {date_fr(o['debut'])}")
    if o["limite"]:
        dates.append(f"Candidature avant le {date_fr(o['limite'])}")
    return f"""
<tr><td style="padding:14px 16px;border-bottom:1px solid #e5e7eb">
  <div style="font-size:12px;color:#2563eb;font-weight:600;text-transform:uppercase;letter-spacing:.03em">{e(lieu)}</div>
  <a href="{e(o['url'])}" style="font-size:16px;font-weight:700;color:#111827;text-decoration:none">{e(o['titre'])}</a>
  <div style="font-size:14px;color:#374151;margin-top:2px">{e(o['entreprise'])}</div>
  <div style="font-size:13px;color:#6b7280;margin-top:4px">{e(details)}</div>
  <div style="font-size:13px;color:#6b7280">{e(' · '.join(dates))}</div>
  <a href="{e(o['url'])}" style="display:inline-block;margin-top:8px;font-size:13px;color:#fff;background:#2563eb;padding:6px 12px;border-radius:6px;text-decoration:none">Voir l'offre</a>
</td></tr>"""


def construire_mail(offres, test=False):
    n = len(offres)
    pays = sorted({o["pays"] for o in offres if o["pays"]})
    resume_pays = ", ".join(pays[:6]) + ("…" if len(pays) > 6 else "")
    if test:
        sujet = f"[Veille V.I.E.] Test : les {n} offres les plus récentes"
    elif n == 1:
        o = offres[0]
        sujet = f"[Veille V.I.E.] {o['titre']} – {o['pays'] or o['entreprise']}"
    else:
        sujet = f"[Veille V.I.E.] {n} nouvelles offres ({resume_pays})"

    visibles = offres[:MAX_OFFRES_PAR_MAIL]
    reste = n - len(visibles)
    cartes = "".join(carte_html(o) for o in visibles)
    if reste:
        cartes += (f'<tr><td style="padding:14px 16px;color:#6b7280">… et {reste} autre(s) offre(s) : '
                   f'<a href="{SITE}/offres/recherche?latest=true">voir les dernières offres</a></td></tr>')
    intro = ("E-mail de test : voici les offres les plus récentes pour vérifier que la veille fonctionne."
             if test else f"{n} nouvelle(s) offre(s) V.I.E. publiée(s) depuis la dernière vérification.")
    corps_html = f"""<!doctype html><html><body style="margin:0;background:#f3f4f6;font-family:Arial,Helvetica,sans-serif">
<table width="100%" cellpadding="0" cellspacing="0" style="max-width:640px;margin:0 auto;background:#fff">
<tr><td style="padding:18px 16px;background:#111827;color:#fff;font-size:18px;font-weight:700">Veille V.I.E.</td></tr>
<tr><td style="padding:12px 16px;font-size:14px;color:#374151">{html.escape(intro)}</td></tr>
{cartes}
<tr><td style="padding:14px 16px;font-size:12px;color:#9ca3af">Source : mon-vie-via.businessfrance.fr · e-mail automatique envoyé par ton robot GitHub.</td></tr>
</table></body></html>"""

    lignes = [intro, ""]
    for o in visibles:
        lignes += [f"- {o['titre']} | {o['entreprise']} | {o['ville']}, {o['pays']} | "
                   f"{o['duree']} | {o['indemnite']}", f"  {o['url']}"]
    if reste:
        lignes.append(f"... et {reste} autre(s) : {SITE}/offres/recherche?latest=true")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = sujet
    msg["From"] = f"Veille V.I.E. <{GMAIL_ADRESSE}>"
    msg["To"] = DESTINATAIRE
    msg.attach(MIMEText("\n".join(lignes), "plain", "utf-8"))
    msg.attach(MIMEText(corps_html, "html", "utf-8"))
    return msg


def envoyer(msg):
    if not (GMAIL_ADRESSE and GMAIL_MOT_DE_PASSE):
        raise SystemExit("ERREUR : secrets GMAIL_ADRESSE / GMAIL_MOT_DE_PASSE_APPLI manquants.")
    print(f"Connexion Gmail avec l'adresse {GMAIL_ADRESSE!r} "
          f"(mot de passe : {len(GMAIL_MOT_DE_PASSE)} caracteres, 16 attendus)")
    try:
        with smtplib.SMTP_SSL(SMTP_HOTE, SMTP_PORT, timeout=TIMEOUT) as s:
            s.login(GMAIL_ADRESSE, GMAIL_MOT_DE_PASSE)
            s.send_message(msg)
    except smtplib.SMTPAuthenticationError:
        raise SystemExit(
            "ERREUR Gmail : adresse ou mot de passe d'application refuse.\n"
            "Verifie le secret GMAIL_ADRESSE (adresse complete) et recree un mot de "
            "passe d'application sur https://myaccount.google.com/apppasswords "
            "(validation en deux etapes obligatoire).")

# ---------------------------------------------------------------------------
# PROGRAMME
# ---------------------------------------------------------------------------

def main(args):
    try:
        offres = toutes_les_offres()
    except urllib.error.HTTPError as err:
        if err.code == 401:
            print("ERREUR 401 : la cle X-API-KEY a change (voir commentaire API_KEY).")
        else:
            print(f"ERREUR API : HTTP {err.code}")
        return 1
    except (urllib.error.URLError, TimeoutError, ValueError) as err:
        print(f"ERREUR reseau/API : {err}")
        return 1

    print(f"{len(offres)} offres en ligne actuellement.")
    if not offres:
        print("Aucune offre recue : on ne touche pas a l'etat (probable souci API).")
        return 1

    if "--apercu" in args:
        for o in offres[:20]:
            print(f"  #{o['id']} {o['titre']} | {o['entreprise']} | {o['pays']} | publiee {o['publiee']}")
        return 0

    if "--test" in args:
        envoyer(construire_mail([o for o in offres if passe_filtres(o)][:5], test=True))
        print("E-mail de test envoye.")
        return 0

    vues = lire_etat()
    ids_actuels = {o["id"] for o in offres}
    if vues is None:
        ecrire_etat(ids_actuels)
        print(f"Premier lancement : {len(ids_actuels)} offres memorisees, aucun e-mail.")
        return 0

    nouvelles = [o for o in offres if o["id"] not in vues]
    a_envoyer = [o for o in nouvelles if passe_filtres(o)]
    print(f"{len(nouvelles)} nouvelle(s) offre(s), {len(a_envoyer)} apres filtres.")

    if a_envoyer:
        envoyer(construire_mail(a_envoyer))   # si l'envoi echoue, l'etat n'est pas mis a jour
        print("E-mail envoye.")

    if nouvelles:
        ecrire_etat(vues | ids_actuels)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
