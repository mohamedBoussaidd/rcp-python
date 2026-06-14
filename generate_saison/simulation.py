"""
Moteur de simulation physiologique de la saison.

Principe : on déroule la saison jour par jour, joueur par joueur. La charge d'une
séance dépend du type ET du poste ; elle alimente une charge aiguë (7 j) et
chronique (28 j) dont le ratio (ACWR) pilote le risque de blessure. Le ressenti
(wellness) et le RPE du lendemain découlent de cette charge et de la fraîcheur.
Tout est ainsi CAUSAL : pas de GPS élevé avec un wellness "tout va bien".

Sortie : un objet SaisonSimulee contenant l'effectif, le calendrier et toutes les
séries (GPS, wellness, RPE, pesées, blessures, conseils) prêtes à être poussées.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np

from . import catalog
from .calendrier import SeancePlan, construire_calendrier, matchs
from .config import ParametresSaison
from .profils import Joueur, generer_effectif


# ─────────────────────────── Structures de sortie ───────────────────────────

@dataclass
class GpsMesure:
    joueur: Joueur
    seance: SeancePlan
    duree_minutes: int
    distance_totale_m: float
    distance_15kmh_m: float
    distance_19kmh_m: float
    distance_sprint_24kmh_m: float
    distance_sprint_28kmh_m: float
    nb_sprints_24kmh: int
    vitesse_max_kmh: float
    nb_accelerations: int
    nb_freinages: int
    ratio_distance_min: float


@dataclass
class WellnessSaisie:
    joueur: Joueur
    date: date
    sommeil: int       # 1=bon .. 5=mauvais (convention uniforme)
    fatigue: int
    douleur: int       # courbatures
    stress: int
    humeur: int
    gene_zone: str | None = None
    gene_intensite: int | None = None


@dataclass
class RpeSaisie:
    joueur: Joueur
    seance: SeancePlan
    rpe: int           # 1..10 (Borg CR-10)
    duree_minutes: int
    type_rpe: str      # PHYSIQUE | TECHNIQUE


@dataclass
class PeseeSaisie:
    joueur: Joueur
    date: date
    poids_kg: float


@dataclass
class BlessureSaisie:
    joueur: Joueur
    debut: date
    fin: date
    type_blessure: str          # code DB : musculaire | ligamentaire | ...
    zone_corporelle: str        # code DB : ischio_jambiers | genou | ...
    libelle_humain: str         # libellé lisible (commentaire / conseil)
    gravite: str                # code DB : leger | modere | grave
    survenue_en_match: bool
    rtp_etapes: list[tuple[str, date]] = field(default_factory=list)


@dataclass
class ConseilSaisie:
    cible_joueur: Joueur | None    # None = conseil collectif (équipe)
    titre: str
    message: str
    date: date


@dataclass
class SaisonSimulee:
    params: ParametresSaison
    effectif: list[Joueur]
    seances: list[SeancePlan]
    gps: list[GpsMesure] = field(default_factory=list)
    wellness: list[WellnessSaisie] = field(default_factory=list)
    rpe: list[RpeSaisie] = field(default_factory=list)
    pesees: list[PeseeSaisie] = field(default_factory=list)
    blessures: list[BlessureSaisie] = field(default_factory=list)
    conseils: list[ConseilSaisie] = field(default_factory=list)

    def resume(self) -> str:
        return (
            f"Saison {self.params.annee_libelle()} — {len(self.effectif)} joueurs, "
            f"{len(self.seances)} séances ({len(matchs(self.seances))} matchs)\n"
            f"  GPS       : {len(self.gps)} lignes\n"
            f"  Wellness  : {len(self.wellness)} lignes\n"
            f"  RPE/sRPE  : {len(self.rpe)} lignes\n"
            f"  Pesées    : {len(self.pesees)} lignes\n"
            f"  Blessures : {len(self.blessures)} épisodes\n"
            f"  Conseils  : {len(self.conseils)} messages"
        )


# ─────────────────────────── Constantes du modèle ───────────────────────────

_ALPHA_AIGUE = 2 / (7 + 1)
_ALPHA_CHRONIQUE = 2 / (28 + 1)
_CHARGE_REF_MATCH = 600.0   # unité de charge arbitraire pour un match plein
_CHARGE_NORM = 650.0        # normalisation charge séance → 0..1 (courbatures)
_DOMS_REMANENCE = 0.45      # part de courbatures restant le lendemain (DOMS persistant 2-3 j)
_PROBA_BLESSURE_BASE = 0.0040  # par exposition, calibrée pour ~15-25/saison/effectif
_IMC_PLAFOND = 24.8            # garde-fou : aucun joueur affiché en surpoids


def _clamp_hooper(x: float) -> int:
    return int(max(1, min(5, round(x))))


class _EtatJoueur:
    """État physiologique courant d'un joueur, mis à jour jour par jour."""

    def __init__(self, joueur: Joueur):
        self.joueur = joueur
        # Charges initialisées à une base modérée pour éviter un ACWR délirant en début de saison.
        base = _CHARGE_REF_MATCH * 0.30
        self.charge_aigue = base
        self.charge_chronique = base
        self.indispo_jusqua: date | None = None
        # Courbatures (DOMS) "entrant" dans la journée, héritées des séances passées (0..1).
        self.courbatures = 0.0

    @property
    def acwr(self) -> float:
        if self.charge_chronique <= 1:
            return 1.0
        return self.charge_aigue / self.charge_chronique

    def applique_charge_jour(self, charge: float) -> None:
        self.charge_aigue = (1 - _ALPHA_AIGUE) * self.charge_aigue + _ALPHA_AIGUE * charge
        self.charge_chronique = (1 - _ALPHA_CHRONIQUE) * self.charge_chronique + _ALPHA_CHRONIQUE * charge


