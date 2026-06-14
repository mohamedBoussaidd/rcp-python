"""
Bootstrap du tenant démo via l'API.

À partir du SEUL compte président créé à la main (compte@demo.fr), le générateur
met en place tout le reste, de façon idempotente :
  1. l'équipe démo (sous le club du président) ;
  2. les comptes "workers" (PREPARATEUR / ENTRAINEUR / MEDICAL) rattachés à
     l'équipe — ils portent les droits d'écriture spécialisés (cf. SecurityConfig)
     et un equipeId, indispensable car equipePourEcriture() lit l'équipe du compte ;
  3. les 25 fiches joueurs (créées par le préparateur) ;
  4. un compte JOUEUR par fiche (pour la saisie wellness/RPE côté joueur).

Renvoie un BootstrapContext : clients authentifiés + identifiants utiles.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field

from . import catalog, config
from .api_client import ApiClient, ApiError
from .profils import Joueur


# Codes de type de séance attendus côté backend (catalogue global).
TYPES_ATTENDUS = ["REPRISE", "TECHNIQUE", "INTENSIF", "PRE_MATCH", "MATCH"]


@dataclass
class BootstrapContext:
    base_url: str
    club_id: str
    equipe_id: str
    president: ApiClient
    workers: dict[str, ApiClient]                 # cle worker → client
    joueurs_clients: dict[str, ApiClient] = field(default_factory=dict)  # nom_complet → client JOUEUR
    type_seance_ids: dict[str, str] = field(default_factory=dict)        # code → id
    comptes_crees: list[tuple[str, str, str]] = field(default_factory=list)  # (role, email, mdp)

    def worker(self, cle_donnee: str) -> ApiClient:
        """Client autorisé à écrire le type de donnée demandé (cf. config.ROLE_POUR)."""
        return self.workers[config.ROLE_POUR[cle_donnee]]


def _slug(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return "".join(c for c in s.lower() if c.isalnum())


def bootstrap(base_url: str, params, effectif: list[Joueur],
              creer_effectif: bool = True) -> BootstrapContext:
    """Met en place le tenant démo. Si creer_effectif est False (mode purge), on
    s'arrête après les workers (pas de création de fiches ni de comptes joueurs)."""
    # 1) Président + garde-fous.
    president = ApiClient(base_url)
    auth = president.login(config.PRESIDENT_EMAIL, config.PRESIDENT_PASSWORD)
    if auth.get("role") != "PRESIDENT":
        raise RuntimeError(
            f"Compte {config.PRESIDENT_EMAIL} : rôle {auth.get('role')} (PRESIDENT attendu). "
            "Le générateur refuse de tourner pour éviter d'écrire hors du tenant démo.")
    club_id = auth.get("clubId")
    if not club_id:
        raise RuntimeError("Le président démo n'a pas de club rattaché — créez-le d'abord.")

    # 2) Équipe démo (idempotent).
    mon_club = president.get("/api/mon-club")
    equipe_id = _trouver_ou_creer_equipe(president, mon_club)
    president.set_contexte(club_id=club_id, equipe_ids=[equipe_id])

    ctx = BootstrapContext(
        base_url=base_url, club_id=club_id, equipe_id=equipe_id,
        president=president, workers={},
    )

    # 3) Catalogue des types de séance.
    ctx.type_seance_ids = _charger_types_seance(president)

    # 4) Workers (idempotent) + connexion.
    membres = {m["email"].lower(): m for m in president.get("/api/mon-club/membres")}
    for w in config.WORKERS:
        _assurer_membre(president, membres, ctx,
                        email=w.email, role=w.role, prenom=w.prenom, nom=w.nom,
                        mdp=config.WORKER_PASSWORD, equipe_id=equipe_id, joueur_id=None)
        client = ApiClient(base_url)
        client.login(w.email, config.WORKER_PASSWORD)
        client.set_contexte(club_id=club_id, equipe_ids=[equipe_id])
        ctx.workers[w.cle] = client

    if not creer_effectif:
        return ctx

    # 5) Fiches joueurs (par le préparateur) — idempotent par (nom, prenom).
    prepa = ctx.workers["preparateur"]
    existants = {(j["nom"], j["prenom"]): j["id"] for j in prepa.get("/api/joueurs/tous")}
    for j in effectif:
        cle = (j.nom, j.prenom)
        if cle in existants:
            j.backend_id = existants[cle]
        else:
            cree = prepa.post("/api/joueurs", json=_payload_joueur(j, params))
            j.backend_id = cree["id"]

    # 6) Comptes JOUEUR (par le président) + connexion de chacun.
    membres = {m["email"].lower(): m for m in president.get("/api/mon-club/membres")}
    for idx, j in enumerate(effectif, start=1):
        email = f"j{idx}.{_slug(j.nom)}@{config.JOUEUR_EMAIL_DOMAIN}"
        j.compte_email = email
        _assurer_membre(president, membres, ctx,
                        email=email, role="JOUEUR", prenom=j.prenom, nom=j.nom,
                        mdp=config.JOUEUR_PASSWORD, equipe_id=equipe_id, joueur_id=j.backend_id)
        client = ApiClient(base_url)
        client.login(email, config.JOUEUR_PASSWORD)
        ctx.joueurs_clients[j.nom_complet] = client

    return ctx


# ─────────────────────────── Helpers ───────────────────────────

def _trouver_ou_creer_equipe(president: ApiClient, mon_club: dict) -> str:
    for e in mon_club.get("equipes", []):
        if e["nom"] == config.EQUIPE_NOM:
            return e["id"]
    cree = president.post("/api/mon-club/equipes",
                          json={"nom": config.EQUIPE_NOM, "categorie": config.EQUIPE_CATEGORIE})
    return cree["id"]


def _charger_types_seance(president: ApiClient) -> dict[str, str]:
    types = {t["code"]: t["id"] for t in president.get("/api/type-seances")}
    manquants = [c for c in TYPES_ATTENDUS if c not in types]
    if manquants:
        raise RuntimeError(f"Types de séance manquants côté backend : {manquants}")
    return types


def _assurer_membre(president, membres, ctx, *, email, role, prenom, nom, mdp, equipe_id, joueur_id):
    """Crée le membre s'il n'existe pas déjà (idempotent par email)."""
    if email.lower() in membres:
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
