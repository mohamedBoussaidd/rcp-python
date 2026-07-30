from fastapi import APIRouter, HTTPException, Header
from uuid import UUID
from datetime import date as _date, timedelta as _timedelta
from app.core.database import get_connection
from app.schemas.schemas import (RisqueBlessure, NiveauFatigue, ResumeJoueur, ChargeCible,
                                 SimulationSeanceRequete)
from typing import List

router = APIRouter()

# Alias abréviations → clé canonique (insensible à la casse)
POSTE_ALIASES: dict[str, str] = {
    "g": "gardien", "gk": "gardien", "gd": "gardien", "goal": "gardien",
    "dc": "defenseur_central", "cb": "defenseur_central", "def": "defenseur_central",
    "lb": "lateral_gauche", "lg": "lateral_gauche",
    "rb": "lateral_droit", "ld": "lateral_droit",
    "md": "milieu_defensif", "mdc": "milieu_defensif",
    "cdm": "milieu_defensif", "dmc": "milieu_defensif", "mdeft": "milieu_defensif",
    "mc": "milieu_central", "cm": "milieu_central", "mf": "milieu_central",
    "mo": "milieu_offensif", "moff": "milieu_offensif", "cam": "milieu_offensif",
    "ag": "ailier_gauche", "aig": "ailier_gauche", "lw": "ailier_gauche",
    "ad": "ailier_droit", "aid": "ailier_droit", "rw": "ailier_droit",
    "att": "attaquant", "st": "attaquant", "fw": "attaquant",
    "ac": "avant_centre", "cf": "avant_centre", "9": "avant_centre",
}

# Correspondance code type → clé config pondération
POIDS_TYPE_KEY: dict[str, str] = {
    "MATCH":        "poids_match",
    "MATCH_AMICAL": "poids_match_amical",
    "INTENSIF":     "poids_intensif",
    "FORCE":        "poids_force",
    "TECHNIQUE":    "poids_technique",
    "PRE_MATCH":    "poids_pre_match",
    "REPRISE":      "poids_reprise",
}

# Correspondance poste → clé config objectif GPS
OBJECTIF_POSTE_KEY: dict[str, str] = {
    "gardien":            "objectif_gardien",
    "defenseur_central":  "objectif_defenseur_central",
    "lateral_droit":      "objectif_lateral_droit",
    "lateral_gauche":     "objectif_lateral_gauche",
    "milieu_defensif":    "objectif_milieu_defensif",
    "milieu_central":     "objectif_milieu_central",
    "milieu_offensif":    "objectif_milieu_offensif",
    "ailier_droit":       "objectif_ailier_droit",
    "ailier_gauche":      "objectif_ailier_gauche",
    "attaquant":          "objectif_attaquant",
    "avant_centre":       "objectif_avant_centre",
}

# Types de match (objectif GPS applicable)
TYPES_MATCH    = ("MATCH", "MATCH_AMICAL")
TYPES_INTENSIF = ("INTENSIF",)

# Séparateur entre le FAIT MESURÉ et l'ÉTIQUETTE physiologique dans les raisons de fatigue.
# `_calcul_fatigue` s'en sert pour découper chaque signal en deux champs exploitables par le
# front. ⚠ Les producteurs de raisons (`_signal2_detail`, `_calcul_signal3/4`, `_signal_wellness`,
# `_signal_srpe`, `_bonus_blessure`, `_bonus_congestion` et le signal 1) écrivent ce même littéral
# dans leurs f-strings : le modifier ici sans le modifier là-bas ne casse rien mais l'étiquette
# resterait collée au fait au lieu d'être isolée.
MARQUEUR_TYPE = " · type suggéré : "


def _normaliser_poste(poste: str) -> str:
    if not poste:
        return ""
    cle = poste.strip().lower()
    return POSTE_ALIASES.get(cle, cle)


def _load_config(conn) -> dict:
    """
    Charge les valeurs de configuration depuis la base.
    Si la table n'existe pas encore (migration non exécutée),
    retourne un dict vide — tous les cfg.get(key, défaut) utilisent
    alors leurs valeurs hardcodées, identiques à l'ancien comportement.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT cle, valeur FROM configuration")
            rows = cur.fetchall()
        return {row[0]: float(row[1]) for row in rows}
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return {}


# ════════════════════════════════════════════════════════════════════════════
# Contexte temporel CENTRALISÉ (saison / période / fraîcheur / blessure)
#
# Source UNIQUE de la règle « pas de données récentes ou hors-saison → pas
# d'alerte ». Tous les indicateurs s'appuient dessus au lieu de refaire chacun
# leur propre fenêtre temporelle. Tolérant aux migrations non passées (mode
# legacy : si les tables saison/effectif n'existent pas, periode_type reste None
# et seul le garde-fou de fraîcheur s'applique).
# ════════════════════════════════════════════════════════════════════════════

# Types de période où l'on N'ALERTE PAS (le joueur n'est pas censé être en charge).
_PERIODES_SILENCE = ("TREVE", "INTERSAISON")
# Types de période sans baseline stable → ACWR non alarmant (montée de charge attendue).
_PERIODES_NEUTRALISER_ACWR = ("PREPARATION", "REPRISE")


def _parse_date_simulee(valeur: str | None):
    """Parse l'en-tête X-Date-Simulee (yyyy-MM-dd) en date, ou None si absent/invalide.
    Outil de TEST : permet de se placer à une date arbitraire (préparation, trêve…)."""
    if not valeur:
        return None
    try:
        return _date.fromisoformat(valeur.strip()[:10])
    except Exception:
        return None


def _jours_depuis_derniere_donnee(joueur_id: UUID, conn, date_ref=None) -> int | None:
    """Jours écoulés depuis la dernière donnée (séance GPS ou RPE). None = jamais."""
    ref = date_ref or _date.today()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT MAX(d) FROM (
                    SELECT MAX(s.date) AS d
                      FROM donnee_gps dg JOIN seance s ON dg.seance_id = s.id
                      WHERE dg.joueur_id = %s AND dg.distance_totale_m > 0
                    UNION ALL
                    SELECT MAX(date) AS d
                      FROM rpe_seance WHERE joueur_id = %s AND charge IS NOT NULL
                ) t
            """, (str(joueur_id), str(joueur_id)))
            row = cur.fetchone()
    except Exception:
        try: conn.rollback()
        except Exception: pass
        return None
    if not row or row[0] is None:
        return None
    return (ref - row[0]).days


def _blessure_active(joueur_id: UUID, conn, date_ref=None) -> tuple:
    """(blessure_active: bool, jours_restants: int|None) — blessure non RETABLI la plus récente.
    jours_restants < 0 = date de retour prévue dépassée. Tolérant à l'absence de table."""
    ref = date_ref or _date.today()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT date_retour_prevue FROM blessure
                WHERE joueur_id = %s AND statut != 'RETABLI'
                ORDER BY date_blessure DESC LIMIT 1
            """, (str(joueur_id),))
            row = cur.fetchone()
    except Exception:
        try: conn.rollback()
        except Exception: pass
        return (False, None)
    if not row:
        return (False, None)
    drp = row[0]
    return (True, (drp - ref).days if drp else None)


def _contexte_joueur(joueur_id: UUID, cfg: dict, conn, date_ref=None) -> dict:
    """
    Contexte temporel d'un joueur : saison EN_COURS de son équipe, période courante,
    fraîcheur des données, blessure active → un ÉTAT exploitable par tous les calculs.
    `date_ref` (date simulée) permet de se placer à une autre date pour tester la
    temporalité (préparation, trêve…) ; défaut = aujourd'hui.

    États :
      EN_CHARGE   : suivi actif, alertes pleines
      REPRISE     : reprise post-trêve, ACWR neutralisé (baseline en reconstruction)
      INACTIF     : aucune donnée récente (> seuil) → indicateurs N/A, pas d'alerte
      HORS_CHARGE : trêve / intersaison → pas d'alerte
      HORS_SAISON : l'équipe utilise les saisons mais aucune n'est en cours → pas d'alerte
      BLESSE      : blessure active → pas d'alerte de charge (le joueur ne s'entraîne pas)
    """
    ref = date_ref or _date.today()
    saison_debut = None
    periode_type = None
    periode_libelle = None
    hors_saison = False

    try:
        with conn.cursor() as cur:
            # Saison au niveau CLUB (V37) : le club est porté directement par la fiche (j.club_id,
            # Phase 4 — plus de cache j.equipe_id). L'équipe COURANTE du joueur (pour la période)
            # est dérivée de son effectif dans la saison EN_COURS (effectif_saison).
            cur.execute("""
                SELECT
                  (SELECT es.equipe_id FROM effectif_saison es
                     JOIN saison s ON s.id = es.saison_id AND s.statut = 'EN_COURS'
                     WHERE es.joueur_id = j.id
                     ORDER BY es.date_entree DESC NULLS LAST LIMIT 1) AS equipe_id,
                  (SELECT s.date_debut FROM saison s
                     WHERE s.club_id = j.club_id AND s.statut = 'EN_COURS'
                     ORDER BY s.date_debut DESC LIMIT 1) AS encours_debut,
                  (SELECT s.id FROM saison s
                     WHERE s.club_id = j.club_id AND s.statut = 'EN_COURS'
                     ORDER BY s.date_debut DESC LIMIT 1) AS encours_id,
                  EXISTS (SELECT 1 FROM saison s WHERE s.club_id = j.club_id) AS a_saisons
                FROM joueur j WHERE j.id = %s
            """, (str(joueur_id),))
            row = cur.fetchone()
        if row:
            equipe_id, encours_debut, encours_id, a_saisons = row[0], row[1], row[2], row[3]
            if encours_id is not None:                    # une saison EN_COURS existe
                saison_debut = encours_debut
                with conn.cursor() as cur:
                    # Période courante de CETTE équipe dans la saison (clé saison_id + equipe_id).
                    cur.execute("""
                        SELECT type, libelle FROM periode_saison
                        WHERE saison_id = %s AND equipe_id = %s
                          AND %s::date BETWEEN date_debut AND date_fin
                        ORDER BY date_debut DESC LIMIT 1
                    """, (str(encours_id), str(equipe_id), ref))
                    pr = cur.fetchone()
                if pr:
                    periode_type, periode_libelle = pr[0], pr[1]
            elif bool(a_saisons):                         # des saisons existent mais aucune EN_COURS
                hors_saison = True
    except Exception:
        try: conn.rollback()
        except Exception: pass   # tables saison absentes → mode legacy

    jours_inactif = _jours_depuis_derniere_donnee(joueur_id, conn, ref)
    blessure_active, jours_restants = _blessure_active(joueur_id, conn, ref)
    seuil_inactif = int(cfg.get("jours_inactif_max", 10))

    if hors_saison:
        etat = "HORS_SAISON"
    elif periode_type in _PERIODES_SILENCE:
        etat = "HORS_CHARGE"
    elif blessure_active:
        etat = "BLESSE"
    elif jours_inactif is None or jours_inactif > seuil_inactif:
        etat = "INACTIF"
    elif periode_type in _PERIODES_NEUTRALISER_ACWR:
        etat = "REPRISE" if periode_type == "REPRISE" else "EN_CHARGE"
    else:
        etat = "EN_CHARGE"

    return {
        "etat": etat,
        "saison_debut": saison_debut,
        "periode_type": periode_type,
        "periode_libelle": periode_libelle,
        "jours_inactif": jours_inactif,
        "blessure_active": blessure_active,
        "blessure_jours_restants": jours_restants,
        # drapeaux dérivés (pratiques pour les appelants)
        "silence": etat in ("HORS_CHARGE", "HORS_SAISON", "INACTIF", "BLESSE"),
        "neutraliser_acwr": periode_type in _PERIODES_NEUTRALISER_ACWR,
    }


def _poids_seance(type_code: str, cfg: dict) -> float:
    key = POIDS_TYPE_KEY.get(type_code, "")
    return cfg.get(key, 0.60) if key else 0.60


def _lundi(d):
    """
    Lundi de la semaine ISO de `d` — équivalent Python de `date_trunc('week', …)` en SQL.
    Sert à compter les semaines RÉELLEMENT présentes dans une fenêtre de référence côté Python,
    quand la requête ramène déjà les lignes (inutile de refaire un COUNT en base).
    Tolère un `datetime` comme une `date`.
    """
    if hasattr(d, "date"):
        d = d.date()
    return d - _timedelta(days=d.weekday())


def _objectif_poste(poste: str, cfg: dict) -> float | None:
    key = OBJECTIF_POSTE_KEY.get(poste, "")
    return cfg.get(key) if key else None


def _poids_a_date(joueur_id: UUID, date_ref, conn) -> tuple:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT hp.poids, j.poids_forme_cible
            FROM historique_poids hp
            JOIN joueur j ON j.id = hp.joueur_id
            WHERE hp.joueur_id = %s AND hp.date <= %s
            ORDER BY hp.date DESC
            LIMIT 1
        """, (str(joueur_id), date_ref))
        row = cur.fetchone()

    if row:
        return (float(row[0]), float(row[1]) if row[1] is not None else None)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT poids_actuel, poids_forme_cible FROM joueur WHERE id = %s",
            (str(joueur_id),)
        )
        row = cur.fetchone()

    if row:
        return (float(row[0]) if row[0] is not None else None,
                float(row[1]) if row[1] is not None else None)
    return (None, None)


