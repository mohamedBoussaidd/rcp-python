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
    # Indicateurs préparateur (bruts)
    acwr: Optional[float] = None                 # ratio charge aiguë/chronique (Gabbett)
    charge_aigue_km: Optional[float] = None      # charge 7 derniers jours (km)
    charge_chronique_km: Optional[float] = None  # charge chronique hebdo (km)
    readiness: Optional[int] = None              # composite bien-être Hooper 0-100
    readiness_date: Optional[str] = None         # date de la dernière saisie wellness
    monotonie: Optional[float] = None            # indice de monotonie de Foster (8 sem.)
    sprint_niveau: Optional[str] = None          # None | POSSIBLE | PROBABLE (fatigue neuromusculaire)
    sprint_message: Optional[str] = None         # message d'orientation (non diagnostique)
