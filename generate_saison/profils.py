"""
Génération de l'effectif : 25 profils joueurs cohérents et reproductibles.

Chaque joueur porte une identité (poste, âge, anthropométrie réaliste selon le
poste) et des traits cachés qui pilotent la simulation (niveau de forme de base,
fragilité, sérieux de saisie du ressenti).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np

from . import catalog
from .config import ParametresSaison


@dataclass
class Joueur:
    # Identité
    prenom: str
    nom: str
    poste: str                 # code poste_principal
    famille: str               # famille de charge (catalog.FAMILLE)
    date_naissance: date
    taille_cm: int
    poids_forme_kg: float      # poids de forme cible (IMC athlétique)
    pied_fort: str
    profil_athletique: str

    # Traits cachés (0..1) pilotant la simulation
    forme_base: float          # capacité athlétique de base
    fragilite: float           # propension aux blessures
    serieux_saisie: float      # régularité de saisie wellness/RPE
    vitesse_max_kmh: float     # pointe de vitesse individuelle

    # Rempli après création côté backend
    backend_id: str | None = None
    compte_email: str | None = None

    @property
    def nom_complet(self) -> str:
        return f"{self.prenom} {self.nom}"


def _age_pour_poste(rng: np.random.Generator) -> int:
    # 18-34 ans, pic autour de 25.
    age = int(round(rng.normal(25, 4)))
    return max(18, min(34, age))


def generer_effectif(params: ParametresSaison) -> list[Joueur]:
    """Construit l'effectif complet de façon déterministe à partir de la seed."""
    rng = np.random.default_rng(params.seed)

    # Postes selon la composition cible.
    postes: list[str] = []
    for poste, n in catalog.COMPOSITION.items():
        postes.extend([poste] * n)
    # Ajuste si la composition ne tombe pas exactement sur nb_joueurs.
    while len(postes) < params.nb_joueurs:
        postes.append(catalog.MIL_CENTRAL)
    postes = postes[: params.nb_joueurs]

    # Noms uniques.
    noms = list(catalog.NOMS)
    prenoms = list(catalog.PRENOMS)
    rng.shuffle(noms)
    rng.shuffle(prenoms)

    aujourdhui = params.debut_saison
    joueurs: list[Joueur] = []
    for i, poste in enumerate(postes):
        famille = catalog.FAMILLE[poste]
        t_moy, t_ec, p_moy, p_ec = catalog.ANTHROPO[famille]

        taille = int(round(rng.normal(t_moy, t_ec)))
        taille = max(168, min(200, taille))

        # Poids de forme dérivé d'un IMC athlétique réaliste (21.5..24) → jamais en surpoids.
        imc = float(rng.uniform(21.5, 24.0))
        poids_forme = round(imc * (taille / 100.0) ** 2, 1)
        # Recentre légèrement vers la moyenne du poste pour rester crédible.
        poids_forme = round(0.6 * poids_forme + 0.4 * rng.normal(p_moy, p_ec), 1)

        age = _age_pour_poste(rng)
        naissance = date(aujourdhui.year - age, int(rng.integers(1, 13)), int(rng.integers(1, 28)))

        # Gardiens : moins de vitesse de pointe ; ailiers/attaquants : plus.
        base_vmax = {"gardien": 30.0, "axe": 32.0, "lateral": 33.5,
                     "milieu": 32.5, "ailier": 34.5, "attaquant": 34.0}[famille]
        vmax = round(float(rng.normal(base_vmax, 1.0)), 1)

        joueurs.append(Joueur(
            prenom=prenoms[i % len(prenoms)],
            nom=noms[i % len(noms)],
            poste=poste,
            famille=famille,
            date_naissance=naissance,
            taille_cm=taille,
            poids_forme_kg=poids_forme,
            pied_fort=catalog.PIEDS[int(rng.integers(0, len(catalog.PIEDS)))],
            profil_athletique=catalog.PROFILS_ATHLE[famille][
                int(rng.integers(0, len(catalog.PROFILS_ATHLE[famille])))
            ],
            forme_base=float(np.clip(rng.normal(0.6, 0.15), 0.2, 0.95)),
            fragilite=float(np.clip(rng.normal(0.3, 0.18), 0.02, 0.9)),
            serieux_saisie=float(np.clip(rng.normal(0.8, 0.15), 0.3, 1.0)),
            vitesse_max_kmh=vmax,
        ))

    return joueurs
