"""
Configuration centrale du générateur : environnements, identifiants du tenant
démo, paramètres de saison et garde-fous.

⚠️ Garde-fous prod (master = prod) :
  - toutes les écritures sont confinées au CLUB DÉMO (identifié par l'email du
    président démo) ; le code refuse de tourner si le compte connecté n'est pas
    ce président, ou si son club contient des données manifestement non-démo ;
  - la sortie SQL est interdite en --env prod ;
  - --env prod exige le flag --confirm.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


# ─────────────────────────── Environnements API ───────────────────────────

ENVIRONNEMENTS: dict[str, str] = {
    "local": "http://localhost:8080",
    "prod": "https://sportgestions.fr",
}


# ─────────────────────────── Comptes du tenant démo ───────────────────────────
# Le PRÉSIDENT est créé À LA MAIN par l'utilisateur (en base prod ET dev), avec
# le rôle PRESIDENT et un club rattaché. Le générateur s'y connecte puis crée
# lui-même les comptes "workers" (rôles d'écriture spécialisés) et les comptes
# JOUEUR, tous rattachés au club du président.

PRESIDENT_EMAIL = "compte@demo.fr"
PRESIDENT_PASSWORD = "demodaydaydemo9999"

# Mots de passe des comptes techniques créés par le générateur (club démo isolé).
WORKER_PASSWORD = "DemoWorker2026!"
JOUEUR_PASSWORD = "DemoJoueur2026!"

# Domaines email réservés au tenant démo (servent aussi de filet de sécurité :
# le générateur ne crée/supprime que des comptes sur ces domaines).
WORKER_EMAIL_DOMAIN = "staff.demo.fr"
JOUEUR_EMAIL_DOMAIN = "joueur.demo.fr"


@dataclass(frozen=True)
class CompteWorker:
    """Compte technique spécialisé, rattaché à l'équipe démo, créé par le président."""
    cle: str          # identifiant interne (preparateur / entraineur / medical)
    email: str
    role: str         # rôle backend (PREPARATEUR | ENTRAINEUR | MEDICAL)
    prenom: str
    nom: str


# Topologie des comptes workers : un par "famille" de droits d'écriture
# (cf. SecurityConfig). Le président ne peut pas tout écrire lui-même
# (joueurs/pesées/GPS/blessures lui sont interdits, et equipePourEcriture()
# renvoie null pour un président) → on passe par ces comptes scopés à l'équipe.
WORKERS: tuple[CompteWorker, ...] = (
    CompteWorker("preparateur", f"prepa@{WORKER_EMAIL_DOMAIN}", "PREPARATEUR", "Paul", "Préparateur"),
    CompteWorker("entraineur", f"coach@{WORKER_EMAIL_DOMAIN}", "ENTRAINEUR", "Éric", "Entraîneur"),
    CompteWorker("medical", f"medic@{WORKER_EMAIL_DOMAIN}", "MEDICAL", "Marie", "Médical"),
)

# Quel worker écrit quel type de donnée (clé = cle du worker).
ROLE_POUR = {
    "joueurs": "preparateur",
    "pesees": "preparateur",
    "gps": "preparateur",
    "seances": "preparateur",
    "wellness_traitement": "preparateur",
    "blessures": "medical",
    "conseils": "medical",
    "exercices": "entraineur",
    "formations": "entraineur",
    "schemas": "entraineur",
    "plan_de_jeu": "entraineur",
    "matchs": "entraineur",
}


# ─────────────────────────── Équipe démo ───────────────────────────

EQUIPE_NOM = "Équipe Première (Démo)"
EQUIPE_CATEGORIE = "Senior"


# ─────────────────────────── Paramètres de saison ───────────────────────────

@dataclass(frozen=True)
class ParametresSaison:
    """Tous les leviers de la simulation, regroupés et reproductibles via la seed."""

    seed: int = 42

    # Effectif
    nb_joueurs: int = 25

    # Calendrier (saison française type : août → mai)
    debut_saison: date = date(2025, 8, 4)        # 1er lundi de pré-saison
    nb_semaines: int = 40                         # ~40 microcycles
    semaine_treve_debut: int = 19                 # trêve hivernale (index semaine)
    nb_semaines_treve: int = 2

    # Réalisme blessures
    incidence_cible_par_saison: float = 20.0      # ~15-25 épisodes / saison / effectif
    seuil_acwr_risque: float = 1.5                # au-delà : risque accru

    # Densité de saisie subjective (tout le monde ne remplit pas chaque jour)
    taux_saisie_wellness: float = 0.85
    taux_saisie_rpe: float = 0.90

    def annee_libelle(self) -> str:
        fin = self.debut_saison.year + 1
        return f"{self.debut_saison.year}-{fin}"


DEFAUT = ParametresSaison()
