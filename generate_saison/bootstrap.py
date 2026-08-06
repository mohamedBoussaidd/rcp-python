"""
Bootstrap MULTI-CLUB du jeu de démo via l'API (chantier A).

Piloté par un SUPER_ADMIN existant (réutilisé), le générateur met en place, de façon
idempotente, 3 clubs de niveaux différents (cf. config.PROFILS) :

  1. création du club + de son PRÉSIDENT (POST /api/clubs — un login démo par club) ;
  2. affectation du PACK du club (PUT /api/admin/clubs/{id}/pack) ;
  3. pour chaque équipe du club : workers scopés à l'équipe (PREPARATEUR / ENTRAINEUR
     [+ MEDICAL si niveau], portant les droits d'écriture), fiches joueurs, comptes
     JOUEUR (saisie wellness/RPE), et la saison + périodes + effectif ;
  4. pour le club Pro : un rôle custom « Entraîneur adjoint ».

Chaque équipe donne un BootstrapContext (comme l'ancien mono-club), consommé ensuite
par les pushers filtrés selon le niveau du club.
"""

from __future__ import annotations

import dataclasses
import unicodedata
from dataclasses import dataclass, field

from . import catalog, config
from .api_client import ApiClient, ApiError
from .config import ProfilClub
from .profils import Joueur
from .simulation import SaisonSimulee, simuler


# Codes de type de séance attendus côté backend (catalogue global).
TYPES_ATTENDUS = ["REPRISE", "TECHNIQUE", "INTENSIF", "PRE_MATCH", "MATCH"]


@dataclass
class BootstrapContext:
    """Tout le nécessaire pour pousser les données d'UNE équipe d'un club de démo."""
    base_url: str
    club_id: str
    equipe_id: str
    equipe_nom: str
    profil: ProfilClub
    president: ApiClient                            # président du club (contexte club+équipe)
    workers: dict[str, ApiClient]                   # cle worker → client (scopé équipe)
    joueurs_clients: dict[str, ApiClient] = field(default_factory=dict)  # nom_complet → client JOUEUR
    type_seance_ids: dict[str, str] = field(default_factory=dict)        # code → id
    comptes_crees: list[tuple[str, str, str]] = field(default_factory=list)  # (role, email, mdp)
    effectif: list[Joueur] = field(default_factory=list)
    saison_sim: SaisonSimulee | None = None
    saison_id: str | None = None

    def worker(self, cle_donnee: str) -> ApiClient:
        """Client autorisé à écrire le type de donnée demandé (cf. config.ROLE_POUR)."""
        return self.workers[config.ROLE_POUR[cle_donnee]]

    def a_worker(self, cle_donnee: str) -> bool:
        return config.ROLE_POUR.get(cle_donnee) in self.workers