# ─────────────────────────── Simulation ───────────────────────────

def simuler(params: ParametresSaison) -> SaisonSimulee:
    rng = np.random.default_rng(params.seed + 1000)
    effectif = generer_effectif(params)
    seances = construire_calendrier(params)
    saison = SaisonSimulee(params=params, effectif=effectif, seances=seances)

    seances_par_date: dict[date, SeancePlan] = {s.date: s for s in seances}
    fin_saison = max(s.date for s in seances)
    debut_saison = min(s.date for s in seances)

    etats = {j.nom_complet: _EtatJoueur(j) for j in effectif}

    # Sélection des effectifs de match (qui joue, qui est sur le banc).
    feuilles_match = _composer_feuilles_match(seances, effectif, rng)

    # Boucle principale jour par jour.
    jour = debut_saison
    while jour <= fin_saison:
        seance = seances_par_date.get(jour)
        for j in effectif:
            etat = etats[j.nom_complet]
            charge_jour = 0.0
            blesse_aujourdhui = etat.indispo_jusqua is not None and jour <= etat.indispo_jusqua

            # 1) Ressenti du matin : reflète les courbatures héritées des séances passées.
            w = _generer_wellness(j, jour, etat, blesse_aujourdhui, rng)
            if w is not None:
                saison.wellness.append(w)

            # 2) Séance du jour : GPS, RPE, risque de blessure.
            session_norm = 0.0
            if seance is not None and not blesse_aujourdhui:
                minutes = _minutes_jouees(j, seance, feuilles_match, rng)
                if minutes > 0:
                    mesure, charge_jour = _generer_gps(j, seance, minutes, etat, rng)
                    saison.gps.append(mesure)
                    saison.rpe.append(_generer_rpe(j, seance, minutes, etat, rng))
                    session_norm = min(1.0, charge_jour / _CHARGE_NORM)

                    # Évaluation du risque de blessure sur cette exposition.
                    bless = _tirer_blessure(j, seance, etat, jour, fin_saison, rng)
                    if bless is not None:
                        saison.blessures.append(bless)
                        etat.indispo_jusqua = bless.fin

            # 3) Mise à jour : charge (ACWR) + courbatures transmises au lendemain.
            etat.applique_charge_jour(charge_jour)
            etat.courbatures = max(session_norm, etat.courbatures * _DOMS_REMANENCE)

        jour += timedelta(days=1)

    # Pesées hebdomadaires + conseils staff (post-traitement).
    saison.pesees = _generer_pesees(effectif, seances, params, rng)
    saison.conseils = _generer_conseils(saison, rng)
    return saison


# ─────────────────────────── Sélection des feuilles de match ───────────────────────────

