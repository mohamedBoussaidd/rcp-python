"""
Calendrier de la saison : déroule les micro-cycles hebdomadaires en une liste de
séances datées (entraînements + matchs), en respectant une trêve hivernale.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from . import catalog
from .config import ParametresSaison


@dataclass
class SeancePlan:
    date: date
    type_code: str             # REPRISE | TECHNIQUE | INTENSIF | PRE_MATCH | MATCH
    semaine: int               # index de semaine (1..nb_semaines)
    est_match: bool
    # Rempli après création côté backend
    backend_id: str | None = None
    # Contexte match (si est_match)
    adversaire: str | None = None
    domicile: bool | None = None
    score: str | None = None
    resultat: str | None = None  # V | N | D


ADVERSAIRES = [
    "FC Rivière", "AS Montagne", "Olympique Vallée", "US Littoral", "Étoile Sportive",
    "Racing Plaine", "Stade Forêt", "AC Colline", "Sporting Marais", "Union Coteaux",
    "FC Estuaire", "AS Bocage", "Olympique Lande", "US Garrigue", "Réveil Causse",
    "Élan Delta", "Stade Polder", "AC Maquis", "Sporting Steppe", "Union Toundra",
]


def construire_calendrier(params: ParametresSaison) -> list[SeancePlan]:
    """Génère toutes les séances de la saison (hors semaines de trêve)."""
    seances: list[SeancePlan] = []
    treve = set(
        range(params.semaine_treve_debut, params.semaine_treve_debut + params.nb_semaines_treve)
    )

    adv_idx = 0
    for s in range(1, params.nb_semaines + 1):
        if s in treve:
            continue
        lundi = params.debut_saison + timedelta(weeks=s - 1)
        # Pré-saison (2 premières semaines) : pas de match officiel, charge montante.
        pre_saison = s <= 2

        for delta, type_code in catalog.PROGRAMME_SEMAINE:
            if pre_saison and type_code == "MATCH":
                type_code = "INTENSIF"  # match amical remplacé par séance physique
            jour = lundi + timedelta(days=delta)
            est_match = type_code == "MATCH"

            plan = SeancePlan(date=jour, type_code=type_code, semaine=s, est_match=est_match)
            if est_match:
                plan.adversaire = ADVERSAIRES[adv_idx % len(ADVERSAIRES)]
                plan.domicile = (adv_idx % 2 == 0)
                adv_idx += 1
            seances.append(plan)

    return seances


def matchs(seances: list[SeancePlan]) -> list[SeancePlan]:
    return [s for s in seances if s.est_match]