def _charge_gps(joueur_id: UUID, cfg: dict, conn, date_ref=None) -> tuple | None:
    """
    Charge externe (GPS) « découplée » — fenêtres NON chevauchantes :
      - aiguë           = SUM distances 7 derniers jours (mètres)
      - chronique hebdo = SUM distances jours 8-35 ÷ semaines RÉELLEMENT présentes (plafonné à
        `acwr_semaines_chronique`) — diviseur ADAPTATIF. Sans lui, un club à historique court
        (< 4 sem.) voit sa chronique divisée par 4 alors qu'il n'a qu'~1 semaine de données →
        ACWR faussement gonflé (~4) → risque sur-alarmé. Au régime établi (≥ cap sem.) le
        diviseur vaut cap : comportement identique à avant.
    `date_ref` permet de calculer à une date passée (tendance). Défaut = aujourd'hui.
    Renvoie (aigue_m, chronique_hebdo_m, semaines_reelles) ou None si pas de base chronique.
    Le 3e élément sert à signaler une baseline courte au front (« estimation provisoire ») ;
    les appelants qui n'en ont pas besoin indexent simplement [0] et [1].
    """
    ref = date_ref or _date.today()
    sem_chronique = int(cfg.get("acwr_semaines_chronique", 4))
    jours_chronique = 7 + sem_chronique * 7   # 35 jours pour 4 semaines
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT
                    SUM(CASE WHEN s.date >= %s::date - INTERVAL '7 days'
                             THEN dg.distance_totale_m ELSE 0 END) AS charge_aigue,
                    SUM(CASE WHEN s.date >= %s::date - INTERVAL '{jours_chronique} days'
                             AND s.date  < %s::date - INTERVAL '7 days'
                             THEN dg.distance_totale_m ELSE 0 END) AS chronique_somme,
                    COUNT(DISTINCT date_trunc('week', s.date))
                        FILTER (WHERE s.date >= %s::date - INTERVAL '{jours_chronique} days'
                                  AND s.date  < %s::date - INTERVAL '7 days') AS chronique_semaines
                FROM donnee_gps dg
                JOIN seance s ON dg.seance_id = s.id
                WHERE dg.joueur_id = %s
                  AND s.date >= %s::date - INTERVAL '{jours_chronique} days'
                  AND s.date <= %s::date
            """, (ref, ref, ref, ref, ref, str(joueur_id), ref, ref))
            row = cur.fetchone()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return None

    if not row or row[1] is None or float(row[1]) == 0 or not row[2]:
        return None
    diviseur = min(sem_chronique, max(1, int(row[2])))
    return (float(row[0] or 0), float(row[1]) / diviseur, diviseur)


def _charge_rpe(joueur_id: UUID, conn, date_ref=None, cfg: dict | None = None) -> tuple | None:
    """
    Charge interne (sRPE = RPE × durée, saisie joueur) « découplée » :
      - aiguë           = SUM charges 7 derniers jours
      - chronique hebdo = SUM charges de la fenêtre chronique ÷ semaines RÉELLEMENT présentes
        — diviseur ADAPTATIF, même raison que `_charge_gps` (pas de sous-estimation).
    Sert de source de repli quand le GPS manque (séances techniques, sans gilets).

    La fenêtre est ALIGNÉE sur celle du GPS (`acwr_semaines_chronique`, défaut 4) : elle était
    figée à 3 semaines, si bien qu'`acwr_gps` et `acwr_rpe` ne reposaient pas sur la même
    longueur de référence — une partie de leur écart n'était qu'un artefact de fenêtre, ce qui
    rendait leur comparaison côte à côte trompeuse.

    Renvoie (aigue, chronique_hebdo, semaines_reelles) ou None si pas de base chronique /
    table absente.
    """
    ref = date_ref or _date.today()
    sem_chronique = int((cfg or {}).get("acwr_semaines_chronique", 4))
    jours_chronique = 7 + sem_chronique * 7
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT
                    SUM(CASE WHEN date >= %s::date - INTERVAL '7 days'
                             THEN charge ELSE 0 END) AS aigue,
                    SUM(CASE WHEN date >= %s::date - INTERVAL '{jours_chronique} days'
                             AND date  < %s::date - INTERVAL '7 days'
                             THEN charge ELSE 0 END) AS chronique_somme,
                    COUNT(DISTINCT date_trunc('week', date))
                        FILTER (WHERE date >= %s::date - INTERVAL '{jours_chronique} days'
                                  AND date  < %s::date - INTERVAL '7 days') AS chronique_semaines
                FROM rpe_seance
                WHERE joueur_id = %s
                  AND date >= %s::date - INTERVAL '{jours_chronique} days'
                  AND date <= %s::date
                  AND charge IS NOT NULL
            """, (ref, ref, ref, ref, ref, str(joueur_id), ref, ref))
            row = cur.fetchone()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return None

    if not row or row[1] is None or float(row[1]) == 0 or not row[2]:
        return None
    diviseur = min(sem_chronique, max(1, int(row[2])))
    return (float(row[0] or 0), float(row[1]) / diviseur, diviseur)


def _charge_acwr_unifiee(joueur_id: UUID, cfg: dict, conn, date_ref=None) -> dict:
    """
    Source de charge UNIFIÉE avec repli (fallback) :
      - GPS présent seul          → ACWR sur les km (charge externe)
      - RPE présent seul           → ACWR sur la charge ressentie (repli)
      - les deux présents          → ACWR combiné pondéré (MIXTE)
      - aucune donnée              → source None

    GPS (externe) et RPE (interne) ne mesurent pas la même chose : on les combine
    via leurs ratios ACWR (sans dimension), pondérés (clés cfg `poids_charge_gps`
    / `poids_charge_rpe`). Les charges aiguë/chronique renvoyées sont en km si le
    GPS est disponible (source GPS ou MIXTE), sinon en unités sRPE.

    Renvoie : {source, acwr, aigue, chronique, unite, acwr_gps, acwr_rpe,
    semaines_gps, semaines_rpe} — les deux ratios isolés et le nombre de semaines de
    référence réellement utilisées de chaque côté sont RAPPORTÉS (et non plus jetés) :
    c'est l'écart entre GPS et ressenti qui informe, pas la seule moyenne pondérée qui
    l'annule. Cf. `_ecart_sources()`.
    """
    gps = _charge_gps(joueur_id, cfg, conn, date_ref)
    rpe = _charge_rpe(joueur_id, conn, date_ref, cfg)

    acwr_gps = (gps[0] / gps[1]) if gps and gps[1] > 0 else None
    acwr_rpe = (rpe[0] / rpe[1]) if rpe and rpe[1] > 0 else None
    sem_gps  = gps[2] if gps and len(gps) > 2 else None
    sem_rpe  = rpe[2] if rpe and len(rpe) > 2 else None

    vide = {"source": None, "acwr": None, "aigue": None, "chronique": None,
            "unite": None, "acwr_gps": None, "acwr_rpe": None,
            "semaines_gps": None, "semaines_rpe": None}

    if acwr_gps is not None and acwr_rpe is not None:
        w_g = float(cfg.get("poids_charge_gps", 0.6))
        w_r = float(cfg.get("poids_charge_rpe", 0.4))
        acwr = (w_g * acwr_gps + w_r * acwr_rpe) / (w_g + w_r)
        return {"source": "MIXTE", "acwr": round(acwr, 2),
                "aigue": round(gps[0] / 1000, 1), "chronique": round(gps[1] / 1000, 1),
                "unite": "km", "acwr_gps": round(acwr_gps, 2), "acwr_rpe": round(acwr_rpe, 2),
                "semaines_gps": sem_gps, "semaines_rpe": sem_rpe}
    if acwr_gps is not None:
        return {"source": "GPS", "acwr": round(acwr_gps, 2),
                "aigue": round(gps[0] / 1000, 1), "chronique": round(gps[1] / 1000, 1),
                "unite": "km", "acwr_gps": round(acwr_gps, 2), "acwr_rpe": None,
                "semaines_gps": sem_gps, "semaines_rpe": None}
    if acwr_rpe is not None:
        return {"source": "RPE", "acwr": round(acwr_rpe, 2),
                "aigue": round(rpe[0], 0), "chronique": round(rpe[1], 0),
                "unite": "sRPE", "acwr_gps": None, "acwr_rpe": round(acwr_rpe, 2),
                "semaines_gps": None, "semaines_rpe": sem_rpe}
    return vide


def _ecart_sources(charge: dict, cfg: dict) -> dict | None:
    """
    Divergence entre charge EXTERNE (GPS) et charge RESSENTIE (sRPE), quand les deux
    existent. L'ACWR mixte est une moyenne pondérée : elle ANNULE justement le cas le plus
    utile (« il en fait autant mais le vit mal »). On rapporte donc l'écart signé et une
    lecture prête à afficher. Seuil configurable `seuil_ecart_sources` (défaut 0.30).

    Renvoie None si une seule source, sinon {ecart, sens, libelle}.
    """
    a_gps, a_rpe = charge.get("acwr_gps"), charge.get("acwr_rpe")
    if a_gps is None or a_rpe is None:
        return None

    seuil = float(cfg.get("seuil_ecart_sources", 0.30))
    ecart = round(a_rpe - a_gps, 2)
    if abs(ecart) < seuil:
        return {"ecart": ecart, "sens": "COHERENT",
                "libelle": "charge mesurée et ressenti concordants"}
    if ecart > 0:
        return {"ecart": ecart, "sens": "RESSENTI_SUP",
                "libelle": "charge mesurée stable mais ressenti en hausse — "
                           "fatigue extra-sportive possible (sommeil, maladie, vie perso)"}
    return {"ecart": ecart, "sens": "MESURE_SUP",
            "libelle": "charge mesurée en hausse mais peu ressentie — "
                       "bonne tolérance, ou sous-déclaration du RPE"}


