from pydantic import BaseModel
from uuid import UUID
from typing import Optional


class RisqueBlessure(BaseModel):
    joueur_id: UUID
    nom: str
    prenom: str
    score_risque: float  # 0-100
    niveau: str          # FAIBLE, MODERE, ELEVE
    # Chantier B — sortie probabiliste explicable (sans ML)
    probabilite: Optional[int] = None       # risque estimé à 7 jours (%)
    phrase: Optional[str] = None            # phrase explicative prête à afficher
    facteur_dominant: Optional[str] = None  # libellé du facteur le plus contributif
    tendance: Optional[str] = None          # HAUSSE | BAISSE | STABLE
    source: Optional[str] = None            # GPS | RPE | MIXTE | None (source de charge)
    # Contexte temporel (saison / période / fraîcheur)
    etat: Optional[str] = None              # EN_CHARGE|REPRISE|INACTIF|HORS_CHARGE|BLESSE
    periode_type: Optional[str] = None      # PREPARATION|COMPETITION|TREVE|REPRISE|INTERSAISON
    periode_libelle: Optional[str] = None
    jours_inactif: Optional[int] = None     # jours depuis la dernière donnée (None = jamais)


class ChargeCible(BaseModel):
    joueur_id: UUID
    disponible: bool
    source: Optional[str] = None       # GPS | RPE | MIXTE | None
    unite: Optional[str] = None        # km | sRPE
    chronique: Optional[float] = None  # charge chronique hebdo (référence)
    acwr_actuel: Optional[float] = None
    cible_min: Optional[float] = None
    cible_ideal: Optional[float] = None
    cible_haute: Optional[float] = None
    plafond: Optional[float] = None
    phrase: str


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
    # Contexte temporel (saison / période / fraîcheur des données)
    etat: Optional[str] = None                   # EN_CHARGE|REPRISE|INACTIF|HORS_CHARGE|BLESSE
    periode_type: Optional[str] = None           # PREPARATION|COMPETITION|TREVE|REPRISE|INTERSAISON
    periode_libelle: Optional[str] = None        # libellé lisible de la période courante
    jours_inactif: Optional[int] = None          # jours depuis la dernière donnée (None = jamais)
    blessure_jours_restants: Optional[int] = None  # jours avant retour prévu (négatif = dépassé)
