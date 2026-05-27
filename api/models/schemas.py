from pydantic import BaseModel
from uuid import UUID
from typing import Optional


class RisqueBlessure(BaseModel):
    joueur_id: UUID
    nom: str
    prenom: str
    score_risque: float  # 0-100
    niveau: str          # FAIBLE, MODERE, ELEVE


class NiveauFatigue(BaseModel):
    joueur_id: UUID
    nom: str
    prenom: str
    score_fatigue: float  # 0-100
    niveau: str           # FRAIS, FATIGUE, EPUISE
    raison: str           # Explication lisible du niveau de fatigue


class ResumeJoueur(BaseModel):
    joueur_id: UUID
    nom: str
    prenom: str
    poste: Optional[str]
    score_risque: float
    score_fatigue: float
    niveau_risque: str
    niveau_fatigue: str