def _count_blessures_risque(joueur_id: UUID, conn, date_ref=None) -> int:
    """Nombre de blessures NON soldées (hors RETABLI) dans les 90 jours précédant la
    date de référence. Une blessure rétablie ne gonfle plus le risque indéfiniment."""
    ref = date_ref or _date.today()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) FROM blessure
                WHERE joueur_id = %s
                  AND statut != 'RETABLI'
                  AND date_blessure >= %s::date - INTERVAL '90 days'
                  AND date_blessure <= %s::date
            """, (str(joueur_id), ref, ref))
            row = cur.fetchone()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return 0
    return int(row[0]) if row else 0


def _calcul_score_risque(joueur_id: UUID, cfg: dict, conn, date_ref=None,
                         neutraliser_acwr: bool = False) -> dict:
    """
    Score de risque de blessure 0-100, fondé sur l'ACWR (Acute:Chronic Workload Ratio)
    « découplé » (Windt & Gabbett 2019) issu de la source UNIFIÉE GPS↔RPE (repli),
    majoré par les blessures récentes et le surpoids (corrections configurables).
    `date_ref` permet de recalculer le score à une date passée (tendance, chantier B).

    `neutraliser_acwr` (préparation / reprise) : pas de baseline stable → un ACWR élevé
    est ATTENDU et ne doit pas alarmer. On le rapporte pour information mais on plafonne
    sa contribution au niveau « charge maîtrisée ».

    Renvoie un dict : score, acwr, charges aiguë/chronique (km si GPS, None sinon),
    source/unite de la charge, et `contributions` (points par facteur + libellé)
    pour construire la phrase explicative et identifier le facteur dominant.
    """
    charge = _charge_acwr_unifiee(joueur_id, cfg, conn, date_ref)
    acwr   = charge["acwr"]

    if acwr is None:
        return {"score": 20.0, "acwr": None,
                "charge_aigue_km": None, "charge_chronique_km": None,
                "source": None, "unite": None, "contributions": []}

    contributions = []
    if neutraliser_acwr:
        # Montée de charge attendue : score neutre, on n'escalade pas sur l'ACWR.
        score_acwr = 20.0
        lib_acwr = f"montée de charge attendue (préparation/reprise) — ACWR {acwr} non alarmant"
    else:
        if acwr < 0.8:
            score_acwr = 15.0
        elif acwr <= 1.3:
            score_acwr = 20.0 + (acwr - 0.8) * 20
        else:
            score_acwr = 30.0 + min((acwr - 1.3) * 50, 50.0)

        pct_acwr = round((acwr - 1) * 100)
        src_txt = {"GPS": "charge", "RPE": "charge ressentie", "MIXTE": "charge"}.get(charge["source"], "charge")
        if acwr > 1.3:
            lib_acwr = f"{src_txt} aiguë +{pct_acwr}% au-dessus de l'habituel (ACWR {acwr})"
        elif acwr < 0.8:
            lib_acwr = f"sous-charge {pct_acwr}% vs habituel (ACWR {acwr})"
        else:
            lib_acwr = f"charge maîtrisée (ACWR {acwr})"
    contributions.append({"facteur": "charge", "points": round(score_acwr, 1), "libelle": lib_acwr})

    score = score_acwr

    blessures_recentes = _count_blessures_risque(joueur_id, conn, date_ref)
    if blessures_recentes > 0:
        pts = blessures_recentes * 15
        score += pts
        contributions.append({"facteur": "blessure", "points": float(pts),
                              "libelle": f"{blessures_recentes} blessure(s) récente(s) (<90 j)"})

    poids, poids_cible = _poids_a_date(joueur_id, date_ref or _date.today(), conn)
    if poids is not None and poids_cible is not None:
        ecart_kg = poids - poids_cible
        if ecart_kg > 0:
            pts_par_kg = cfg.get("correction_surpoids_pts_par_kg", 5.0)
            plafond    = cfg.get("correction_surpoids_plafond_pts", 20.0)
            pts = min(ecart_kg * pts_par_kg, plafond)
            score += pts
            contributions.append({"facteur": "poids", "points": round(pts, 1),
                                  "libelle": f"surpoids +{round(ecart_kg, 1)} kg vs poids de forme"})

    sem_gps = charge.get("semaines_gps")
    cap_gps = int(cfg.get("acwr_semaines_chronique", 4))

    return {
        "score":               min(round(score, 1), 100.0),
        "acwr":                acwr,
        "charge_aigue_km":     charge["aigue"] if charge["unite"] == "km" else None,
        "charge_chronique_km": charge["chronique"] if charge["unite"] == "km" else None,
        "source":              charge["source"],
        "unite":               charge["unite"],
        "contributions":       contributions,
        # Décomposition de la charge : les deux ratios isolés + leur fenêtre de référence,
        # pour que le front affiche les 3 lectures (mixte, GPS, ressenti) au lieu d'une seule.
        "acwr_gps":            charge.get("acwr_gps"),
        "acwr_rpe":            charge.get("acwr_rpe"),
        "semaines_gps":        sem_gps,
        "semaines_rpe":        charge.get("semaines_rpe"),
        "ecart_sources":       _ecart_sources(charge, cfg),
        # Baseline plus courte que le cap → le ratio est mathématiquement plus instable.
        "provisoire":          bool(sem_gps is not None and sem_gps < cap_gps),
    }


def _moyenne_hebdo_gps(joueur_id: UUID, conn, lundi, cap: int) -> tuple | None:
    """
    Baseline STABLE de la charge cible : volume GPS des `cap` dernières semaines COMPLÈTES
    (fenêtre [lundi − cap·7 j ; lundi[ — inclut la semaine passée, exclut la semaine en cours)
    ET le nombre de semaines réellement présentes (diviseur adaptatif). Ancrée au lundi : figée
    toute la semaine, recalculée chaque lundi (pas de dérive en cours de semaine). Volontairement
    distincte de la fenêtre « chronique découplée » de l'ACWR risque, qui exclut les 7 derniers
    jours et raterait un historique court.
    Renvoie (somme_mètres, semaines_présentes) ou None si aucune donnée dans la fenêtre.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT SUM(dg.distance_totale_m),
                       COUNT(DISTINCT date_trunc('week', s.date))
                FROM donnee_gps dg
                JOIN seance s ON dg.seance_id = s.id
                WHERE dg.joueur_id = %s
                  AND s.date >= %s::date - INTERVAL '{int(cap) * 7} days'
                  AND s.date <  %s::date
            """, (str(joueur_id), lundi, lundi))
            row = cur.fetchone()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return None
    if not row or row[0] is None or float(row[0]) == 0 or not row[1]:
        return None
    return (float(row[0]), int(row[1]))


def _moyenne_hebdo_rpe(joueur_id: UUID, conn, lundi, cap: int) -> tuple | None:
    """Repli sRPE de `_moyenne_hebdo_gps` (même fenêtre calendaire) quand le GPS manque."""
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT SUM(charge),
                       COUNT(DISTINCT date_trunc('week', date))
                FROM rpe_seance
                WHERE joueur_id = %s
                  AND charge IS NOT NULL
                  AND date >= %s::date - INTERVAL '{int(cap) * 7} days'
                  AND date <  %s::date
            """, (str(joueur_id), lundi, lundi))
            row = cur.fetchone()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return None
    if not row or row[0] is None or float(row[0]) == 0 or not row[1]:
        return None
    return (float(row[0]), int(row[1]))


def _charge_cible(joueur_id: UUID, cfg: dict, conn, date_ref=None) -> dict:
    """
    Recommandation de charge pour la SEMAINE DE TRAVAIL en cours (lundi→dimanche), individualisée.
    Baseline = moyenne hebdo des dernières semaines COMPLÈTES réellement présentes (diviseur
    ADAPTATIF `min(cap, semaines présentes)`, plafonné à `cap`), ANCRÉE AU LUNDI : l'objectif est
    figé toute la semaine et ne se recalcule que le lundi (pas de dérive en cours de semaine).
    Projetée par les bornes ACWR : sûre [0.8 ; 1.3], idéale ~1.05. Exprimée en km si GPS, sinon
    en unités sRPE (repli). Se fiabilise à partir de `cap` semaines ; en-deçà, estimation signalée.
    Renvoie {disponible, source, unite, ...} — disponible=False si aucune base de charge.
    """
    ref     = date_ref or _date.today()
    lundi   = ref - _timedelta(days=ref.weekday())   # lundi ISO de la semaine en cours
    cap_gps = int(cfg.get("acwr_semaines_chronique", 4))
    cap_rpe = 3

    gps = _moyenne_hebdo_gps(joueur_id, conn, lundi, cap_gps)
    if gps is not None:
        somme_m, semaines = gps
        source, unite, cap = "GPS", "km", cap_gps
        chro = round((somme_m / min(cap, max(1, semaines))) / 1000, 1)
    else:
        rpe = _moyenne_hebdo_rpe(joueur_id, conn, lundi, cap_rpe)
        if rpe is None:
            return {"disponible": False, "source": None, "unite": None,
                    "phrase": "Pas assez de données de charge pour recommander une cible."}
        somme, semaines = rpe
        source, unite, cap = "RPE", "sRPE", cap_rpe
        chro = round(somme / min(cap, max(1, semaines)))

    if chro <= 0:
        return {"disponible": False, "source": source, "unite": unite,
                "phrase": "Pas assez de données de charge pour recommander une cible."}

    acwr_min   = float(cfg.get("acwr_cible_min", 0.8))
    acwr_ideal = float(cfg.get("acwr_cible_ideal", 1.05))
    acwr_haute = float(cfg.get("acwr_cible_haute", 1.2))
    acwr_max   = float(cfg.get("acwr_cible_max", 1.3))
    arr = (lambda v: round(v, 1)) if unite == "km" else (lambda v: round(v))

    cible_min   = arr(chro * acwr_min)
    cible_ideal = arr(chro * acwr_ideal)
    cible_haute = arr(chro * acwr_haute)
    plafond     = arr(chro * acwr_max)

    provisoire = semaines < cap
    phrase = (f"Charge cible semaine : {cible_min}–{cible_haute} {unite} "
              f"(idéal ~{cible_ideal}). Plafond à ne pas dépasser : {plafond} {unite}.")
    if provisoire:
        phrase += (f" Estimation provisoire — basée sur {semaines} semaine"
                   f"{'s' if semaines > 1 else ''} de données (se fiabilise à {cap}).")
    return {
        "disponible":        True,
        "source":            source,
        "unite":             unite,
        "chronique":         chro,
        "cible_min":         cible_min,
        "cible_ideal":       cible_ideal,
        "cible_haute":       cible_haute,
        "plafond":           plafond,
        "semaines":          semaines,
        "semaines_requises": cap,
        "provisoire":        provisoire,
        "phrase":            phrase,
    }


def _baseline_ratio(joueur_id: UUID, type_seance_id, conn, recence_j: int) -> tuple:
    """
    Baseline personnelle en m/min : moyenne des 10 dernières séances RÉALISÉES du joueur, dans la
    fenêtre de récence. `type_seance_id=None` → toutes séances confondues (repli affichable quand
    le type est trop mince). Renvoie (ratio, n) — même définition que la « distance attendue » du
    rapport de séance (sujet 4), extraite ici pour être réutilisable hors d'une séance existante.
    """
    filtre_type = "AND s.type_seance_id = %s" if type_seance_id else ""
    params: list = [str(joueur_id)]
    if type_seance_id:
        params.append(str(type_seance_id))
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT AVG(sub.ratio), COUNT(*) FROM (
                SELECT dg.distance_totale_m / NULLIF(dg.duree_minutes, 0) AS ratio
                FROM donnee_gps dg
                JOIN seance s ON dg.seance_id = s.id
                WHERE dg.joueur_id = %s
                  {filtre_type}
                  AND s.statut = 'REALISEE'
                  AND s.date >= CURRENT_DATE - INTERVAL '{recence_j} days'
                  AND dg.duree_minutes > 0
                  AND dg.distance_totale_m > 0
                ORDER BY s.date DESC
                LIMIT 10
            ) sub
        """, params)
        r = cur.fetchone()
    return (float(r[0]) if r and r[0] is not None else None, int(r[1]) if r and r[1] else 0)


def _simuler_acwr(joueur_id: UUID, cfg: dict, conn, delta_m: float = 0.0) -> dict:
    """
    Recalcule l'ACWR du joueur EN AJOUTANT `delta_m` mètres à sa charge AIGUË (une séance à venir
    tombe dans la fenêtre aiguë ; la fenêtre chronique, elle, ne bouge pas — d'où un simple delta
    sur l'aiguë).

    N'ALTÈRE AUCUNE fonction existante : réutilise `_charge_gps` / `_charge_rpe` en lecture et
    refait la combinaison pondérée à l'identique de `_charge_acwr_unifiee`, sur les valeurs BRUTES
    (pas les valeurs arrondies renvoyées par celle-ci). Le score de risque officiel reste inchangé.

    Renvoie {source, acwr_avant, acwr_apres, aigue_avant_km, aigue_apres_km, chronique_km}.
    """
    gps = _charge_gps(joueur_id, cfg, conn)
    rpe = _charge_rpe(joueur_id, conn, None, cfg)
    w_g = float(cfg.get("poids_charge_gps", 0.6))
    w_r = float(cfg.get("poids_charge_rpe", 0.4))

    def _combine(add_m: float):
        a_gps = ((gps[0] + add_m) / gps[1]) if gps and gps[1] > 0 else None
        a_rpe = (rpe[0] / rpe[1]) if rpe and rpe[1] > 0 else None   # le sRPE de la séance est inconnu
        if a_gps is not None and a_rpe is not None:
            return (w_g * a_gps + w_r * a_rpe) / (w_g + w_r), "MIXTE"
        if a_gps is not None:
            return a_gps, "GPS"
        if a_rpe is not None:
            return a_rpe, "RPE"
        return None, None

    avant, source = _combine(0.0)
    apres, _      = _combine(delta_m)
    return {
        "source":         source,
        "acwr_avant":     round(avant, 2) if avant is not None else None,
        "acwr_apres":     round(apres, 2) if apres is not None else None,
        "aigue_avant_km": round(gps[0] / 1000, 1) if gps else None,
        "aigue_apres_km": round((gps[0] + delta_m) / 1000, 1) if gps else None,
        "chronique_km":   round(gps[1] / 1000, 1) if gps else None,
    }


def _zone_acwr(acwr, cfg: dict) -> str | None:
    """Zone lisible d'un ACWR selon les bornes configurées : SOUS_CHARGE / OPTIMALE / SURCHARGE."""
    if acwr is None:
        return None
    if acwr < float(cfg.get("acwr_cible_min", 0.8)):
        return "SOUS_CHARGE"
    if acwr > float(cfg.get("acwr_cible_max", 1.3)):
        return "SURCHARGE"
    return "OPTIMALE"


def _simulation_seance_data(conn, cfg, scope, type_seance_id, duree_minutes: int) -> dict:
    """
    Cœur du scénario « une séance » de la simulation. Pour chaque joueur de l'effectif :
    distance attendue (baseline m/min du même type × durée), ACWR avant/après ajout de cette
    distance, zone avant/après, et bascule éventuelle vers la surcharge.

    Purement en LECTURE : aucune séance n'est créée, aucune donnée n'est écrite.
    """
    recence_j = int(cfg.get("baseline_recence_jours", 90))
    duree = max(1, int(duree_minutes or 0))

    libelle_type = None
    if type_seance_id:
        with conn.cursor() as cur:
            cur.execute("SELECT libelle FROM type_seance WHERE id = %s", (str(type_seance_id),))
            row = cur.fetchone()
            libelle_type = row[0] if row else None

    joueurs = []
    for (jid, nom, prenom, poste) in _joueurs_resume(conn, scope):
        jid = str(jid)
        ratio, n = _baseline_ratio(jid, type_seance_id, conn, recence_j)
        origine = "TYPE"
        if (ratio is None or n < 3) and type_seance_id:
            # Type trop mince → repli explicite sur la baseline toutes séances confondues.
            ratio_g, n_g = _baseline_ratio(jid, None, conn, recence_j)
            if ratio_g is not None and n_g > n:
                ratio, n, origine = ratio_g, n_g, "GLOBALE"

        if ratio is None:
            joueurs.append({
                "joueur_id": jid, "nom": nom, "prenom": prenom, "poste": poste or "",
                "km_attendu": None, "baseline_n": 0, "baseline_origine": None,
                "acwr_avant": None, "acwr_apres": None,
                "zone_avant": None, "zone_apres": None, "bascule": False,
                "statut": "SANS_BASELINE",
            })
            continue

        attendu_m = ratio * duree
        sim = _simuler_acwr(jid, cfg, conn, delta_m=attendu_m)
        zone_avant = _zone_acwr(sim["acwr_avant"], cfg)
        zone_apres = _zone_acwr(sim["acwr_apres"], cfg)
        joueurs.append({
            "joueur_id": jid, "nom": nom, "prenom": prenom, "poste": poste or "",
            "km_attendu":      round(attendu_m / 1000, 1),
            "baseline_n":      n,
            "baseline_origine": origine,
            "acwr_avant":      sim["acwr_avant"],
            "acwr_apres":      sim["acwr_apres"],
            "aigue_avant_km":  sim["aigue_avant_km"],
            "aigue_apres_km":  sim["aigue_apres_km"],
            "chronique_km":    sim["chronique_km"],
            "zone_avant":      zone_avant,
            "zone_apres":      zone_apres,
            "bascule":         zone_avant != "SURCHARGE" and zone_apres == "SURCHARGE",
            "statut":          "OK" if n >= 3 else "PEU_FIABLE",
        })

    evalues     = [j for j in joueurs if j["acwr_apres"] is not None]
    nb_bascule  = sum(1 for j in evalues if j["bascule"])
    nb_sur_av   = sum(1 for j in evalues if j["zone_avant"] == "SURCHARGE")
    nb_sur_ap   = sum(1 for j in evalues if j["zone_apres"] == "SURCHARGE")
    km_moyen    = round(sum(j["km_attendu"] for j in evalues) / len(evalues), 1) if evalues else None
    peu_fiable  = [j for j in evalues if j["statut"] == "PEU_FIABLE"]

    return {
        "seance": {
            "type_seance_id": str(type_seance_id) if type_seance_id else None,
            "type_libelle":   libelle_type,
            "duree_minutes":  duree,
        },
        "synthese": {
            "nb_evalues":          len(evalues),
            "nb_sans_baseline":    len(joueurs) - len(evalues),
            "nb_surcharge_avant":  nb_sur_av,
            "nb_surcharge_apres":  nb_sur_ap,
            "nb_bascule":          nb_bascule,
            "km_attendu_moyen":    km_moyen,
            "nb_peu_fiable":       len(peu_fiable),
        },
        "joueurs": sorted(joueurs, key=lambda j: (j["acwr_apres"] is None, -(j["acwr_apres"] or 0))),
    }


def _signal2_detail(joueur_id: UUID, types: tuple, label_groupe: str,
                    cfg: dict, conn) -> tuple:
    """
    Signal 2 enrichi — 3 sous-signaux sur les dernières séances du groupe (≤ 60 jours) :
      A — m/min global          → fatigue générale
      B — vitesse max           → fatigue neuromusculaire explosive
      C — ratio dist >19 km/h   → fatigue neuromusculaire intensive

    Chaque sous-signal compare la moyenne de ses N séances les plus RÉCENTES à celle des séances
    plus anciennes du même groupe. N est réglable INDÉPENDAMMENT pour les trois
    (`nb_seances_recentes_intensite` / `_vmax` / `_hi`, défaut 2) : une vitesse de pointe jugée
    sur 2 séances est bien plus bruitée qu'une intensité moyenne, et le staff doit pouvoir
    arbitrer réactivité contre stabilité indicateur par indicateur. `nb_seances_reference_min`
    fixe le minimum de séances de comparaison exigé (défaut 2) — c'était un `len(rows) < 4` en
    dur, garde-fou unique pour les trois sous-signaux.
    Seuils lus depuis la configuration.
    """
    n_a     = max(1, int(cfg.get("nb_seances_recentes_intensite", 2)))
    n_b     = max(1, int(cfg.get("nb_seances_recentes_vmax",      2)))
    n_c     = max(1, int(cfg.get("nb_seances_recentes_hi",        2)))
    ref_min = max(1, int(cfg.get("nb_seances_reference_min",      2)))
    # Profondeur de référence conservée (8 séances au-delà des récentes = les 10 d'avant avec les
    # valeurs par défaut) : augmenter N ne doit pas rogner la base de comparaison.
    limite  = max(n_a, n_b, n_c) + 8

    with conn.cursor() as cur:
        cur.execute("""
            SELECT dg.distance_totale_m, dg.duree_minutes,
                   dg.vitesse_max_kmh, dg.distance_19kmh_m
            FROM donnee_gps dg
            JOIN seance s ON dg.seance_id = s.id
            JOIN type_seance ts ON s.type_seance_id = ts.id
            WHERE dg.joueur_id = %s
              AND ts.code = ANY(%s)
              AND dg.distance_totale_m > 0
              AND dg.duree_minutes > 0
              AND s.date >= CURRENT_DATE - INTERVAL '60 days'
            ORDER BY s.date DESC, dg.id DESC
            LIMIT %s
        """, (str(joueur_id), list(types), limite))
        rows = cur.fetchall()

    if not rows:
        return 0, None, []

    def _decoupe(valeurs: list, n: int) -> tuple | None:
        """
        Moyenne des `n` valeurs les plus récentes vs moyenne des plus anciennes.
        None si la base de comparaison est trop courte (< `ref_min`) : le sous-signal est alors
        simplement absent plutôt que calculé sur une référence d'une seule séance.
        """
        if len(valeurs) < n + ref_min:
            return None
        recent    = sum(valeurs[:n]) / n
        reference = sum(valeurs[n:]) / len(valeurs[n:])
        return (recent, reference) if reference > 0 else None

    sous_signaux = []

    s_mmin_prob = cfg.get("seuil_mmin_probable", 0.80)
    s_mmin_poss = cfg.get("seuil_mmin_possible", 0.88)
    s_vmax_prob = cfg.get("seuil_vmax_probable", 0.88)
    s_vmax_poss = cfg.get("seuil_vmax_possible", 0.94)
    s_hi_prob   = cfg.get("seuil_hi_probable",   0.75)
    s_hi_poss   = cfg.get("seuil_hi_possible",   0.85)

    # ── A : m/min global ──
    decoupe_a = _decoupe([float(r[0]) / float(r[1]) for r in rows], n_a)
    if decoupe_a:
        ra, ha  = decoupe_a
        ratio_a = ra / ha
        pct_a   = round((1 - ratio_a) * 100)
        if ratio_a <= s_mmin_prob:
            sc_a, type_a = 55, "fatigue générale probable"
        elif ratio_a <= s_mmin_poss:
            sc_a, type_a = 30, "fatigue générale possible"
        else:
            sc_a, type_a = 0, None
        sous_signaux.append({
            "score": sc_a, "type": type_a,
            "msg": f"intensité globale {'−'+str(pct_a)+'%' if pct_a > 0 else 'stable'} "
                   f"({round(ra,1)} m/min sur {n_a} séance{'s' if n_a > 1 else ''}, réf. {round(ha,1)})"
        })

    # ── B : vitesse max ──
    decoupe_b = _decoupe([float(r[2]) for r in rows if r[2] is not None], n_b)
    if decoupe_b:
        rb, hb  = decoupe_b
        ratio_b = rb / hb
        pct_b   = round((1 - ratio_b) * 100)
        if ratio_b <= s_vmax_prob:
            sc_b, type_b = 55, "fatigue neuromusculaire explosive probable"
        elif ratio_b <= s_vmax_poss:
            sc_b, type_b = 30, "fatigue neuromusculaire explosive possible"
        else:
            sc_b, type_b = 0, None
        sous_signaux.append({
            "score": sc_b, "type": type_b,
            "msg": f"vitesse max {'−'+str(pct_b)+'%' if pct_b > 0 else 'stable'} "
                   f"({round(rb,1)} km/h sur {n_b} séance{'s' if n_b > 1 else ''}, réf. {round(hb,1)})"
        })

    # ── C : ratio dist >19 km/h / distance totale ──
    decoupe_c = _decoupe(
        [float(r[3]) / float(r[0]) for r in rows if r[3] is not None and float(r[0]) > 0], n_c)
    if decoupe_c:
        rc, hc  = decoupe_c
        ratio_c = rc / hc
        pct_c   = round((1 - ratio_c) * 100)
        rc_pct  = round(rc * 100, 1)
        hc_pct  = round(hc * 100, 1)
        if ratio_c <= s_hi_prob:
            sc_c, type_c = 55, "fatigue neuromusculaire intensive probable"
        elif ratio_c <= s_hi_poss:
            sc_c, type_c = 30, "fatigue neuromusculaire intensive possible"
        else:
            sc_c, type_c = 0, None
        sous_signaux.append({
            "score": sc_c, "type": type_c,
            "msg": f"efforts >19 km/h {'−'+str(pct_c)+'%' if pct_c > 0 else 'stables'} "
                   f"({rc_pct}% sur {n_c} séance{'s' if n_c > 1 else ''} vs {hc_pct}% de la dist.)"
        })

    if not sous_signaux:
        return 0, None, []

    score_max = max(s["score"] for s in sous_signaux)

    if score_max == 0:
        return 0, None, sous_signaux

    principal = max(sous_signaux, key=lambda s: s["score"])
    autres    = [s for s in sous_signaux if s is not principal]

    raison_principale = (
        f"séances {label_groupe} — {principal['msg']}"
        + (f" · type suggéré : {principal['type']}" if principal["type"] else "")
    )

    return score_max, raison_principale, autres


def _calcul_signal3(joueur_id: UUID, cfg: dict, conn) -> tuple:
    """
    Signal 3 — Indice de monotonie Foster sur 8 semaines glissantes.
    Monotonie = moyenne(charges hebdo) / écart-type(charges hebdo)
    """
    today = _date.today()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT ts.code, dg.distance_totale_m, s.date
            FROM donnee_gps dg
            JOIN seance s ON dg.seance_id = s.id
            JOIN type_seance ts ON s.type_seance_id = ts.id
            WHERE dg.joueur_id = %s
              AND s.date >= CURRENT_DATE - INTERVAL '56 days'
              AND dg.distance_totale_m > 0
        """, (str(joueur_id),))
        rows = cur.fetchall()

    weekly_loads = [0.0] * 8
    for code, dist, session_date in rows:
        if hasattr(session_date, 'date'):
            session_date = session_date.date()
        days_ago = (today - session_date).days
        if 0 <= days_ago < 56:
            weekly_loads[days_ago // 7] += float(dist) * _poids_seance(code, cfg)

    if sum(1 for w in weekly_loads if w > 500) < 5:
        return 0, None

    mean_load  = sum(weekly_loads) / 8
    stdev_load = (sum((w - mean_load) ** 2 for w in weekly_loads) / 8) ** 0.5

    if mean_load < 1500:
        return 0, None

    monotonie = (mean_load / stdev_load) if stdev_load > 10 else 99.0
    km_moy    = round(mean_load / 1000, 1)

    seuil_alerte    = cfg.get("seuil_monotonie_alerte",    2.0)
    seuil_vigilance = cfg.get("seuil_monotonie_vigilance", 1.5)

    if monotonie > seuil_alerte:
        return (25,
            f"indice de monotonie {round(monotonie, 1)} — charge très uniforme sur 8 sem. "
            f"({km_moy} km pond./sem.) · type suggéré : surmenage chronique probable")
    elif monotonie > seuil_vigilance:
        return (15,
            f"indice de monotonie {round(monotonie, 1)} — rythme répétitif sur 8 sem. "
            f"({km_moy} km pond./sem.) · type suggéré : surmenage chronique possible")

    return 0, None


def _calcul_signal4(joueur_id: UUID, cfg: dict, conn) -> tuple:
    """
    Signal 4 — Espacement insuffisant entre séances haute intensité.
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT ts.code, s.date
            FROM donnee_gps dg
            JOIN seance s ON dg.seance_id = s.id
            JOIN type_seance ts ON s.type_seance_id = ts.id
            WHERE dg.joueur_id = %s
              AND ts.code = ANY(%s)
              AND s.date >= CURRENT_DATE - INTERVAL '28 days'
              AND dg.distance_totale_m > 0
            ORDER BY s.date ASC
        """, (str(joueur_id), ['MATCH', 'MATCH_AMICAL', 'INTENSIF']))
        rows_hi = cur.fetchall()

    with conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(DISTINCT s.date)
            FROM donnee_gps dg
            JOIN seance s ON dg.seance_id = s.id
            WHERE dg.joueur_id = %s
              AND s.date >= CURRENT_DATE - INTERVAL '14 days'
              AND dg.distance_totale_m > 0
        """, (str(joueur_id),))
        jours_seance_14j = int((cur.fetchone() or [0])[0])

    delai_mm = int(cfg.get("delai_match_match_jours",       3))
    delai_ii = int(cfg.get("delai_intensif_intensif_jours", 2))
    repos_min = int(cfg.get("repos_min_14_jours",           4))

    score   = 0
    raisons = []

    match_dates = [r[1] for r in rows_hi if r[0] in ('MATCH', 'MATCH_AMICAL')]
    for i in range(1, len(match_dates)):
        delta = (match_dates[i] - match_dates[i - 1]).days
        if delta < delai_mm:
            score += 25
            raisons.append(f"match-match en {delta}j")

    hi_dates = [r[1] for r in rows_hi if r[0] == 'INTENSIF']
    for i in range(1, len(hi_dates)):
        delta = (hi_dates[i] - hi_dates[i - 1]).days
        if delta < delai_ii:
            score += 15
            raisons.append(f"intensif-intensif en {delta}j")

    repos_14j = 14 - min(jours_seance_14j, 14)
    if repos_14j < repos_min:
        score += 20
        raisons.append(f"{repos_14j}j de repos sur 14j")

    score = min(score, 40)
    if score == 0:
        return 0, None

    libelle = "fatigue neuromusculaire " + ("probable" if score >= 25 else "possible")
    return score, f"récupération insuffisante — {' · '.join(raisons[:3])} · type suggéré : {libelle}"


