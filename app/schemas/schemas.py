from pydantic import BaseModel
from uuid import UUID
from typing import Optional


class Contribution(BaseModel):
    """
    Un facteur du score de risque, avec son poids. Permet au front d'afficher les 2 causes
    principales puis de replier le reste — sans avoir à parser la phrase explicative.
    """
    facteur: str            # charge | blessure | poids
    points: float           # points apportés au score (0-100)
    libelle: str            # fait mesuré, prêt à afficher


class SignalFatigue(BaseModel):
    """
    Un signal du score de fatigue. `fait` est la mesure (« vitesse max −12 % … »), `type_suggere`
    l'étiquette physiologique correspondante (« fatigue neuromusculaire explosive probable »),
    volontairement séparée pour que l'interface montre la mesure et relègue l'étiquette au détail.
    """
    facteur: str                            # charge_hebdo | performance_gps | monotonie | …
    points: float
    fait: str
    type_suggere: Optional[str] = None


class EcartSources(BaseModel):
    """
    Divergence entre charge mesurée (GPS) et charge ressentie (sRPE). L'ACWR mixte étant une
    moyenne pondérée, il annule cet écart — qui est pourtant l'information la plus utile.
    """
    ecart: float           # acwr_rpe − acwr_gps (signé)
    sens: str              # COHERENT | RESSENTI_SUP | MESURE_SUP
    libelle: str


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
    # Explicabilité : composition du score + décomposition de la charge (3 lectures de l'ACWR)
    contributions: list[Contribution] = []
    acwr: Optional[float] = None            # ratio retenu (selon `source`)
    acwr_gps: Optional[float] = None        # ratio sur la charge mesurée seule
    acwr_rpe: Optional[float] = None        # ratio sur la charge ressentie seule
    semaines_gps: Optional[int] = None      # longueur de référence réellement utilisée (GPS)
    semaines_rpe: Optional[int] = None      # idem pour le ressenti (fenêtres alignées)
    ecart_sources: Optional[EcartSources] = None
    provisoire: Optional[bool] = None       # baseline plus courte que la fenêtre cible


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
    semaines: Optional[int] = None            # semaines de données réellement disponibles
    semaines_requises: Optional[int] = None   # seuil de fiabilité (fenêtre chronique)
    provisoire: Optional[bool] = None         # True tant que semaines < semaines_requises
    phrase: str


class SimulationSeanceRequete(BaseModel):
    """
    Corps de la simulation « et si… » : une séance HYPOTHÉTIQUE à venir. `type_seance_id` sert à
    choisir la baseline de chaque joueur (m/min sur ce type de séance) ; `duree_minutes` la projette
    en distance attendue. Aucune séance n'est créée : c'est une projection, rien n'est écrit.
    """
    type_seance_id: Optional[UUID] = None   # None = baseline « toutes séances confondues »
    duree_minutes: int


class NiveauFatigue(BaseModel):
    joueur_id: UUID
    nom: str
    prenom: str
    score_fatigue: float  # 0-100
    niveau: str           # NOMINAL, VIGILANCE, ALERTE
    raison: str           # Explication lisible (conservée : phrase de repli)
    # Explicabilité : signaux triés par poids décroissant + sous-signaux GPS informatifs
    signaux: list[SignalFatigue] = []
    indicatifs: list[str] = []
    donnees: Optional[bool] = None  # False = aucune donnée de charge sur 28 j


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
    # Explicabilité : /etat-effectif et les dashboards n'avaient QUE le score et le niveau, donc
    # aucun moyen d'expliquer un chiffre. Les compositions arrivent maintenant avec la liste.
    contributions: list[Contribution] = []       # composition du score de risque
    signaux: list[SignalFatigue] = []            # composition du score de fatigue
    acwr_gps: Optional[float] = None
    acwr_rpe: Optional[float] = None
    semaines_gps: Optional[int] = None
    semaines_rpe: Optional[int] = None
    ecart_sources: Optional[EcartSources] = None
    provisoire: Optional[bool] = None