def _composer_feuilles_match(seances, effectif, rng) -> dict[date, dict[str, int]]:
    """Pour chaque match : minutes jouées par joueur (0 = non retenu)."""
    feuilles: dict[date, dict[str, int]] = {}
    # Hiérarchie titulaires : pondérée par la forme de base + bruit par match (rotation).
    for s in seances:
        if not s.est_match:
            continue
        scores = {
            j.nom_complet: j.forme_base + float(rng.normal(0, 0.25))
            for j in effectif if j.poste != catalog.GARDIEN
        }
        gardiens = [j for j in effectif if j.poste == catalog.GARDIEN]
        classement = sorted(scores, key=scores.get, reverse=True)

        minutes: dict[str, int] = {}
        # 1 gardien titulaire (rotation légère).
        gk = gardiens[int(rng.integers(0, len(gardiens)))] if gardiens else None
        if gk:
            minutes[gk.nom_complet] = int(rng.integers(88, 97))

        titulaires = classement[:10]   # 10 joueurs de champ
        remplacants = classement[10:15]
        for nom in titulaires:
            minutes[nom] = int(rng.integers(70, 97))
        for nom in remplacants:
            if rng.random() < 0.7:     # tous les remplaçants n'entrent pas
                minutes[nom] = int(rng.integers(10, 35))
        feuilles[s.date] = minutes
    return feuilles


def _minutes_jouees(joueur, seance, feuilles_match, rng) -> int:
    if seance.est_match:
        return feuilles_match.get(seance.date, {}).get(joueur.nom_complet, 0)
    # Entraînement : quasi tout l'effectif, repos individuel occasionnel.
    if rng.random() < 0.05:
        return 0
    dmin, dmax = catalog.DUREE_MIN[seance.type_code]
    return int(rng.integers(dmin, dmax + 1))


# ─────────────────────────── Génération GPS ───────────────────────────

def _generer_gps(joueur, seance, minutes, etat, rng):
    fam = joueur.famille
    dmin, dmax = catalog.DIST_REFERENCE[seance.type_code]
    dist_ref = float(rng.uniform(dmin, dmax)) * catalog.MOD_DISTANCE[fam]

    # Pour un match/entraînement écourté, distance proportionnelle aux minutes.
    if seance.est_match:
        ref_minutes = 95.0
        dist_ref *= minutes / ref_minutes

    # Modulation par la forme (forme_base) et la fatigue (ACWR élevé → rendement ↓).
    facteur_forme = 0.9 + 0.2 * joueur.forme_base
    facteur_fraicheur = float(np.clip(1.1 - 0.12 * max(0.0, etat.acwr - 1.0), 0.8, 1.1))
    dist = round(dist_ref * facteur_forme * facteur_fraicheur, 1)

    # Zones d'intensité — part haute intensité modulée par le poste.
    mod_hi = catalog.MOD_HAUTE_INTENSITE[fam]
    d15 = round(dist * float(rng.uniform(0.38, 0.50)), 1)
    d19 = round(dist * float(rng.uniform(0.16, 0.26)) * mod_hi, 1)
    d24 = round(dist * float(rng.uniform(0.05, 0.12)) * mod_hi, 1)
    d28 = round(dist * float(rng.uniform(0.015, 0.05)) * mod_hi, 1)

    nb_sprint = max(0, int(d24 / float(rng.uniform(52, 82))))
    vmax = round(float(np.clip(rng.normal(joueur.vitesse_max_kmh, 0.8),
                               joueur.vitesse_max_kmh - 2, joueur.vitesse_max_kmh + 1.5)), 1)
    nb_acc = max(3, int(dist / float(rng.uniform(170, 270))))
    nb_fre = max(2, int(nb_acc * float(rng.uniform(0.65, 1.05))))
    ratio = round(dist / minutes, 2) if minutes else 0.0

    mesure = GpsMesure(
        joueur=joueur, seance=seance, duree_minutes=minutes,
        distance_totale_m=dist, distance_15kmh_m=d15, distance_19kmh_m=d19,
        distance_sprint_24kmh_m=d24, distance_sprint_28kmh_m=d28,
        nb_sprints_24kmh=nb_sprint, vitesse_max_kmh=vmax,
        nb_accelerations=nb_acc, nb_freinages=nb_fre, ratio_distance_min=ratio,
    )

    # Charge du jour (unité interne) : proportionnelle à distance × intensité relative.
    charge = (dist / 10000.0) * _CHARGE_REF_MATCH * (0.6 + 0.8 * catalog.CHARGE_RELATIVE[seance.type_code])
    return mesure, charge