def _signal_wellness(joueur_id: UUID, cfg: dict, conn) -> tuple:
    """
    Signal wellness — ressenti subjectif récent (indice de Hooper, saisie joueur).
    Score de bien-être 0..100 (items négatifs inversés ; plus haut = mieux) calculé
    sur la dernière saisie (≤ 3 jours). Un score bas augmente la fatigue.
    Renvoie (0, None) si pas de saisie récente ou si la table n'existe pas encore.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT sommeil, fatigue, douleur, stress, humeur
                FROM wellness_quotidien
                WHERE joueur_id = %s
                  AND date >= CURRENT_DATE - INTERVAL '3 days'
                ORDER BY date DESC
                LIMIT 1
            """, (str(joueur_id),))
            row = cur.fetchone()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return 0, None

    if not row:
        return 0, None

    sommeil, fatigue_i, douleur, stress, humeur = (int(v) for v in row)
    # Échelle de saisie : 1 = excellent → 10 = très mauvais pour TOUS les items.
    # Composite bien-être 0..100 (plus haut = mieux) : on inverse les 5 items.
    composite = round(((11 - sommeil) + (11 - humeur) + (11 - fatigue_i) + (11 - douleur) + (11 - stress)) / 5 * 10)

    # Items dégradés à signaler (convention uniforme : haut = mauvais pour les 5 items).
    soucis = []
    if fatigue_i >= 8: soucis.append("fatigue élevée")
    if douleur >= 8:   soucis.append("courbatures")
    if stress >= 8:    soucis.append("stress")
    if sommeil >= 8:   soucis.append("sommeil dégradé")
    if humeur >= 8:    soucis.append("humeur basse")
    detail = (" — " + ", ".join(soucis)) if soucis else ""

    seuil_alerte    = cfg.get("seuil_wellness_alerte",    40)
    seuil_vigilance = cfg.get("seuil_wellness_vigilance", 55)

    if composite < seuil_alerte:
        return 25, (f"ressenti dégradé (bien-être {composite}/100{detail})"
                    f" · type suggéré : fatigue subjective probable")
    elif composite < seuil_vigilance:
        return 12, (f"ressenti à surveiller (bien-être {composite}/100{detail})"
                    f" · type suggéré : fatigue subjective possible")
    return 0, None


def _signal_srpe(joueur_id: UUID, cfg: dict, conn) -> tuple:
    """
    Signal sRPE — charge subjective (RPE × durée) saisie par le joueur.
    ACWR sur la charge ressentie : aiguë (7 j) vs chronique hebdomadaire.
    Complète la charge GPS (utile notamment pour les séances sans GPS, ex. techniques).

    Le calcul de charge est DÉLÉGUÉ à `_charge_rpe`, qui applique la fenêtre configurée
    (`acwr_semaines_chronique`) et le diviseur ADAPTATIF. Ce signal refaisait auparavant sa
    propre requête, avec une fenêtre figée à 28 jours et un diviseur figé à 3 : sur un
    historique court il gonflait mécaniquement le ratio, et il pouvait contredire la carte
    ACWR ressentie du même joueur. Une seule source de vérité désormais.

    Renvoie (0, None) si données insuffisantes ou table absente.
    """
    charge = _charge_rpe(joueur_id, conn, None, cfg)
    if not charge or charge[1] <= 0 or charge[0] <= 0:
        return 0, None

    aigue, chronique, semaines = charge[0], charge[1], charge[2]
    ratio = aigue / chronique
    pct   = round((ratio - 1) * 100)
    seuil_prob = cfg.get("seuil_srpe_probable", 1.50)
    seuil_poss = cfg.get("seuil_srpe_possible", 1.30)
    cap     = int(cfg.get("acwr_semaines_chronique", 4))
    ref_txt = f" (réf. {semaines} sem.)" if semaines < cap else ""

    if ratio >= seuil_prob:
        return 25, (f"charge ressentie (sRPE) +{pct}% vs habituel{ref_txt}"
                    f" · type suggéré : surcharge subjective probable")
    elif ratio >= seuil_poss:
        return 12, (f"charge ressentie (sRPE) élevée +{pct}%{ref_txt}"
                    f" · type suggéré : surcharge subjective possible")
    return 0, None