def _slug(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return "".join(c for c in s.lower() if c.isalnum())


# ═══════════════════════════════ Orchestration ═══════════════════════════════

def login_admin(base_url: str, email: str | None, password: str | None) -> ApiClient:
    """Connecte le SUPER_ADMIN pilote et vérifie son rôle (garde-fou)."""
    if not email or not password:
        raise RuntimeError(
            "Identifiants super-admin manquants : passez --admin-email/--admin-password "
            f"ou les variables d'env {config.ADMIN_EMAIL_ENV}/{config.ADMIN_PASSWORD_ENV}.")
    admin = ApiClient(base_url)
    auth = admin.login(email, password)
    if auth.get("role") != "SUPER_ADMIN":
        raise RuntimeError(
            f"Compte {email} : rôle {auth.get('role')} (SUPER_ADMIN attendu). "
            "Le générateur multi-club a besoin d'un super-admin pour créer clubs et packs.")
    return admin


def preparer_clubs(admin: ApiClient, base_url: str, params, log=print) -> list[BootstrapContext]:
    """Met en place les 3 clubs (idempotent) et renvoie un contexte par équipe, saison simulée incluse.

    IMPORTANT : toute l'ORCHESTRATION passe par le SUPER_ADMIN (bypass des permissions). Le président
    créé à la création du club n'a PAS d'affectation RBAC → aucune permission ; il ne sert donc que de
    login démo. Les WORKERS, eux, reçoivent leur affectation via /mon-club/membres et portent les
    droits d'écriture des données."""
    types: dict[str, str] | None = None
    contexts: list[BootstrapContext] = []

    for pi, profil in enumerate(config.PROFILS):
        log(f"\n=== Club « {profil.nom} » (pack {profil.pack}, {profil.nb_equipes} équipe(s)) ===")
        club_id = _creer_ou_trouver_club(admin, profil, log)
        _assigner_pack(admin, club_id, profil.pack, log)

        # Le super-admin agit DANS le club via le contexte (X-Contexte-Club).
        admin.set_contexte(club_id=club_id)

        if types is None:
            types = _charger_types_seance(admin)  # catalogue global, lu avec un club actif

        if profil.role_custom:
            _creer_role_custom(admin, club_id, profil, log)

        ctx_club: list[BootstrapContext] = []
        for idx in range(profil.nb_equipes):
            equipe_nom = config.EQUIPE_NOMS[idx] if idx < len(config.EQUIPE_NOMS) else f"Équipe {idx + 1}"
            equipe_cat = config.EQUIPE_CATEGORIES[idx] if idx < len(config.EQUIPE_CATEGORIES) else "Senior"
            equipe_id = _creer_ou_trouver_equipe(admin, club_id, equipe_nom, equipe_cat)
            log(f"  • {equipe_nom} ({equipe_id[:8]}…)")

            params_eq = dataclasses.replace(
                params, nb_joueurs=profil.nb_joueurs, intensite=profil.intensite,
                seed=params.seed + 1000 * (pi + 1) + idx)
            saison = simuler(params_eq)

            ctx = _preparer_equipe(base_url, admin, club_id, equipe_id, equipe_nom,
                                   profil, params_eq, types, saison.effectif, log)
            ctx.saison_sim = saison
            ctx_club.append(ctx)
            contexts.append(ctx)

        # Saison au niveau CLUB (une seule EN_COURS), puis périodes + effectif par équipe.
        saison_id = _assurer_saison(admin, ctx_club[0], params, log)
        for ctx in ctx_club:
            ctx.saison_id = saison_id
            _definir_periodes_effectif(admin, ctx, saison_id, log)

    return contexts


# ═══════════════════════════════ Étapes ═══════════════════════════════

def _creer_ou_trouver_club(admin: ApiClient, profil: ProfilClub, log) -> str:
    for c in admin.get("/api/clubs") or []:
        if c.get("nom") == profil.nom:
            return c["id"]
    cree = admin.post("/api/clubs", json={
        "nom": profil.nom,
        "president": {
            "email": profil.president_email,
            "nom": profil.president_nom,
            "prenom": profil.president_prenom,
            "motDePasse": config.PRESIDENT_DEMO_PASSWORD,
        },
    })
    log(f"  club créé (président {profil.president_email})")
    return cree["id"]


def _assigner_pack(admin: ApiClient, club_id: str, pack: str, log) -> None:
    admin.put(f"/api/admin/clubs/{club_id}/pack", json={"packCode": pack})
    log(f"  pack « {pack} » assigné")


def _creer_role_custom(admin: ApiClient, club_id: str, profil: ProfilClub, log) -> str | None:
    """Crée le rôle custom du club (idempotent par libellé). Best-effort (super-admin, contexte club)."""
    admin.set_contexte(club_id=club_id)
    try:
        for r in admin.get("/api/roles") or []:
            if r.get("libelle") == profil.role_custom and not r.get("systeme"):
                return r["id"]
        cree = admin.post("/api/roles", json={
            "libelle": profil.role_custom,
            "permissions": list(config.ROLE_CUSTOM_ADJOINT_PERMS),
        })
        log(f"  rôle custom « {profil.role_custom} » créé")
        return cree.get("id")
    except ApiError as e:
        log(f"  (rôle custom ignoré : {e.statut})")
        return None


def _creer_ou_trouver_equipe(admin: ApiClient, club_id: str, nom: str, categorie: str) -> str:
    admin.set_contexte(club_id=club_id)
    mon_club = admin.get("/api/mon-club")
    for e in mon_club.get("equipes", []):
        if e["nom"] == nom:
            return e["id"]
    cree = admin.post("/api/mon-club/equipes", json={"nom": nom, "categorie": categorie})
    return cree["id"]


def _preparer_equipe(base_url, admin, club_id, equipe_id, equipe_nom, profil,
                     params, types, effectif: list[Joueur], log) -> BootstrapContext:
    ctx = BootstrapContext(
        base_url=base_url, club_id=club_id, equipe_id=equipe_id, equipe_nom=equipe_nom,
        profil=profil, president=admin, workers={}, type_seance_ids=types, effectif=effectif,
    )

    # Création des membres pilotée par le super-admin, DANS le contexte du club.
    admin.set_contexte(club_id=club_id)

    # Workers scopés à l'équipe (medical seulement si le club a le module médical). Ils reçoivent
    # leur affectation RBAC via /mon-club/membres → ce sont eux qui écriront les données.
    membres = {m["email"].lower(): m for m in admin.get("/api/mon-club/membres")}
    for w in config.WORKERS:
        if w.cle == "medical" and not profil.medical:
            continue
        email = f"{_prefixe_worker(w.email)}.{profil.cle}{_suffixe_equipe(equipe_nom)}@{config.WORKER_EMAIL_DOMAIN}"
        _assurer_membre(admin, membres, ctx, email=email, role=w.role,
                        prenom=w.prenom, nom=w.nom, mdp=config.WORKER_PASSWORD,
                        equipe_id=equipe_id, joueur_id=None)
        client = ApiClient(base_url)
        client.login(email, config.WORKER_PASSWORD)
        client.set_contexte(club_id=club_id, equipe_ids=[equipe_id])
        ctx.workers[w.cle] = client

    # Fiches joueurs (par le préparateur) — idempotent par (nom, prénom) DANS l'équipe.
    #
    # ⚠ `/api/joueurs/tous` sérialise l'entité `Joueur`, qui n'a PLUS de champ `equipeId` depuis la
    # Phase 4 (V51 : le cache legacy a été supprimé, l'appartenance se dérive de `effectif_saison`).
    # Filtrer dessus donnait donc un dictionnaire TOUJOURS vide, et le générateur recréait tout
    # l'effectif à chaque exécution — 50 fiches par équipe au lieu de 25 après deux runs.
    # `/api/joueurs` est scopé par le contexte et résout l'équipe via l'effectif de la saison
    # EN_COURS : c'est la seule lecture qui dit encore « qui est dans cette équipe ».
    prepa = ctx.workers["preparateur"]
    existants = {(j["nom"], j["prenom"]): j["id"] for j in (prepa.get("/api/joueurs") or [])}
    for j in effectif:
        cle = (j.nom, j.prenom)
        # `pop` et non `get` : une fiche déjà attribuée à un joueur simulé ne peut pas l'être une
        # seconde fois. Deux homonymes tirés au sort recevaient sinon le MÊME backend_id, et
        # l'effectif partait avec un doublon que la contrainte unique rejetait en 500.
        if cle in existants:
            j.backend_id = existants.pop(cle)
        else:
            j.backend_id = prepa.post("/api/joueurs", json=_payload_joueur(j, params))["id"]

    # Comptes JOUEUR (par le super-admin, contexte club) + connexion de chacun.
    admin.set_contexte(club_id=club_id)
    membres = {m["email"].lower(): m for m in admin.get("/api/mon-club/membres")}
    for n, j in enumerate(effectif, start=1):
        email = f"{profil.cle}{_suffixe_equipe(equipe_nom)}.j{n}.{_slug(j.nom)}@{config.JOUEUR_EMAIL_DOMAIN}"
        j.compte_email = email
        _assurer_membre(admin, membres, ctx, email=email, role="JOUEUR",
                        prenom=j.prenom, nom=j.nom, mdp=config.JOUEUR_PASSWORD,
                        equipe_id=equipe_id, joueur_id=j.backend_id)
        client = ApiClient(base_url)
        client.login(email, config.JOUEUR_PASSWORD)
        ctx.joueurs_clients[j.nom_complet] = client

    return ctx


def _assurer_saison(admin: ApiClient, ctx: BootstrapContext, params, log) -> str:
    """
    Saison qui CONTIENT le calendrier simulé — réutilisée si elle existe, ouverte sinon.

    On ne se contente pas de « la saison EN_COURS » : générer plusieurs saisons successives est le
    cas d'usage normal (une saison passée + celle en cours), et réutiliser aveuglément l'EN_COURS
    injectait les séances hors de la saison censée les contenir. C'est exactement ce qui a produit
    un club de démo dont la vue séance montrait l'année précédente.

    Trois cas : la saison qui couvre le calendrier existe déjà → on la prend ; aucune saison ne le
    couvre → on l'ouvre (le backend clôture et BORNE la précédente au passage, cf. V105) ; le
    calendrier chevauche partiellement une saison → on refuse, parce qu'aucun découpage n'est
    évidemment le bon.
    """
    from datetime import date as _d, timedelta as _td
    admin.set_contexte(club_id=ctx.club_id, equipe_ids=[ctx.equipe_id])
    debut_sim = params.debut_saison
    fin_sim = debut_sim + _td(weeks=params.nb_semaines)

    existantes = admin.get("/api/saisons") or []
    for s in existantes:
        if not s.get("dateDebut") or not s.get("dateFin"):
            continue
        debut, fin = _d.fromisoformat(s["dateDebut"]), _d.fromisoformat(s["dateFin"])
        if debut <= debut_sim and fin_sim <= fin:                    # couvre entièrement
            # Elle doit être EN_COURS pour que l'injection fonctionne : l'appartenance d'un joueur
            # à une équipe se dérive de l'effectif de la saison EN_COURS (Phase 4). Sur une saison
            # clôturée, les joueurs n'appartiennent à rien et toute saisie de RPE ou de wellness
            # est refusée en « séance hors de votre équipe ».
            if s.get("statut") != "EN_COURS":
                admin.put(f"/api/saisons/{s['id']}", json={
                    "libelle": s["libelle"], "dateDebut": s["dateDebut"], "dateFin": s["dateFin"],
                    "statut": "EN_COURS", "genererPeriodes": False,
                })
                log(f"  saison « {s.get('libelle')} » rouverte pour l'injection")
            log(f"  calendrier {debut_sim} → {fin_sim} : dans la saison « {s.get('libelle')} »")
            return s["id"]
        if debut_sim <= fin and debut <= fin_sim:                    # recouvrement partiel
            raise RuntimeError(
                f"Le calendrier simulé ({debut_sim} → {fin_sim}) chevauche partiellement la saison "
                f"« {s.get('libelle')} » ({debut} → {fin}) sans y tenir entièrement.\n"
                f"  → alignez --debut-saison / --semaines sur cette saison, ou choisissez une "
                f"fenêtre entièrement libre.\n"
                f"  Deux saisons ne peuvent pas se recouvrir : le rattachement des séances se "
                f"déduit de leur date, il deviendrait ambigu."
            )

    # Aucune saison ne couvre la fenêtre : on l'ouvre. Les bornes suivent l'usage français
    # (1er juillet → 30 juin) tout en garantissant de contenir le calendrier simulé.
    debut_saison = _d(debut_sim.year, 7, 1) if debut_sim.month >= 7 else _d(debut_sim.year - 1, 7, 1)
    fin_saison = _d(debut_saison.year + 1, 6, 30)
    if debut_sim < debut_saison: debut_saison = debut_sim
    if fin_sim > fin_saison: fin_saison = fin_sim
    libelle = f"{debut_saison.year}-{debut_saison.year + 1}"

    cree = admin.post("/api/saisons", json={
        "libelle": libelle,
        "dateDebut": debut_saison.isoformat(),
        "dateFin": fin_saison.isoformat(),
        "statut": "EN_COURS",
        "genererPeriodes": True,
    })
    log(f"  saison « {libelle} » ouverte ({debut_saison} → {fin_saison})")
    return cree["id"]


def _definir_periodes_effectif(admin: ApiClient, ctx: BootstrapContext, saison_id: str, log) -> None:
    """Périodes par défaut + effectif de l'équipe dans la saison (idempotent, super-admin par équipe)."""
    admin.set_contexte(club_id=ctx.club_id, equipe_ids=[ctx.equipe_id])
    try:
        admin.post(f"/api/saisons/{saison_id}/periodes/defaut")
    except ApiError:
        pass  # déjà générées
    ids = list(dict.fromkeys(j.backend_id for j in ctx.effectif if j.backend_id))  # ordre conservé
    admin.put(f"/api/saisons/{saison_id}/effectif", json={"joueurIds": ids})


# ─────────────────────────── Helpers ───────────────────────────

def _prefixe_worker(email: str) -> str:
    """« prepa@staff… » → « prepa » (préfixe court réutilisé pour l'email par équipe)."""
    return email.split("@", 1)[0]


def _suffixe_equipe(equipe_nom: str) -> str:
    """Suffixe d'unicité par équipe (ex. « Équipe Réserve » → « reserve »)."""
    return _slug(equipe_nom.replace("Équipe", "")) or "e"


def _charger_types_seance(admin: ApiClient) -> dict[str, str]:
    types = {t["code"]: t["id"] for t in admin.get("/api/type-seances")}
    manquants = [c for c in TYPES_ATTENDUS if c not in types]
    if manquants:
        raise RuntimeError(f"Types de séance manquants côté backend : {manquants}")
    return types


def _assurer_membre(president, membres, ctx, *, email, role, prenom, nom, mdp, equipe_id, joueur_id):
    """
    Crée le membre s'il n'existe pas déjà (idempotent par email), et le REMET EN SERVICE s'il
    existe mais a été désactivé.

    La désactivation est un effet normal de la gestion d'effectif : un joueur écarté de la saison
    perd son accès PWA. Mais un compte de démo désactivé bloquait tout run ultérieur — l'email
    étant pris, on passait notre chemin, puis le login échouait en 401 au premier POST
    `/api/moi/wellness`. On le réactive et on le rattache à sa fiche courante.
    """
    existant = membres.get(email.lower())
    if existant and existant.get("id"):
        if existant.get("actif") is False:
            try:
                # `MembreUpdateRequest` ne porte que role / specialite / equipeId / actif.
                president.put(f"/api/membres/{existant['id']}", json={"actif": True})
                existant["actif"] = True
            except ApiError:
                pass   # droits insuffisants sur ce tier : le login échouera plus loin, plus clairement
        # Re-rattachement à la fiche COURANTE : une purge suivie d'une régénération recrée les
        # fiches avec de nouveaux identifiants, et un compte resté pointé sur l'ancienne n'appartient
        # à aucun effectif — toute saisie de RPE ou de wellness repartait en « séance hors de votre
        # équipe », sans que rien n'indique que le lien était en cause.
        if joueur_id and existant.get("joueurId") != joueur_id:
            try:
                president.put(f"/api/membres/{existant['id']}/fiche", json={"joueurId": joueur_id})
                existant["joueurId"] = joueur_id
            except ApiError:
                pass
        return
    if existant:
        return
    payload = {
        "email": email, "nom": nom, "prenom": prenom, "motDePasse": mdp,
        "role": role, "equipeId": equipe_id,
    }
    if joueur_id:
        payload["joueurId"] = joueur_id
    president.post("/api/mon-club/membres", json=payload)
    membres[email.lower()] = payload
    ctx.comptes_crees.append((role, email, mdp))


def _payload_joueur(j: Joueur, params) -> dict:
    return {
        "nom": j.nom,
        "prenom": j.prenom,
        "dateNaissance": j.date_naissance.isoformat(),
        "poidsActuel": j.poids_forme_kg,
        "poidsFormeCible": j.poids_forme_kg,
        "taille": j.taille_cm,
        "piedFort": j.pied_fort,
        "postePrincipal": catalog.POSTE_DB[j.poste],
        "profilAthletique": j.profil_athletique,
        "statut": "actif",
        "dateArriveeClub": params.debut_saison.isoformat(),
    }