# ─────────────────────────── Génération RPE / sRPE ───────────────────────────

def _generer_rpe(joueur, seance, minutes, etat, rng) -> RpeSaisie:
    base = {"MATCH": 8.4, "INTENSIF": 7.3, "TECHNIQUE": 5.0,
            "PRE_MATCH": 3.8, "REPRISE": 3.2}[seance.type_code]
    # Fatigue accumulée → effort perçu plus élevé pour un même travail.
    surcharge = 0.8 * max(0.0, etat.acwr - 1.0)
    rpe = base + surcharge + float(rng.normal(0, 0.6))
    rpe = int(max(1, min(10, round(rpe))))
    return RpeSaisie(
        joueur=joueur, seance=seance, rpe=rpe,
        duree_minutes=minutes, type_rpe=catalog.RPE_TYPE[seance.type_code],
    )


# ─────────────────────────── Génération wellness ───────────────────────────

def _generer_wellness(joueur, jour, etat, blesse, rng) -> WellnessSaisie | None:
    # Tous les joueurs ne saisissent pas tous les jours (régularité individuelle).
    if rng.random() > joueur.serieux_saisie:
        return None

    if blesse:
        # Joueur blessé : douleurs marquées, humeur/sommeil dégradés.
        return WellnessSaisie(
            joueur=joueur, date=jour,
            sommeil=_clamp_hooper(rng.normal(3.2, 0.7)),
            fatigue=_clamp_hooper(rng.normal(3.0, 0.7)),
            douleur=_clamp_hooper(rng.normal(4.2, 0.6)),
            stress=_clamp_hooper(rng.normal(3.3, 0.8)),
            humeur=_clamp_hooper(rng.normal(3.6, 0.8)),
            gene_zone="Zone blessée", gene_intensite=_clamp_hooper(rng.normal(4.0, 0.6)),
        )

    # Deux moteurs : courbatures du jour (DOMS, séances récentes) et fatigue de fond (ACWR).
    doms = etat.courbatures                                           # 0..1
    fatigue_idx = float(np.clip((etat.acwr - 0.7) / 1.3, 0.0, 1.0))   # 0..1

    # 1=bon .. 5=mauvais. Base bonne (~2), dégradée par DOMS et fatigue de fond.
    # La douleur (courbatures) suit surtout le DOMS → grosse séance la veille = douleur ↑.
    douleur = _clamp_hooper(rng.normal(1.6 + 2.8 * doms + 0.5 * fatigue_idx, 0.55))
    fatigue = _clamp_hooper(rng.normal(1.8 + 1.8 * doms + 1.4 * fatigue_idx, 0.55))
    sommeil = _clamp_hooper(rng.normal(2.0 + 0.9 * doms + 0.6 * fatigue_idx, 0.6))
    stress = _clamp_hooper(rng.normal(2.0 + 0.5 * doms + 0.6 * fatigue_idx, 0.7))
    humeur = _clamp_hooper(rng.normal(2.0 + 0.6 * doms + 0.7 * fatigue_idx, 0.7))

    # Gêne ponctuelle si courbatures fortes (signal pré-blessure éventuel).
    gene_zone = gene_int = None
    if douleur >= 4 and rng.random() < 0.25:
        gene_zone = rng.choice(["Cuisse", "Mollet", "Genou", "Cheville", "Dos"])
        gene_int = _clamp_hooper(rng.normal(3.0, 0.7))

    return WellnessSaisie(
        joueur=joueur, date=jour, sommeil=sommeil, fatigue=fatigue,
        douleur=douleur, stress=stress, humeur=humeur,
        gene_zone=gene_zone, gene_intensite=gene_int,
    )


# ─────────────────────────── Blessures ───────────────────────────