def _bonus_blessure(joueur_id: UUID, cfg: dict, conn) -> tuple:
    """Bonus si blessure NON soldée récente — fenêtre et score configurables.
    Les blessures RETABLI sont exclues : une blessure rétablie ne doit pas maintenir
    une alerte de fatigue pendant des semaines après le retour du joueur."""
    fenetre = int(cfg.get("fenetre_blessure_fatigue_jours", 56))
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT COUNT(*)
            FROM blessure
            WHERE joueur_id = %s
              AND statut != 'RETABLI'
              AND date_blessure >= CURRENT_DATE - INTERVAL '{fenetre} days'
        """, (str(joueur_id),))
        row = cur.fetchone()

    nb = int(row[0]) if row else 0
    if nb == 0:
        return 0, None

    pts = int(cfg.get("bonus_blessure_pts", 20))
    return pts, f"{nb} blessure(s) récente(s) (<{fenetre//7} sem.) · type suggéré : risque de rechute"


def _bonus_congestion(joueur_id: UUID, cfg: dict, conn) -> tuple:
    """Bonus si congestion de matchs — seuils configurables."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*)
            FROM donnee_gps dg
            JOIN seance s ON dg.seance_id = s.id
            JOIN type_seance ts ON s.type_seance_id = ts.id
            WHERE dg.joueur_id = %s
              AND ts.code = ANY(%s)
              AND s.date >= CURRENT_DATE - INTERVAL '15 days'
              AND dg.distance_totale_m > 0
        """, (str(joueur_id), ['MATCH', 'MATCH_AMICAL']))
        row = cur.fetchone()

    nb        = int(row[0]) if row else 0
    seuil_prob = int(cfg.get("seuil_congestion_probable", 4))
    seuil_poss = int(cfg.get("seuil_congestion_possible", 3))

    if nb >= seuil_prob:
        return 20, f"{nb} matchs en 15j · type suggéré : fatigue cumulative probable"
    elif nb >= seuil_poss:
        return 10, f"{nb} matchs en 15j · type suggéré : fatigue cumulative possible"
    return 0, None


def _calcul_fatigue(joueur_id: UUID, cfg: dict, conn) -> dict:
    """
    Signal 1 — Charge hebdomadaire pondérée vs semaine normale
    Signal 2 — Baisse de performance GPS sur MATCH/INTENSIF
    Signal 3 — Indice de monotonie Foster (8 semaines)
    Signal 4 — Espacement insuffisant entre séances haute intensité
    Bonus  B — Blessure récente
    Bonus  C — Congestion de matchs
    Tous les seuils sont lus depuis la configuration.
    """
    # ── Signal 1 ──
    # Fenêtre de référence ALIGNÉE sur celle de l'ACWR (`acwr_semaines_chronique`) : elle était
    # figée à 21 jours ici et vaut 4 semaines là-bas, si bien que la carte ACWR et ce signal
    # parlaient de « la semaine normale » sur deux périodes différentes — et pouvaient donc se
    # contredire à l'écran pour le même joueur.
    sem_ref   = int(cfg.get("acwr_semaines_chronique", 4))
    jours_ref = 7 + sem_ref * 7
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT ts.code, dg.distance_totale_m, s.date,
                   s.date >= CURRENT_DATE - INTERVAL '7 days' AS est_recent
            FROM donnee_gps dg
            JOIN seance s ON dg.seance_id = s.id
            JOIN type_seance ts ON s.type_seance_id = ts.id
            WHERE dg.joueur_id = %s
              AND s.date >= CURRENT_DATE - INTERVAL '{jours_ref} days'
              AND dg.distance_totale_m > 0
        """, (str(joueur_id),))
        rows_charge = cur.fetchall()

    lignes_ref = [r for r in rows_charge if not r[3]]
    charge_7j  = sum(float(r[1]) * _poids_seance(r[0], cfg) for r in rows_charge if r[3])
    charge_ref = sum(float(r[1]) * _poids_seance(r[0], cfg) for r in lignes_ref)

    # Diviseur ADAPTATIF — semaines RÉELLEMENT présentes dans la fenêtre de référence, plafonnées
    # au cap configuré. Il était figé à 3 : un historique court (reprise, club neuf, joueur qui
    # vient d'arriver) voyait sa « semaine normale » divisée par 3 alors qu'il n'avait qu'une
    # semaine de données, et la semaine en cours paraissait 3× plus chargée qu'elle ne l'était
    # (+246 % affiché pour +15 % réels). Même règle que `_charge_gps` / `_charge_rpe` :
    # ⚠ ces trois endroits calculent une charge hebdomadaire de référence et doivent être
    # corrigés ENSEMBLE — la correction de `_charge_gps` avait justement oublié celui-ci.
    # On ne fusionne pas pour autant avec `_charge_gps` : ici les distances sont PONDÉRÉES par
    # type de séance (`_poids_seance`), ce que l'ACWR ne fait pas.
    semaines_ref = len({_lundi(r[2]) for r in lignes_ref})
    diviseur     = min(sem_ref, max(1, semaines_ref))
    charge_chrono_hebdo = charge_ref / diviseur if charge_ref > 0 else None

    s1_score  = 0
    s1_raison = None
    seuil_prob = cfg.get("seuil_surcharge_probable", 1.40)
    seuil_poss = cfg.get("seuil_surcharge_possible", 1.20)

    if charge_chrono_hebdo and charge_chrono_hebdo > 0 and charge_7j > 0:
        ratio_charge = charge_7j / charge_chrono_hebdo
        pct          = round((ratio_charge - 1) * 100)
        km_7j        = round(charge_7j / 1000, 1)
        km_normal    = round(charge_chrono_hebdo / 1000, 1)
        # La profondeur réellement disponible est annoncée : une référence d'une seule semaine
        # reste un repère fragile, autant que le staff le voie plutôt que de le deviner.
        ref_txt = f", réf. {diviseur} sem." if diviseur < sem_ref else ""
        if ratio_charge >= seuil_prob:
            s1_score  = 45
            s1_raison = (
                f"surcharge hebdomadaire +{pct}% ({km_7j} km pondérés vs {km_normal} km normal{ref_txt})"
                f" · type suggéré : surcharge métabolique probable"
            )
        elif ratio_charge >= seuil_poss:
            s1_score  = 25
            s1_raison = (
                f"charge hebdomadaire élevée +{pct}% ({km_7j} km pondérés vs {km_normal} km normal{ref_txt})"
                f" · type suggéré : surcharge métabolique possible"
            )

    # ── Signal 2 ──
    s2_sc_m, s2_ra_m, s2_det_m = _signal2_detail(joueur_id, TYPES_MATCH,    "de match",   cfg, conn)
    s2_sc_i, s2_ra_i, s2_det_i = _signal2_detail(joueur_id, TYPES_INTENSIF, "intensives", cfg, conn)

    if s2_sc_m >= s2_sc_i:
        s2_score, s2_raison, s2_details = s2_sc_m, s2_ra_m, s2_det_m
    else:
        s2_score, s2_raison, s2_details = s2_sc_i, s2_ra_i, s2_det_i

    # ── Signal 3 ──
    s3_score, s3_raison = _calcul_signal3(joueur_id, cfg, conn)

    # ── Signal 4 ──
    s4_score, s4_raison = _calcul_signal4(joueur_id, cfg, conn)

    # ── Signal wellness (ressenti subjectif) ──
    w_score, w_raison = _signal_wellness(joueur_id, cfg, conn)

    # ── Signal sRPE (charge ressentie) ──
    sr_score, sr_raison = _signal_srpe(joueur_id, cfg, conn)

    # ── Bonus blessure ──
    b_score, b_raison = _bonus_blessure(joueur_id, cfg, conn)

    # ── Bonus congestion ──
    c_score, c_raison = _bonus_congestion(joueur_id, cfg, conn)

    score = min(s1_score + s2_score + s3_score + s4_score + w_score + sr_score + b_score + c_score, 100.0)

    # ── Signaux structurés ──
    # Chaque signal expose son POIDS et deux textes séparés : le FAIT MESURÉ (« vitesse max −12 %
    # (28,4 km/h, réf. 32,3) ») et l'ÉTIQUETTE physiologique suggérée (« fatigue neuromusculaire
    # explosive probable »). Le front met le fait en avant et relègue l'étiquette au détail, avec
    # lien vers la méthodologie : le vocabulaire scientifique reste juste sans parler en premier.
    # Auparavant tout était concaténé dans `raison`, ce qui obligeait à parser une phrase française.
    brut = [
        ("charge_hebdo",     s1_score, s1_raison),
        ("performance_gps",  s2_score, s2_raison),
        ("monotonie",        s3_score, s3_raison),
        ("recuperation",     s4_score, s4_raison),
        ("ressenti",         w_score,  w_raison),
        ("charge_ressentie", sr_score, sr_raison),
        ("blessure",         b_score,  b_raison),
        ("congestion",       c_score,  c_raison),
    ]
    signaux = []
    for facteur, pts, texte in brut:
        if not texte:
            continue
        fait, _, type_suggere = texte.partition(MARQUEUR_TYPE)
        signaux.append({
            "facteur":      facteur,
            "points":       float(pts),
            "fait":         fait.strip(" ·"),
            "type_suggere": type_suggere.strip() or None,
        })
    signaux.sort(key=lambda s: s["points"], reverse=True)

    # ── Message ──
    parties = [r for r in [s1_raison, s2_raison, s3_raison, s4_raison, w_raison, sr_raison, b_raison, c_raison] if r]
    indicatifs = [s["msg"] for s in s2_details if s.get("msg")]

    if parties:
        raison = "Détecté : " + " · ".join(parties) + "."
        if indicatifs:
            raison += " À titre indicatif — " + " · ".join(indicatifs) + "."
    elif not rows_charge:
        raison = "Données insuffisantes pour l'analyse."
    else:
        raison = "Charge normale, aucune baisse de performance détectée."
        if indicatifs:
            raison += " Indicateurs — " + " · ".join(indicatifs) + "."

    return {
        "score":      round(score, 1),
        "niveau":     _niveau_fatigue(score),
        "raison":     raison,
        "signaux":    signaux,
        "indicatifs": indicatifs,
        "donnees":    bool(rows_charge),
    }


def _readiness_joueur(joueur_id: UUID, conn) -> tuple:
    """
    Readiness = dernier composite de bien-être (indice de Hooper, saisie joueur),
    0..100, plus haut = mieux. Fenêtre de 7 jours pour rester informatif sur le
    dashboard. Renvoie (composite|None, date_iso|None).
    """
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT sommeil, fatigue, douleur, stress, humeur, date
                FROM wellness_quotidien
                WHERE joueur_id = %s
                  AND date >= CURRENT_DATE - INTERVAL '7 days'
                ORDER BY date DESC
                LIMIT 1
            """, (str(joueur_id),))
            row = cur.fetchone()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return None, None

    if not row:
        return None, None

    sommeil, fatigue_i, douleur, stress, humeur = (int(v) for v in row[:5])
    # Tous les items 1=excellent..10=très mauvais → inversés pour « plus haut = mieux ».
    composite = round(((11 - sommeil) + (11 - humeur) + (11 - fatigue_i) + (11 - douleur) + (11 - stress)) / 5 * 10)
    return composite, str(row[5])


def _monotonie_joueur(joueur_id: UUID, cfg: dict, conn) -> float | None:
    """
    Indice de monotonie de Foster (8 semaines glissantes) — valeur brute.
    Monotonie = moyenne(charges hebdo pondérées) / écart-type(charges hebdo).
    Renvoie None si données insuffisantes. Isolé du scoring de fatigue.
    """
    today = _date.today()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT ts.code, dg.distance_totale_m, s.date
                FROM donnee_gps dg
                JOIN seance s ON dg.seance_id = s.id
                JOIN type_seance ts ON s.type_seance_id = ts.id
                WHERE dg.joueur_id = %s
                  AND s.date >= CURRENT_DATE - INTERVAL '56 days'
                  AND dg.distance_totale_m > 0
            """, (str(joueur_id),))
            rows = cur.fetchall()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return None

    weekly_loads = [0.0] * 8
    for code, dist, session_date in rows:
        if hasattr(session_date, 'date'):
            session_date = session_date.date()
        days_ago = (today - session_date).days
        if 0 <= days_ago < 56:
            weekly_loads[days_ago // 7] += float(dist) * _poids_seance(code, cfg)

    if sum(1 for w in weekly_loads if w > 500) < 5:
        return None

    mean_load = sum(weekly_loads) / 8
    if mean_load < 1500:
        return None

    stdev_load = (sum((w - mean_load) ** 2 for w in weekly_loads) / 8) ** 0.5
    if stdev_load <= 10:
        return 99.0
    return round(mean_load / stdev_load, 1)


def _sprint_neuromusculaire(joueur_id: UUID, cfg: dict, conn) -> dict:
    """
    Marqueur neuromusculaire orienté (NON diagnostique).

    Le marqueur FIABLE de fatigue nerveuse est la perte de CAPACITÉ à atteindre
    la vitesse de pointe — pas le volume de sprint (qui dépend surtout du format
    de séance). On raisonne donc en PIC sur une fenêtre, pas séance à séance :
      - vmax : pic des 7 derniers jours vs pic de la baseline (~4 sem., j8-35).
        → robuste : une journée « technique » à basse vitesse ne déclenche rien
          tant que le joueur a touché sa pointe une fois dans la semaine.
      - distance > 28 km/h / min : sert UNIQUEMENT de confirmation, jamais de
        déclencheur seul (un faible volume = souvent pas de sprint au programme).

    Déclenche seulement si la vmax de pointe baisse. La baisse du volume >28 km/h
    ne fait que renforcer (POSSIBLE → PROBABLE). Sur séances MATCH / INTENSIF.
    On ne localise PAS de muscle (le GPS ne le permet pas) : message d'orientation.
    Renvoie {niveau: None|'POSSIBLE'|'PROBABLE', message: str|None}.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT s.date, dg.duree_minutes, dg.vitesse_max_kmh, dg.distance_sprint_28kmh_m
                FROM donnee_gps dg
                JOIN seance s ON dg.seance_id = s.id
                JOIN type_seance ts ON s.type_seance_id = ts.id
                WHERE dg.joueur_id = %s
                  AND ts.code = ANY(%s)
                  AND s.date >= CURRENT_DATE - INTERVAL '35 days'
                  AND dg.distance_totale_m > 0
                  AND dg.duree_minutes > 0
                ORDER BY s.date DESC
            """, (str(joueur_id), ['MATCH', 'MATCH_AMICAL', 'INTENSIF']))
            rows = cur.fetchall()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return {"niveau": None, "message": None}

    today = _date.today()

    def jours_depuis(d):
        if hasattr(d, 'date'):
            d = d.date()
        return (today - d).days

    recent   = [r for r in rows if jours_depuis(r[0]) < 7]
    baseline = [r for r in rows if 7 <= jours_depuis(r[0]) <= 35]

    # Au moins 2 séances HI récentes (pic récent fiable) et 2 en baseline,
    # sinon le pic récent (1 séance) serait trop bruité pour conclure.
    if len(recent) < 2 or len(baseline) < 2:
        return {"niveau": None, "message": None}

    # ── Gate principal : pic de vitesse de pointe (capacité) ──
    vmax_r = [float(r[2]) for r in recent if r[2] is not None]
    vmax_b = [float(r[2]) for r in baseline if r[2] is not None]
    if len(vmax_r) < 2 or len(vmax_b) < 2:
        return {"niveau": None, "message": None}

    pic_r, pic_b = max(vmax_r), max(vmax_b)
    if pic_b <= 0:
        return {"niveau": None, "message": None}

    ratio_vmax = pic_r / pic_b
    # Seuil POSSIBLE à 7 % : absorbe le bruit GPS et le biais d'échantillonnage
    # (la baseline a plus de séances → pic mécaniquement un peu plus haut).
    seuil_poss = cfg.get("seuil_vmax_capacite_possible", 0.93)
    seuil_prob = cfg.get("seuil_vmax_capacite_probable", 0.90)

    # Capacité intacte (le joueur a touché sa pointe récemment) → aucun signal.
    if ratio_vmax > seuil_poss:
        return {"niveau": None, "message": None}

    pct_vmax = round((1 - ratio_vmax) * 100)

    # ── Confirmation (volume > 28 km/h par minute) ──
    def d28_par_min(rs):
        dur = sum(float(r[1]) for r in rs)
        d28 = sum(float(r[3]) for r in rs if r[3] is not None)
        return (d28 / dur) if dur > 0 else None

    pr, pb = d28_par_min(recent), d28_par_min(baseline)
    seuil_corrob = cfg.get("seuil_sprint_corroboration", 0.80)
    volume_baisse = pr is not None and pb and pb > 0 and (pr / pb) <= seuil_corrob
    pct_d28 = round((1 - pr / pb) * 100) if (pr is not None and pb and pb > 0) else None

    niveau = "PROBABLE" if (ratio_vmax <= seuil_prob and volume_baisse) else "POSSIBLE"

    message = (f"possibilité de fatigue neuromusculaire : baisse de {pct_vmax}% "
               f"de sa vitesse de pointe sur ses séances à haute intensité (vs 4 sem.)")
    if volume_baisse:
        message += f", confirmée par −{pct_d28}% de courses à plus de 28 km/h"
    return {"niveau": niveau, "message": message}


def _niveau_risque(score: float) -> str:
    if score < 30:
        return "FAIBLE"
    elif score < 60:
        return "MODERE"
    return "ELEVE"


def _niveau_fatigue(score: float) -> str:
    if score < 30:
        return "NOMINAL"
    elif score < 60:
        return "VIGILANCE"
    return "ALERTE"


# Ancres (score 0-100 → probabilité % de blessure à 7 jours). Mapping monotone,
# calibrable plus tard sur les blessures observées — AUCUN apprentissage ici.
_PROBA_ANCRES = [(0, 2), (20, 5), (30, 8), (45, 14), (60, 24), (80, 42), (100, 60)]


def _score_vers_proba(score: float) -> int:
    """Convertit un score de risque 0-100 en probabilité % à 7 jours (interpolation linéaire)."""
    s = max(0.0, min(float(score), 100.0))
    for (x0, y0), (x1, y1) in zip(_PROBA_ANCRES, _PROBA_ANCRES[1:]):
        if s <= x1:
            t = 0 if x1 == x0 else (s - x0) / (x1 - x0)
            return round(y0 + t * (y1 - y0))
    return _PROBA_ANCRES[-1][1]


def _risque_probabiliste(joueur_id: UUID, cfg: dict, conn, ctx=None, date_ref=None) -> dict:
    """
    Sortie probabiliste EXPLICABLE du risque de blessure (sans ML) :
      - probabilité estimée à 7 jours (mapping du score),
      - facteur dominant (plus forte contribution),
      - tendance (score actuel vs score à J-7),
      - phrase prête à afficher.

    Tient compte du CONTEXTE (saison/période/fraîcheur) : hors charge / inactif /
    blessé → pas d'estimation sur données périmées ; préparation/reprise → ACWR neutralisé.
    `date_ref` (date simulée) décale toute l'évaluation à une autre date.
    """
    from datetime import timedelta
    if ctx is None:
        ctx = _contexte_joueur(joueur_id, cfg, conn, date_ref)

    base = {
        "etat": ctx["etat"], "periode_type": ctx["periode_type"],
        "periode_libelle": ctx["periode_libelle"], "jours_inactif": ctx["jours_inactif"],
    }

    if ctx["silence"]:
        phrase = {
            "HORS_CHARGE": f"Hors charge ({ctx['periode_libelle'] or 'trêve / intersaison'}) — "
                           f"risque de blessure non évalué.",
            "HORS_SAISON": "Aucune saison en cours — risque non évalué (hors saison).",
            "INACTIF":     "Aucune donnée récente — risque non évalué (hors charge).",
            "BLESSE":      "Joueur en cours de blessure — suivi médical, charge non évaluée.",
        }.get(ctx["etat"], "Risque non évalué.")
        return {**base, "score": 0.0, "probabilite": None, "niveau": "FAIBLE",
                "phrase": phrase, "facteur_dominant": None, "tendance": "STABLE", "source": None,
                "contributions": [], "acwr": None, "acwr_gps": None, "acwr_rpe": None,
                "semaines_gps": None, "semaines_rpe": None, "ecart_sources": None,
                "provisoire": False}

    risque = _calcul_score_risque(joueur_id, cfg, conn, date_ref=date_ref,
                                  neutraliser_acwr=ctx["neutraliser_acwr"])
    score  = risque["score"]
    proba  = _score_vers_proba(score)

    contributions = risque.get("contributions") or []
    dominant = max(contributions, key=lambda c: c["points"], default=None)
    facteur_dominant = dominant["libelle"] if dominant else None

    # Tendance : comparaison au score d'il y a 7 jours (même neutralisation)
    seuil = float(cfg.get("tendance_seuil_pts", 5))
    try:
        score_avant = _calcul_score_risque(joueur_id, cfg, conn,
                                           date_ref=(date_ref or _date.today()) - timedelta(days=7),
                                           neutraliser_acwr=ctx["neutraliser_acwr"])["score"]
        delta = score - score_avant
        if delta >= seuil:
            tendance, fleche = "HAUSSE", "↗ en hausse"
        elif delta <= -seuil:
            tendance, fleche = "BAISSE", "↘ en baisse"
        else:
            tendance, fleche = "STABLE", "→ stable"
    except Exception:
        tendance, fleche = "STABLE", "→ stable"

    # Sans base de charge, le score retombe sur un plancher conventionnel (20) : afficher une
    # probabilité dérivée de ce plancher ferait passer un joueur SANS DONNÉES pour un joueur
    # sain. On coupe donc la probabilité — le score reste, mais il n'est plus habillé en %.
    if risque["acwr"] is None:
        phrase = "Données de charge insuffisantes pour estimer le risque."
        proba  = None
    else:
        phrase = f"Risque ~{proba} % à 7 jours"
        if facteur_dominant:
            phrase += f" · facteur principal : {facteur_dominant}"
        phrase += f" · {fleche}"

    return {
        **base,
        "score":            score,
        "probabilite":      proba,
        "niveau":           _niveau_risque(score),
        "phrase":           phrase,
        "facteur_dominant": facteur_dominant,
        "tendance":         tendance,
        "source":           risque.get("source"),
        # Explicabilité : la liste complète des facteurs (triée par poids décroissant) permet au
        # front d'afficher les 2 causes principales puis de replier le reste, sans parser la phrase.
        "contributions":    sorted(contributions, key=lambda c: c["points"], reverse=True),
        "acwr":             risque.get("acwr"),
        "acwr_gps":         risque.get("acwr_gps"),
        "acwr_rpe":         risque.get("acwr_rpe"),
        "semaines_gps":     risque.get("semaines_gps"),
        "semaines_rpe":     risque.get("semaines_rpe"),
        "ecart_sources":    risque.get("ecart_sources"),
        "provisoire":       risque.get("provisoire", False),
    }