def _tirer_blessure(joueur, seance, etat, jour, fin_saison, rng) -> BlessureSaisie | None:
    # Probabilité par exposition, accrue par ACWR élevé, fragilité et exposition match.
    facteur_acwr = 1.0 + 2.5 * max(0.0, etat.acwr - 1.5)   # explose au-delà du seuil 1.5
    facteur_fragilite = 0.5 + 1.5 * joueur.fragilite
    facteur_expo = 1.8 if seance.est_match else 1.0
    p = _PROBA_BLESSURE_BASE * facteur_acwr * facteur_fragilite * facteur_expo
    if rng.random() > p:
        return None

    type_db, zone_db, libelle, jmin, jmax, gravite = catalog.BLESSURES_TYPES[
        int(rng.integers(0, len(catalog.BLESSURES_TYPES)))
    ]
    duree = int(rng.integers(jmin, jmax + 1))
    debut = jour + timedelta(days=1)
    fin = min(debut + timedelta(days=duree), fin_saison)

    # Étapes RTP réparties sur la durée d'indispo.
    etapes: list[tuple[str, date]] = []
    n = len(catalog.RTP_ETAPES)
    for i, nom in enumerate(catalog.RTP_ETAPES):
        d = debut + timedelta(days=int(round(duree * i / max(1, n - 1))))
        etapes.append((nom, min(d, fin)))

    return BlessureSaisie(
        joueur=joueur, debut=debut, fin=fin, type_blessure=type_db,
        zone_corporelle=zone_db, libelle_humain=libelle, gravite=gravite,
        survenue_en_match=seance.est_match, rtp_etapes=etapes,
    )


# ─────────────────────────── Pesées / IMC ───────────────────────────

def _generer_pesees(effectif, seances, params, rng) -> list[PeseeSaisie]:
    pesees: list[PeseeSaisie] = []
    debut = min(s.date for s in seances)
    fin = max(s.date for s in seances)

    for j in effectif:
        # Décalage de "méforme" individuel léger et constant.
        biais = float(rng.normal(0, 0.4))
        jour = debut
        semaine = 0
        while jour <= fin:
            # Pré-saison : poids un peu au-dessus de la forme, converge en ~4 semaines.
            if semaine < 4:
                offset = (1.6 - 0.4 * semaine)
            elif params.semaine_treve_debut <= semaine < params.semaine_treve_debut + 3:
                offset = 0.7   # léger relâchement à la trêve
            else:
                offset = 0.0
            poids = j.poids_forme_kg + offset + biais + float(rng.normal(0, 0.35))
            poids = min(poids, _IMC_PLAFOND * (j.taille_cm / 100.0) ** 2)  # jamais en surpoids
            pesees.append(PeseeSaisie(joueur=j, date=jour, poids_kg=round(poids, 1)))
            jour += timedelta(days=7)
            semaine += 1
    return pesees


# ─────────────────────────── Conseils staff ───────────────────────────

def _generer_conseils(saison, rng) -> list[ConseilSaisie]:
    conseils: list[ConseilSaisie] = []

    # Conseils individuels déclenchés sur les épisodes de blessure (prévention/reprise).
    for b in saison.blessures:
        conseils.append(ConseilSaisie(
            cible_joueur=b.joueur,
            titre="Protocole de reprise",
            message=(f"Suite à « {b.libelle_humain} » : respecte les étapes de réathlétisation, "
                     "hydratation et sommeil renforcés avant le retour collectif."),
            date=b.debut,
        ))

    # Quelques conseils collectifs jalonnant la saison.
    matchs_saison = matchs(saison.seances)
    jalons = matchs_saison[:: max(1, len(matchs_saison) // 6)][:6]
    messages = [
        ("Récupération post-match", "Privilégiez sommeil et nutrition dans les 24 h ; retour au calme actif demain."),
        ("Hydratation", "Pensez à l'hydratation avant/pendant/après l'effort, surtout en début de saison."),
        ("Gestion de charge", "Semaine chargée : écoutez vos sensations et signalez toute gêne via le wellness."),
        ("Prévention ischios", "Maintien des exercices excentriques (Nordic) en routine préventive."),
        ("Sommeil", "Visez 8 h de sommeil régulier — premier levier de récupération."),
        ("Avant trêve", "Maintenez une activité légère pendant la coupure pour limiter le déconditionnement."),
    ]
    for m, (titre, msg) in zip(jalons, messages):
        conseils.append(ConseilSaisie(cible_joueur=None, titre=titre, message=msg, date=m.date))

    return conseils