@router.get("/risque/{joueur_id}", response_model=RisqueBlessure)
def get_risque_blessure(joueur_id: UUID, x_date_simulee: str | None = Header(default=None)):
    date_ref = _parse_date_simulee(x_date_simulee)
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, nom, prenom FROM joueur WHERE id = %s",
                    (str(joueur_id),)
                )
                joueur = cur.fetchone()

            if not joueur:
                raise HTTPException(status_code=404, detail="Joueur introuvable")

            cfg = _load_config(conn)
            r   = _risque_probabiliste(joueur_id, cfg, conn, date_ref=date_ref)

        return RisqueBlessure(
            joueur_id=joueur_id,
            nom=joueur[1],
            prenom=joueur[2],
            score_risque=r["score"],
            niveau=r["niveau"],
            probabilite=r["probabilite"],
            phrase=r["phrase"],
            facteur_dominant=r["facteur_dominant"],
            tendance=r["tendance"],
            source=r["source"],
            etat=r.get("etat"),
            periode_type=r.get("periode_type"),
            periode_libelle=r.get("periode_libelle"),
            jours_inactif=r.get("jours_inactif"),
            contributions=r.get("contributions") or [],
            acwr=r.get("acwr"),
            acwr_gps=r.get("acwr_gps"),
            acwr_rpe=r.get("acwr_rpe"),
            semaines_gps=r.get("semaines_gps"),
            semaines_rpe=r.get("semaines_rpe"),
            ecart_sources=r.get("ecart_sources"),
            provisoire=r.get("provisoire"),
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/charge-cible/{joueur_id}", response_model=ChargeCible)
def get_charge_cible(joueur_id: UUID):
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM joueur WHERE id = %s", (str(joueur_id),))
                if not cur.fetchone():
                    raise HTTPException(status_code=404, detail="Joueur introuvable")
            cfg = _load_config(conn)
            c   = _charge_cible(joueur_id, cfg, conn)

        return ChargeCible(joueur_id=joueur_id, **c)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/fatigue/{joueur_id}", response_model=NiveauFatigue)
def get_fatigue(joueur_id: UUID, x_date_simulee: str | None = Header(default=None)):
    date_ref = _parse_date_simulee(x_date_simulee)
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, nom, prenom FROM joueur WHERE id = %s",
                    (str(joueur_id),)
                )
                joueur = cur.fetchone()

            if not joueur:
                raise HTTPException(status_code=404, detail="Joueur introuvable")

            cfg = _load_config(conn)
            ctx = _contexte_joueur(joueur_id, cfg, conn, date_ref)
            if ctx["silence"]:
                ji = ctx["jours_inactif"]
                depuis = f" depuis {ji} j" if ji is not None else ""
                libelle_periode = ctx["periode_libelle"] or "trêve / intersaison"
                raison = {
                    "HORS_CHARGE": f"Hors charge ({libelle_periode}) — pas de suivi de fatigue.",
                    "HORS_SAISON": "Aucune saison en cours — pas de suivi de fatigue.",
                    "INACTIF":     f"Aucune donnée récente{depuis} — fatigue non évaluée.",
                    "BLESSE":      "Joueur en cours de blessure — fatigue d'entraînement non évaluée.",
                }.get(ctx["etat"], "Fatigue non évaluée.")
                fatigue = {"score": 0.0, "niveau": "NOMINAL", "raison": raison,
                           "signaux": [], "indicatifs": [], "donnees": False}
            else:
                fatigue = _calcul_fatigue(joueur_id, cfg, conn)

        return NiveauFatigue(
            joueur_id=joueur_id,
            nom=joueur[1],
            prenom=joueur[2],
            score_fatigue=fatigue["score"],
            niveau=fatigue["niveau"],
            raison=fatigue["raison"],
            signaux=fatigue.get("signaux") or [],
            indicatifs=fatigue.get("indicatifs") or [],
            donnees=fatigue.get("donnees"),
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _equipes_scope(x_contexte_equipes, x_contexte_club, conn):
    """
    Équipes sur lesquelles scoper une vue d'équipe, d'après les en-têtes de contexte transmis
    par le BACK Java (qui a déjà résolu la portée autorisée via ScopeResolver — Python ne fait
    que filtrer ce que le back lui demande) :
      X-Contexte-Equipes (CSV d'ids) prioritaire, sinon toutes les équipes de X-Contexte-Club.
    Retourne une liste d'equipe_id (str) ou None = pas de scoping (le back n'a rien transmis).
    """
    if x_contexte_equipes:
        ids = [e.strip() for e in x_contexte_equipes.split(",") if e.strip()]
        return ids or None
    if x_contexte_club and x_contexte_club.strip():
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM equipe WHERE club_id = %s", (x_contexte_club.strip(),))
                ids = [str(r[0]) for r in cur.fetchall()]
            return ids or None
        except Exception:
            try: conn.rollback()
            except Exception: pass
    return None


@router.get("/charge-collective")
def get_charge_collective(semaines: int = 4,
                          x_date_simulee: str | None = Header(default=None),
                          x_contexte_equipes: str | None = Header(default=None),
                          x_contexte_club: str | None = Header(default=None)):
    """
    Charge collective (km) par semaine glissante sur les `semaines` dernières
    semaines (4, 8 ou 12). Index 0 = la plus ancienne, dernier = semaine en cours.
    La « semaine en cours » est ancrée sur la date simulée (X-Date-Simulee, super-admin)
    quand elle est fournie, sinon sur la date réelle ; les séances postérieures sont exclues.
    """
    semaines = semaines if semaines in (4, 8, 12) else 4
    jours = semaines * 7
    ref = _parse_date_simulee(x_date_simulee) or _date.today()
    try:
        with get_connection() as conn:
            scope = _equipes_scope(x_contexte_equipes, x_contexte_club, conn)
            extra = ""
            # Ordre des paramètres = ordre des %s dans la requête (ref utilisée 3×).
            qp: list = [semaines, ref, ref, jours, ref]
            if scope:
                extra = " AND s.equipe_id = ANY(%s)"; qp.append(scope)
            with conn.cursor() as cur:
                # bucket : 0 = semaine la plus ancienne … (semaines-1) = semaine en cours (= ref)
                cur.execute(f"""
                    SELECT
                        %s - 1 - FLOOR((%s::date - s.date) / 7)::int AS semaine_idx,
                        ROUND(SUM(dg.distance_totale_m) / 1000.0, 1) AS total_km
                    FROM donnee_gps dg
                    JOIN seance s ON dg.seance_id = s.id
                    JOIN joueur j ON j.id = dg.joueur_id
                    WHERE s.date >= %s::date - (%s || ' days')::interval
                      AND s.date <= %s::date
                      AND j.statut != 'inactif'{extra}
                    GROUP BY 1
                    ORDER BY 1
                """, tuple(qp))
                rows = cur.fetchall()

        data = [0.0] * semaines
        for row in rows:
            idx = int(row[0])
            if 0 <= idx < semaines:
                data[idx] = float(row[1])

        labels = [f"S-{semaines - i}" for i in range(semaines)]
        return {"labels": labels, "data": data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/seance/{seance_id}/rapport")
def get_rapport_seance(seance_id: UUID):
    try:
        with get_connection() as conn:
            cfg = _load_config(conn)

            with conn.cursor() as cur:
                cur.execute("""
                    SELECT s.id, s.date, ts.code, ts.libelle, s.type_seance_id,
                           s.objectif, s.objectif_distance_m, s.objectif_intensite,
                           s.objectif_distance_haute_intensite_m,
                           s.duree_minutes, ts.duree_theorique_min
                    FROM seance s
                    JOIN type_seance ts ON ts.id = s.type_seance_id
                    WHERE s.id = %s
                """, (str(seance_id),))
                seance = cur.fetchone()

            if not seance:
                raise HTTPException(status_code=404, detail="Séance introuvable")

            type_seance_id = seance[4]
            type_code      = seance[2]
            seance_date    = seance[1]

            # Objectif d'équipe saisi par le préparateur (Phase 1), tous types
            objectif_texte             = seance[5]
            objectif_distance_m        = int(seance[6]) if seance[6] is not None else None
            objectif_intensite         = int(seance[7]) if seance[7] is not None else None
            objectif_distance_hi_m     = int(seance[8]) if seance[8] is not None else None

            # Durée de référence de la séance, pour proratiser l'objectif d'équipe par
            # joueur selon son temps de jeu réel. Chaîne de repli (jamais nulle en pratique) :
            # durée planifiée de la séance → somme du déroulé d'exercices →
            # durée théorique du type → (dernier repli) max des durées réelles GPS (plus bas).
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT SUM(COALESCE(se.duree_minutes, e.duree_minutes))
                    FROM seance_exercice se
                    JOIN exercice e ON e.id = se.exercice_id
                    WHERE se.seance_id = %s
                """, (str(seance_id),))
                ref_row = cur.fetchone()
            duree_planifiee = float(seance[9])  if seance[9]  else None
            duree_deroule   = float(ref_row[0]) if ref_row and ref_row[0] else None
            duree_type      = float(seance[10]) if seance[10] else None
            duree_reference = duree_planifiee or duree_deroule or duree_type

            with conn.cursor() as cur:
                cur.execute("""
                    SELECT j.id, j.nom, j.prenom, j.poste_principal,
                           dg.distance_totale_m, dg.duree_minutes,
                           dg.vitesse_max_kmh, dg.nb_sprints_24kmh
                    FROM donnee_gps dg
                    JOIN joueur j ON j.id = dg.joueur_id
                    WHERE dg.seance_id = %s
                    ORDER BY j.nom, j.prenom
                """, (str(seance_id),))
                players = cur.fetchall()

            # Dernier repli : sans déroulé ni durée planifiée (séance-coquille GPS ou match),
            # on prend la plus longue durée réelle jouée (≈ un joueur ayant fait toute la séance).
            if not duree_reference and players:
                duree_reference = max((float(p[5]) for p in players if p[5]), default=None)

            sous_norme_pct = cfg.get("seuil_sous_norme_pct", 20.0)
            sur_norme_pct  = cfg.get("seuil_sur_norme_pct",  20.0)
            corr_pct_kg    = cfg.get("correction_surpoids_pct_par_kg",  2.0)
            corr_pct_max   = cfg.get("correction_surpoids_plafond_pct", 20.0)
            recence_j      = int(cfg.get("baseline_recence_jours", 90))

            lignes = []
            for p in players:
                joueur_id     = p[0]
                poste         = _normaliser_poste(p[3] or "")
                dist_reelle   = float(p[4]) if p[4] is not None else None
                duree_reelle  = float(p[5]) if p[5] is not None else None
                poids_actuel, poids_cible = _poids_a_date(joueur_id, seance_date, conn)

                # Baseline personnelle = m/min moyen des 10 dernières séances RÉALISÉES (H.5) du
                # même type, dans la fenêtre de récence (écarte une baseline trop ancienne, ex.
                # d'avant une longue blessure). `_n` = taille réelle. La variante « toutes séances
                # confondues » sert de repli affichable quand le type est trop mince.
                def _baseline_rapport(meme_type: bool):
                    filtre_type = "AND s.type_seance_id = %s" if meme_type else ""
                    params = [str(joueur_id)]
                    if meme_type:
                        params.append(str(type_seance_id))
                    params += [str(seance_id), seance_date]
                    with conn.cursor() as cur:
                        cur.execute(f"""
                            SELECT AVG(sub.ratio), COUNT(*) FROM (
                                SELECT dg.distance_totale_m / NULLIF(dg.duree_minutes, 0) AS ratio
                                FROM donnee_gps dg
                                JOIN seance s ON dg.seance_id = s.id
                                WHERE dg.joueur_id = %s
                                  {filtre_type}
                                  AND dg.seance_id != %s
                                  AND s.statut = 'REALISEE'
                                  AND s.date >= %s::date - INTERVAL '{recence_j} days'
                                  AND dg.duree_minutes > 0
                                  AND dg.distance_totale_m > 0
                                ORDER BY s.date DESC
                                LIMIT 10
                            ) sub
                        """, params)
                        r = cur.fetchone()
                    ratio = float(r[0]) if r and r[0] is not None else None
                    n     = int(r[1])   if r and r[1] else 0
                    return ratio, n

                avg_ratio,   baseline_n   = _baseline_rapport(True)
                avg_ratio_g, baseline_n_g = _baseline_rapport(False)
                dist_attendue   = round(avg_ratio   * duree_reelle, 0) if avg_ratio   and duree_reelle else None
                dist_attendue_g = round(avg_ratio_g * duree_reelle, 0) if avg_ratio_g and duree_reelle else None

                delta_m = delta_pct = None
                statut  = "SANS_BASELINE"

                if dist_reelle is not None and dist_attendue and dist_attendue > 0:
                    delta_m   = round(dist_reelle - dist_attendue, 0)
                    delta_pct = round((delta_m / dist_attendue) * 100, 1)
                    statut    = ("SOUS_NORME" if delta_pct < -sous_norme_pct
                                 else "SUR_NORME" if delta_pct > sur_norme_pct
                                 else "DANS_NORME")

                # Objectif séance (équipe) au prorata du temps joué — tous types
                objectif_seance_m = atteint_objectif_seance = None
                if objectif_distance_m and duree_reference and duree_reelle:
                    objectif_seance_m = round(objectif_distance_m * (duree_reelle / duree_reference), 0)
                    if dist_reelle is not None:
                        atteint_objectif_seance = dist_reelle >= objectif_seance_m

                objectif_m = ratio_objectif = ratio_objectif_original = None
                correction_poids_pct = ecart_poids_kg = atteint_objectif = None

                if type_code in ('MATCH', 'MATCH_AMICAL') and duree_reelle:
                    ratio_objectif_original = _objectif_poste(poste, cfg)
                    if ratio_objectif_original is not None:
                        correction_poids_pct = None
                        if poids_actuel is not None and poids_cible is not None:
                            ecart_kg = max(0.0, poids_actuel - poids_cible)
                            if ecart_kg >= 0.5:
                                correction_poids_pct = round(min(ecart_kg * corr_pct_kg, corr_pct_max), 1)
                                ecart_poids_kg       = round(ecart_kg, 1)
                        coeff          = 1.0 - (correction_poids_pct or 0.0) / 100.0
                        ratio_objectif = round(ratio_objectif_original * coeff, 2)
                        objectif_m     = round(ratio_objectif * duree_reelle, 0)
                        atteint_objectif = dist_reelle >= objectif_m if dist_reelle is not None else None

                lignes.append({
                    "joueur_id":               str(joueur_id),
                    "nom":                     p[1],
                    "prenom":                  p[2],
                    "poste":                   p[3] or "",
                    "duree_minutes":           int(duree_reelle) if duree_reelle else None,
                    "distance_reelle":         dist_reelle,
                    "distance_attendue":         dist_attendue,
                    "baseline_n":                baseline_n,
                    "distance_attendue_globale": dist_attendue_g,
                    "baseline_n_globale":        baseline_n_g,
                    "ratio_reel":              round(dist_reelle / duree_reelle, 1) if dist_reelle and duree_reelle else None,
                    "delta_m":                 delta_m,
                    "delta_pct":               delta_pct,
                    "statut":                  statut,
                    "vitesse_max":             float(p[6]) if p[6] is not None else None,
                    "nb_sprints":              int(p[7])   if p[7] is not None else None,
                    "objectif_m":              objectif_m,
                    "ratio_objectif":          ratio_objectif,
                    "ratio_objectif_original": ratio_objectif_original,
                    "correction_poids_pct":    correction_poids_pct,
                    "ecart_poids_kg":          ecart_poids_kg,
                    "atteint_objectif":        atteint_objectif,
                    "objectif_seance_m":       objectif_seance_m,
                    "atteint_objectif_seance": atteint_objectif_seance,
                })

        return {
            "seance_id":    str(seance_id),
            "date":         str(seance[1]),
            "type_code":    type_code,
            "type_libelle": seance[3],
            "nb_joueurs":   len(lignes),
            # Objectif d'équipe de la séance (cible prépa, tous types)
            "objectif":                            objectif_texte,
            "objectif_distance_m":                 objectif_distance_m,
            "objectif_intensite":                  objectif_intensite,
            "objectif_distance_haute_intensite_m": objectif_distance_hi_m,
            "duree_reference_minutes":             int(duree_reference) if duree_reference else None,
            "lignes":       lignes,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/equipe/charge")
def get_charge_equipe(debut: str | None = None, fin: str | None = None, types: str | None = None,
                      x_contexte_equipes: str | None = Header(default=None),
                      x_contexte_club: str | None = Header(default=None)):
    """
    Charge externe agrégée de l'équipe sur une période.
    Renvoie deux vues :
      - seances : une ligne par séance de la période (totaux d'équipe + distance attendue) ;
      - joueurs : totaux par joueur + classement (tri par distance décroissante).
    La distance attendue réutilise la baseline du rapport par séance (ratio moyen des
    10 dernières séances de même type du joueur).
    """
    sous_seuil = sur_seuil = None
    type_codes = [t.strip().upper() for t in types.split(",")] if types else None
    try:
        with get_connection() as conn:
            cfg = _load_config(conn)
            sous_seuil = cfg.get("seuil_sous_norme_pct", 20.0)
            sur_seuil  = cfg.get("seuil_sur_norme_pct",  20.0)
            recence_j  = int(cfg.get("baseline_recence_jours", 90))
            scope = _equipes_scope(x_contexte_equipes, x_contexte_club, conn)

            # Historique des ratios par (joueur, type), du plus récent au plus ancien.
            # Baseline d'une séance = moyenne des 10 plus récentes du même type, HORS séance
            # courante (même logique que le rapport par séance, sans correction météo).
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT dg.joueur_id, s.type_seance_id, dg.seance_id,
                           dg.distance_totale_m / NULLIF(dg.duree_minutes, 0) AS ratio
                    FROM donnee_gps dg
                    JOIN seance s ON s.id = dg.seance_id
                    WHERE dg.duree_minutes > 0 AND dg.distance_totale_m > 0
                      AND s.statut = 'REALISEE'
                      AND s.date >= CURRENT_DATE - INTERVAL '{recence_j} days'
                    ORDER BY dg.joueur_id, s.type_seance_id, s.date DESC
                """)
                hist: dict = {}
                for jid_, tid_, sid_, ratio_ in cur.fetchall():
                    if ratio_ is None:
                        continue
                    hist.setdefault((str(jid_), str(tid_)), []).append((str(sid_), float(ratio_)))

            # Lignes GPS de la période (scoping équipe via contexte + filtre type optionnel).
            params: list = []
            where = ["j.statut != 'inactif'"]
            if scope:
                where.append("s.equipe_id = ANY(%s)"); params.append(scope)
            if debut:
                where.append("s.date >= %s"); params.append(debut)
            if fin:
                where.append("s.date <= %s"); params.append(fin)
            if type_codes:
                where.append("ts.code = ANY(%s)"); params.append(type_codes)

            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT s.id, s.date, ts.code, ts.libelle, s.type_seance_id,
                           j.id, j.nom, j.prenom, j.poste_principal,
                           dg.distance_totale_m, dg.duree_minutes,
                           dg.distance_19kmh_m, dg.distance_sprint_28kmh_m,
                           dg.nb_sprints_24kmh, dg.vitesse_max_kmh,
                           dg.nb_accelerations, dg.nb_freinages
                    FROM donnee_gps dg
                    JOIN seance s ON s.id = dg.seance_id
                    JOIN type_seance ts ON ts.id = s.type_seance_id
                    JOIN joueur j ON j.id = dg.joueur_id
                    WHERE {' AND '.join(where)}
                    ORDER BY s.date, j.nom, j.prenom
                """, params)
                rows = cur.fetchall()

        def _statut(dist, att):
            if dist is None or not att or att <= 0:
                return "SANS_BASELINE"
            pct = (dist - att) / att * 100
            return "SOUS_NORME" if pct < -sous_seuil else "SUR_NORME" if pct > sur_seuil else "DANS_NORME"

        def _baseline(jid: str, tid: str, sid: str):
            lst = [r for (s, r) in hist.get((jid, tid), []) if s != sid][:10]
            return sum(lst) / len(lst) if lst else None

        def _f(v):  return float(v) if v is not None else None
        def _i(v):  return int(v)   if v is not None else None

        seances: dict = {}
        joueurs: dict = {}

        for r in rows:
            (sid, sdate, tcode, tlib, type_seance_id,
             jid, nom, prenom, poste,
             dist, duree, d19, d28, sprints, vmax, accel, frein) = r
            sid, jid, type_seance_id = str(sid), str(jid), str(type_seance_id)
            dist  = _f(dist); duree = _f(duree)
            ratio = _baseline(jid, type_seance_id, sid)
            att   = round(ratio * duree, 0) if ratio and duree else None

            s = seances.get(sid)
            if s is None:
                s = seances[sid] = {
                    "seance_id": sid, "date": str(sdate), "type_code": tcode, "type_libelle": tlib,
                    "nb_joueurs": 0, "distance_totale_m": 0.0, "distance_attendue_m": 0.0,
                    "duree_minutes": 0.0, "distance_19kmh_m": 0.0, "distance_28kmh_m": 0.0,
                    "nb_sprints": 0, "nb_accelerations": 0, "nb_freinages": 0,
                    "vitesse_max": None, "_att_count": 0,
                }
            s["nb_joueurs"]        += 1
            s["distance_totale_m"] += dist or 0.0
            s["duree_minutes"]     += duree or 0.0
            s["distance_19kmh_m"]  += _f(d19) or 0.0
            s["distance_28kmh_m"]  += _f(d28) or 0.0
            s["nb_sprints"]        += _i(sprints) or 0
            s["nb_accelerations"]  += _i(accel) or 0
            s["nb_freinages"]      += _i(frein) or 0
            if att is not None:
                s["distance_attendue_m"] += att
                s["_att_count"]          += 1
            if vmax is not None:
                s["vitesse_max"] = max(s["vitesse_max"] or 0.0, _f(vmax))

            j = joueurs.get(jid)
            if j is None:
                j = joueurs[jid] = {
                    "joueur_id": jid, "nom": nom, "prenom": prenom, "poste": poste or "",
                    "nb_seances": 0, "distance_totale_m": 0.0, "distance_attendue_m": 0.0,
                    "duree_minutes": 0.0, "distance_19kmh_m": 0.0, "distance_28kmh_m": 0.0,
                    "nb_sprints": 0, "vitesse_max": None, "_att_count": 0,
                }
            j["nb_seances"]        += 1
            j["distance_totale_m"] += dist or 0.0
            j["duree_minutes"]     += duree or 0.0
            j["distance_19kmh_m"]  += _f(d19) or 0.0
            j["distance_28kmh_m"]  += _f(d28) or 0.0
            j["nb_sprints"]        += _i(sprints) or 0
            if att is not None:
                j["distance_attendue_m"] += att
                j["_att_count"]          += 1
            if vmax is not None:
                j["vitesse_max"] = max(j["vitesse_max"] or 0.0, _f(vmax))

        def _finalise(d: dict, par_joueur: bool) -> dict:
            att        = round(d["distance_attendue_m"], 0) if d["_att_count"] else None
            duree_sum  = d["duree_minutes"]
            nb         = d["nb_joueurs"] if not par_joueur else 1
            d["distance_totale_m"]   = round(d["distance_totale_m"], 0)
            d["distance_attendue_m"] = att
            d["distance_19kmh_m"]    = round(d["distance_19kmh_m"], 0)
            d["distance_28kmh_m"]    = round(d["distance_28kmh_m"], 0)
            # Intensité = distance d'équipe / minutes-joueur cumulées (m/min).
            d["ratio_reel"]          = round(d["distance_totale_m"] / duree_sum, 0) if duree_sum else None
            # Durée affichée : total (par joueur) ou moyenne par joueur (par séance).
            d["duree_minutes"]       = round(duree_sum / nb, 0) if nb else round(duree_sum, 0)
            d["statut"]              = _statut(d["distance_totale_m"], att)
            d["delta_pct"]           = round((d["distance_totale_m"] - att) / att * 100, 1) if att else None
            if d["vitesse_max"] is not None:
                d["vitesse_max"] = round(d["vitesse_max"], 1)
            d.pop("_att_count", None)
            return d

        seances_out = [_finalise(s, False) for s in seances.values()]
        seances_out.sort(key=lambda s: s["date"])
        joueurs_out = [_finalise(j, True) for j in joueurs.values()]
        joueurs_out.sort(key=lambda j: j["distance_totale_m"], reverse=True)
        for i, j in enumerate(joueurs_out):
            j["rang"] = i + 1

        return {"seances": seances_out, "joueurs": joueurs_out}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/equipe/objectif-hebdo")
def get_objectif_hebdo(x_contexte_equipes: str | None = Header(default=None),
                       x_contexte_club: str | None = Header(default=None)):
    """
    Panneau « Objectif de la semaine » (semaine ISO en cours, lundi → aujourd'hui).
    Par joueur de l'effectif : cumul de distance de la semaine, cible A.5 (« suggestion
    intelligente »), objectif retenu (manuel d'équipe si défini, sinon la cible A.5) et atteinte.
    L'objectif manuel n'est lu que si le contexte cible UNE seule équipe.
    """
    try:
        with get_connection() as conn:
            cfg   = _load_config(conn)
            scope = _equipes_scope(x_contexte_equipes, x_contexte_club, conn)
            return _objectif_hebdo_data(conn, cfg, scope)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _objectif_hebdo_data(conn, cfg, scope) -> dict:
    """
    Cœur du panneau « Objectif de la semaine » (extrait pour être réutilisé par la carte briefing
    sans repasser par la couche HTTP). Par joueur : cumul de la semaine en cours, cible A.5,
    objectif retenu (manuel d'équipe si défini et scope = 1 équipe, sinon cible A.5) et atteinte.
    """
    objectif_m = None
    if scope and len(scope) == 1:
        with conn.cursor() as cur:
            cur.execute("SELECT objectif_distance_m FROM objectif_hebdo WHERE equipe_id = %s",
                        (scope[0],))
            row = cur.fetchone()
            objectif_m = int(row[0]) if row and row[0] is not None else None

    # Cumul de distance de la semaine en cours (lundi → aujourd'hui), par joueur.
    cwhere = ["s.date >= date_trunc('week', CURRENT_DATE)::date"]
    cparams: list = []
    if scope:
        cwhere.append("s.equipe_id = ANY(%s)"); cparams.append(scope)
    cumul: dict = {}
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT dg.joueur_id, SUM(dg.distance_totale_m)
            FROM donnee_gps dg JOIN seance s ON s.id = dg.seance_id
            WHERE {' AND '.join(cwhere)}
            GROUP BY dg.joueur_id
        """, cparams)
        for jid, tot in cur.fetchall():
            cumul[str(jid)] = float(tot or 0.0)

    joueurs = []
    somme_ideal = 0.0
    nb_ideal = 0
    sem_dispo: list = []   # semaines de données réellement disponibles (fiabilité de la suggestion)
    for (jid, nom, prenom, poste) in _joueurs_resume(conn, scope):
        jid = str(jid)
        cible = _charge_cible(jid, cfg, conn)
        cible_ideal_m = None
        if cible.get("disponible") and cible.get("unite") == "km" and cible.get("cible_ideal") is not None:
            cible_ideal_m = round(cible["cible_ideal"] * 1000)
            somme_ideal += cible_ideal_m
            nb_ideal += 1
            if cible.get("semaines") is not None:
                sem_dispo.append(cible["semaines"])
        cum = round(cumul.get(jid, 0.0))
        obj = objectif_m if objectif_m is not None else cible_ideal_m
        atteint = (cum >= obj) if obj else None
        reste   = max(0, obj - cum) if obj else None
        joueurs.append({
            "joueur_id":     jid,
            "nom":           nom,
            "prenom":        prenom,
            "poste":         poste or "",
            "cumul_m":       cum,
            "cible_ideal_m": cible_ideal_m,
            "cible_min_m":   round(cible["cible_min"]   * 1000) if cible_ideal_m is not None and cible.get("cible_min")   is not None else None,
            "cible_haute_m": round(cible["cible_haute"] * 1000) if cible_ideal_m is not None and cible.get("cible_haute") is not None else None,
            "plafond_m":     round(cible["plafond"]     * 1000) if cible_ideal_m is not None and cible.get("plafond")     is not None else None,
            "objectif_m":    obj,
            "source":        "MANUEL" if objectif_m is not None else ("INTELLIGENT" if cible_ideal_m is not None else None),
            "atteint":       atteint,
            "reste_m":       reste,
        })

    concernes    = [j for j in joueurs if j["atteint"] is not None]
    nb_concernes = len(concernes)
    nb_atteint   = sum(1 for j in concernes if j["atteint"])
    meilleur = None
    avec_obj = [j for j in joueurs if j["objectif_m"]]
    if avec_obj:
        m = max(avec_obj, key=lambda j: j["cumul_m"])
        meilleur = {"joueur_id": m["joueur_id"], "nom": m["nom"],
                    "prenom": m["prenom"], "cumul_m": m["cumul_m"]}

    return {
        "objectif_distance_m":  objectif_m,
        "suggestion_moyenne_m": round(somme_ideal / nb_ideal) if nb_ideal else None,
        "suggestion_semaines":  min(sem_dispo) if sem_dispo else None,
        "suggestion_provisoire": bool(sem_dispo) and min(sem_dispo) < 4,
        "multi_equipes":        bool(scope) and len(scope) > 1,
        "nb_atteint":           nb_atteint,
        "nb_concernes":         nb_concernes,
        "meilleur":             meilleur,
        "joueurs":              joueurs,
    }


@router.get("/equipe/briefing")
def get_briefing(x_contexte_equipes: str | None = Header(default=None),
                 x_contexte_club: str | None = Header(default=None)):
    """
    Bundle d'INDICATEURS COMPACTS pour la carte « briefing » du préparateur.
    N'est JAMAIS envoyé tel quel au front : consommé par le back Java qui le met en mots (LLM) ou
    remplit un gabarit. Dérivé du panneau objectif hebdo (cumul de la semaine vs cible ACWR par
    joueur) → atteinte de l'objectif + joueurs en surcharge (cumul > plafond) / sous-charge
    (cumul < cible mini). Aucune donnée brute, uniquement des chiffres agrégés.
    """
    try:
        with get_connection() as conn:
            cfg   = _load_config(conn)
            scope = _equipes_scope(x_contexte_equipes, x_contexte_club, conn)
            oh    = _objectif_hebdo_data(conn, cfg, scope)

        joueurs = oh["joueurs"]

        def _nom(j):
            return f'{(j.get("prenom") or "").strip()} {(j.get("nom") or "").strip()}'.strip()

        surcharge, souscharge = [], []
        for j in joueurs:
            cum, plaf, cmin = j.get("cumul_m"), j.get("plafond_m"), j.get("cible_min_m")
            if plaf is not None and cum is not None and cum > plaf:
                surcharge.append({"nom": _nom(j), "cumul_m": cum, "plafond_m": plaf})
            elif cmin is not None and cum is not None and cum < cmin:
                souscharge.append({"nom": _nom(j), "cumul_m": cum, "cible_min_m": cmin})
        surcharge.sort(key=lambda x: x["cumul_m"] - x["plafond_m"], reverse=True)
        souscharge.sort(key=lambda x: x["cible_min_m"] - x["cumul_m"], reverse=True)

        restes = [j["reste_m"] for j in joueurs if j.get("reste_m")]
        reste_moyen = round(sum(restes) / len(restes)) if restes else None

        source = ("MANUEL" if oh["objectif_distance_m"] is not None
                  else ("INTELLIGENT" if oh["suggestion_moyenne_m"] is not None else None))

        return {
            "multi_equipes": oh["multi_equipes"],
            "effectif": {"nb_joueurs": len(joueurs)},
            "objectif_hebdo": {
                "source":               source,
                "objectif_manuel_m":    oh["objectif_distance_m"],
                "suggestion_moyenne_m": oh["suggestion_moyenne_m"],
                "nb_atteint":           oh["nb_atteint"],
                "nb_concernes":         oh["nb_concernes"],
                "reste_moyen_m":        reste_moyen,
                "meilleur":             oh["meilleur"],
            },
            "charge_semaine": {
                "nb_surcharge":  len(surcharge),
                "nb_souscharge": len(souscharge),
                "surcharge":     surcharge[:3],
                "souscharge":    souscharge[:3],
            },
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/equipe/simulation")
def post_simulation_seance(requete: SimulationSeanceRequete,
                           x_contexte_equipes: str | None = Header(default=None),
                           x_contexte_club: str | None = Header(default=None)):
    """
    Simulation « et si… » — scénario « une séance ». À partir d'une séance HYPOTHÉTIQUE (type +
    durée), projette pour chaque joueur la distance attendue (baseline m/min sur ce type de séance)
    et recalcule son ACWR en ajoutant cette distance à la charge aiguë → qui basculerait au-dessus
    du plafond si la séance avait lieu.

    POST parce qu'on envoie un corps, mais l'opération est en LECTURE SEULE : aucune séance n'est
    créée, aucune donnée n'est écrite. Le score de risque officiel n'est pas affecté.
    """
    try:
        with get_connection() as conn:
            cfg   = _load_config(conn)
            scope = _equipes_scope(x_contexte_equipes, x_contexte_club, conn)
            return _simulation_seance_data(conn, cfg, scope,
                                           requete.type_seance_id, requete.duree_minutes)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/equipe/derives")
def get_derives(x_contexte_equipes: str | None = Header(default=None),
                x_contexte_club: str | None = Header(default=None)):
    """
    Dérives lentes de l'effectif sur ~4 semaines, en TROIS axes séparés (pour une lecture globale
    de chacun) : volume (distance totale), charge haute intensité (distance ≥ 19 km/h) et ressenti
    (fatigue subjective composite). Par axe et par joueur : comparaison de la moyenne des 14 derniers
    jours vs les 14 précédents → dérive en hausse / en baisse au-delà d'un seuil. Indicateurs déjà
    agrégés (jamais de données brutes au LLM), consommés par la carte web/PWA et le debrief textuel.
    """
    SEUIL = 20.0   # % de variation à partir duquel on parle de dérive
    try:
        with get_connection() as conn:
            scope = _equipes_scope(x_contexte_equipes, x_contexte_club, conn)
            roster = _joueurs_resume(conn, scope)
            noms = {str(jid): f'{(prenom or "").strip()} {(nom or "").strip()}'.strip()
                    for (jid, nom, prenom, poste) in roster}
            ids = list(noms.keys())

            gps = {}   # jid -> (vol_recent, vol_ref, hi_recent, hi_ref)
            well = {}  # jid -> (w_recent, w_ref)  (composite, haut = plus de fatigue)
            if ids:
                gwhere = ["s.statut = 'REALISEE'", "s.date >= CURRENT_DATE - INTERVAL '28 days'",
                          "dg.joueur_id = ANY(%s)"]
                gparams: list = [ids]
                if scope:
                    gwhere.append("s.equipe_id = ANY(%s)"); gparams.append(scope)
                with conn.cursor() as cur:
                    cur.execute(f"""
                        SELECT dg.joueur_id,
                          SUM(CASE WHEN s.date >= CURRENT_DATE - INTERVAL '14 days' THEN dg.distance_totale_m ELSE 0 END),
                          SUM(CASE WHEN s.date <  CURRENT_DATE - INTERVAL '14 days' THEN dg.distance_totale_m ELSE 0 END),
                          SUM(CASE WHEN s.date >= CURRENT_DATE - INTERVAL '14 days' THEN COALESCE(dg.distance_19kmh_m,0) ELSE 0 END),
                          SUM(CASE WHEN s.date <  CURRENT_DATE - INTERVAL '14 days' THEN COALESCE(dg.distance_19kmh_m,0) ELSE 0 END)
                        FROM donnee_gps dg JOIN seance s ON s.id = dg.seance_id
                        WHERE {' AND '.join(gwhere)}
                        GROUP BY dg.joueur_id
                    """, gparams)
                    for jid, vr, vf, hr, hf in cur.fetchall():
                        gps[str(jid)] = (float(vr or 0), float(vf or 0), float(hr or 0), float(hf or 0))
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT joueur_id,
                          AVG(CASE WHEN date >= CURRENT_DATE - INTERVAL '14 days' THEN comp END),
                          AVG(CASE WHEN date <  CURRENT_DATE - INTERVAL '14 days' THEN comp END)
                        FROM (
                          SELECT joueur_id, date,
                                 ((11-sommeil)+(11-humeur)+(11-fatigue)+(11-douleur)+(11-stress))/5.0*10 AS comp
                          FROM wellness_quotidien
                          WHERE date >= CURRENT_DATE - INTERVAL '28 days' AND joueur_id = ANY(%s)
                        ) w
                        GROUP BY joueur_id
                    """, (ids,))
                    for jid, wr, wf in cur.fetchall():
                        well[str(jid)] = (float(wr) if wr is not None else None,
                                          float(wf) if wf is not None else None)

        def _drift(recent, ref):
            """(direction, pct) si dérive au-delà du seuil, sinon None. ref insuffisant → None."""
            if recent is None or ref is None or ref <= 0:
                return None
            pct = round((recent - ref) / ref * 100, 1)
            if pct >= SEUIL:  return ("hausse", pct)
            if pct <= -SEUIL: return ("baisse", pct)
            return ("stable", pct)

        def _axe(code, libelle, sens_hausse, valeur):
            hausse, baisse = [], []
            for jid in ids:
                d = _drift(*valeur(jid))
                if d is None or d[0] == "stable":
                    continue
                ligne = {"joueur_id": jid, "nom": noms.get(jid, "joueur"), "drift_pct": d[1]}
                (hausse if d[0] == "hausse" else baisse).append(ligne)
            hausse.sort(key=lambda x: x["drift_pct"], reverse=True)
            baisse.sort(key=lambda x: x["drift_pct"])
            return {
                "code": code, "libelle": libelle, "sens_hausse": sens_hausse,
                "nb_hausse": len(hausse), "nb_baisse": len(baisse),
                "hausse": hausse[:5], "baisse": baisse[:5],
            }

        axes = [
            _axe("volume", "Volume (distance totale)", "charge en hausse",
                 lambda jid: (gps.get(jid, (0, 0, 0, 0))[0], gps.get(jid, (0, 0, 0, 0))[1])),
            _axe("intensite", "Haute intensité (≥ 19 km/h)", "sollicitation intense en hausse",
                 lambda jid: (gps.get(jid, (0, 0, 0, 0))[2], gps.get(jid, (0, 0, 0, 0))[3])),
            _axe("wellness", "Ressenti (fatigue subjective)", "fatigue en hausse",
                 lambda jid: well.get(jid, (None, None))),
        ]
        return {
            "fenetre_jours": 28,
            "seuil_pct": SEUIL,
            "effectif": {"nb_joueurs": len(ids)},
            "axes": axes,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _joueurs_resume(conn, scope=None):
    """
    Périmètre du résumé d'équipe : l'effectif des saisons EN_COURS si la notion de
    saison/effectif existe ET est renseignée ; sinon repli LEGACY sur tous les joueurs
    actifs (non-breaking tant qu'aucune saison n'a été ouverte).
    `scope` (liste d'equipe_id du contexte) restreint aux équipes ciblées. Quand un
    scope est transmis, il fait TOUJOURS foi : résultat scopé même vide (club neuf sans
    effectif), jamais de repli plateforme entière.
    """
    try:
        with conn.cursor() as cur:
            extra = ""; params: list = []
            if scope:
                # Scope par l'équipe d'EFFECTIF (es.equipe_id), pas le cache j.equipe_id :
                # gère le multi-équipe et n'exclut jamais un joueur au cache périmé. Le JOIN
                # sur effectif_saison écarte nativement les fiches staff (aucun effectif).
                extra = " AND es.equipe_id = ANY(%s)"; params.append(scope)
            cur.execute(f"""
                SELECT j.id, j.nom, j.prenom, j.poste_principal
                FROM joueur j
                JOIN effectif_saison es ON es.joueur_id = j.id
                JOIN saison s ON s.id = es.saison_id AND s.statut = 'EN_COURS'
                WHERE j.statut != 'inactif'{extra}
                GROUP BY j.id, j.nom, j.prenom, j.poste_principal
                ORDER BY j.nom, j.prenom
            """, params)
            rows = cur.fetchall()
        if scope:
            return rows
        if rows:
            return rows
    except Exception:
        try: conn.rollback()
        except Exception: pass
        if scope:
            return []
    # Repli legacy (AUCUN scope transmis et aucun effectif renseigné) : la colonne
    # joueur.equipe_id n'existe plus (V51), on ne peut plus scoper par équipe ici
    # → tous les joueurs actifs (chemin pré-effectif only).
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, nom, prenom, poste_principal
            FROM joueur WHERE statut != 'inactif' ORDER BY nom, prenom
        """)
        return cur.fetchall()


@router.get("/equipe", response_model=List[ResumeJoueur])
def get_resume_equipe(x_date_simulee: str | None = Header(default=None),
                      x_contexte_equipes: str | None = Header(default=None),
                      x_contexte_club: str | None = Header(default=None)):
    date_ref = _parse_date_simulee(x_date_simulee)
    try:
        with get_connection() as conn:
            cfg = _load_config(conn)
            scope = _equipes_scope(x_contexte_equipes, x_contexte_club, conn)
            joueurs = _joueurs_resume(conn, scope)

            resultats = []
            for j in joueurs:
                joueur_id = UUID(str(j[0]))
                ctx = _contexte_joueur(joueur_id, cfg, conn, date_ref)
                readiness, readiness_date = _readiness_joueur(joueur_id, conn)

                # Champs de contexte communs (toujours renvoyés pour l'UI).
                commun = dict(
                    joueur_id=joueur_id, nom=j[1], prenom=j[2], poste=j[3],
                    readiness=readiness, readiness_date=readiness_date,
                    etat=ctx["etat"], periode_type=ctx["periode_type"],
                    periode_libelle=ctx["periode_libelle"], jours_inactif=ctx["jours_inactif"],
                    blessure_jours_restants=ctx["blessure_jours_restants"],
                )

                # Hors charge / inactif / blessé : aucune alerte calculée sur des données
                # périmées — indicateurs neutres, le joueur sort des « à surveiller ».
                if ctx["silence"]:
                    resultats.append(ResumeJoueur(
                        **commun,
                        score_risque=0.0, score_fatigue=0.0,
                        niveau_risque="FAIBLE", niveau_fatigue="NOMINAL",
                        acwr=None, charge_aigue_km=None, charge_chronique_km=None,
                        monotonie=None, sprint_niveau=None, sprint_message=None,
                    ))
                    continue

                risque  = _calcul_score_risque(joueur_id, cfg, conn, date_ref=date_ref,
                                               neutraliser_acwr=ctx["neutraliser_acwr"])
                fatigue = _calcul_fatigue(joueur_id, cfg, conn)
                sprint  = _sprint_neuromusculaire(joueur_id, cfg, conn)
                resultats.append(ResumeJoueur(
                    **commun,
                    score_risque=risque["score"],
                    score_fatigue=fatigue["score"],
                    niveau_risque=_niveau_risque(risque["score"]),
                    niveau_fatigue=fatigue["niveau"],
                    acwr=risque["acwr"],
                    charge_aigue_km=risque["charge_aigue_km"],
                    charge_chronique_km=risque["charge_chronique_km"],
                    monotonie=_monotonie_joueur(joueur_id, cfg, conn),
                    sprint_niveau=sprint["niveau"],
                    sprint_message=sprint["message"],
                    # Composition des deux scores : sans ça, /etat-effectif et les dashboards
                    # affichaient un chiffre sans pouvoir l'expliquer.
                    contributions=sorted(risque.get("contributions") or [],
                                         key=lambda c: c["points"], reverse=True),
                    signaux=fatigue.get("signaux") or [],
                    acwr_gps=risque.get("acwr_gps"),
                    acwr_rpe=risque.get("acwr_rpe"),
                    semaines_gps=risque.get("semaines_gps"),
                    semaines_rpe=risque.get("semaines_rpe"),
                    ecart_sources=risque.get("ecart_sources"),
                    provisoire=risque.get("provisoire"),
                ))

        return resultats
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
