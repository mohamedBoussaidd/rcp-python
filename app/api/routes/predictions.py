from fastapi import APIRouter, HTTPException, Header
from uuid import UUID
from datetime import date as _date
from app.core.database import get_connection
from app.schemas.schemas import RisqueBlessure, NiveauFatigue, ResumeJoueur, ChargeCible
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
            # Saison au niveau CLUB (V37) : on remonte le club via l'équipe du joueur.
            # Saison EN_COURS du club (le cas échéant) + le club a-t-il des saisons ?
            cur.execute("""
                SELECT
                  j.equipe_id AS equipe_id,
                  (SELECT s.date_debut FROM saison s
                     JOIN equipe e ON e.club_id = s.club_id
                     WHERE e.id = j.equipe_id AND s.statut = 'EN_COURS'
                     ORDER BY s.date_debut DESC LIMIT 1) AS encours_debut,
                  (SELECT s.id FROM saison s
                     JOIN equipe e ON e.club_id = s.club_id
                     WHERE e.id = j.equipe_id AND s.statut = 'EN_COURS'
                     ORDER BY s.date_debut DESC LIMIT 1) AS encours_id,
                  EXISTS (SELECT 1 FROM saison s
                     JOIN equipe e ON e.club_id = s.club_id
                     WHERE e.id = j.equipe_id) AS a_saisons
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
      - chronique hebdo = SUM distances jours 8-35 / 4 semaines (mètres)
    `date_ref` permet de calculer à une date passée (tendance). Défaut = aujourd'hui.
    Renvoie (aigue_m, chronique_hebdo_m) ou None si pas de base chronique.
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
                             THEN dg.distance_totale_m ELSE 0 END) / %s AS charge_chronique_hebdo
                FROM donnee_gps dg
                JOIN seance s ON dg.seance_id = s.id
                WHERE dg.joueur_id = %s
                  AND s.date >= %s::date - INTERVAL '{jours_chronique} days'
                  AND s.date <= %s::date
            """, (ref, ref, ref, float(sem_chronique), str(joueur_id), ref, ref))
            row = cur.fetchone()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return None

    if not row or row[1] is None or float(row[1]) == 0:
        return None
    return (float(row[0] or 0), float(row[1]))


def _charge_rpe(joueur_id: UUID, conn, date_ref=None) -> tuple | None:
    """
    Charge interne (sRPE = RPE × durée, saisie joueur) « découplée » :
      - aiguë           = SUM charges 7 derniers jours
      - chronique hebdo = SUM charges jours 8-28 / 3 semaines
    Sert de source de repli quand le GPS manque (séances techniques, sans gilets).
    Renvoie (aigue, chronique_hebdo) ou None si pas de base chronique / table absente.
    """
    ref = date_ref or _date.today()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    SUM(CASE WHEN date >= %s::date - INTERVAL '7 days'
                             THEN charge ELSE 0 END) AS aigue,
                    SUM(CASE WHEN date >= %s::date - INTERVAL '28 days'
                             AND date  < %s::date - INTERVAL '7 days'
                             THEN charge ELSE 0 END) / 3.0 AS chronique_hebdo
                FROM rpe_seance
                WHERE joueur_id = %s
                  AND date >= %s::date - INTERVAL '28 days'
                  AND date <= %s::date
                  AND charge IS NOT NULL
            """, (ref, ref, ref, str(joueur_id), ref, ref))
            row = cur.fetchone()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return None

    if not row or row[1] is None or float(row[1]) == 0:
        return None
    return (float(row[0] or 0), float(row[1]))


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

    Renvoie : {source, acwr, aigue, chronique, unite, acwr_gps, acwr_rpe}.
    """
    gps = _charge_gps(joueur_id, cfg, conn, date_ref)
    rpe = _charge_rpe(joueur_id, conn, date_ref)

    acwr_gps = (gps[0] / gps[1]) if gps and gps[1] > 0 else None
    acwr_rpe = (rpe[0] / rpe[1]) if rpe and rpe[1] > 0 else None

    vide = {"source": None, "acwr": None, "aigue": None, "chronique": None,
            "unite": None, "acwr_gps": None, "acwr_rpe": None}

    if acwr_gps is not None and acwr_rpe is not None:
        w_g = float(cfg.get("poids_charge_gps", 0.6))
        w_r = float(cfg.get("poids_charge_rpe", 0.4))
        acwr = (w_g * acwr_gps + w_r * acwr_rpe) / (w_g + w_r)
        return {"source": "MIXTE", "acwr": round(acwr, 2),
                "aigue": round(gps[0] / 1000, 1), "chronique": round(gps[1] / 1000, 1),
                "unite": "km", "acwr_gps": round(acwr_gps, 2), "acwr_rpe": round(acwr_rpe, 2)}
    if acwr_gps is not None:
        return {"source": "GPS", "acwr": round(acwr_gps, 2),
                "aigue": round(gps[0] / 1000, 1), "chronique": round(gps[1] / 1000, 1),
                "unite": "km", "acwr_gps": round(acwr_gps, 2), "acwr_rpe": None}
    if acwr_rpe is not None:
        return {"source": "RPE", "acwr": round(acwr_rpe, 2),
                "aigue": round(rpe[0], 0), "chronique": round(rpe[1], 0),
                "unite": "sRPE", "acwr_gps": None, "acwr_rpe": round(acwr_rpe, 2)}
    return vide


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

    return {
        "score":               min(round(score, 1), 100.0),
        "acwr":                acwr,
        "charge_aigue_km":     charge["aigue"] if charge["unite"] == "km" else None,
        "charge_chronique_km": charge["chronique"] if charge["unite"] == "km" else None,
        "source":              charge["source"],
        "unite":               charge["unite"],
        "contributions":       contributions,
    }


def _charge_cible(joueur_id: UUID, cfg: dict, conn) -> dict:
    """
    Recommandation de charge pour la semaine à venir, individualisée.
    On part de la charge chronique hebdo (source unifiée GPS↔RPE) et on projette
    une fourchette par les bornes ACWR : sûre [0.8 ; 1.3], idéale ~1.05.
    Exprimée en km si GPS disponible, sinon en unités sRPE (repli).
    Renvoie {disponible, source, unite, ...} — disponible=False si pas de base chronique.
    """
    charge = _charge_acwr_unifiee(joueur_id, cfg, conn)
    chro   = charge["chronique"]
    if chro is None or chro <= 0:
        return {"disponible": False, "source": charge["source"], "unite": charge["unite"],
                "phrase": "Pas assez de données de charge pour recommander une cible."}

    acwr_min   = float(cfg.get("acwr_cible_min", 0.8))
    acwr_ideal = float(cfg.get("acwr_cible_ideal", 1.05))
    acwr_haute = float(cfg.get("acwr_cible_haute", 1.2))
    acwr_max   = float(cfg.get("acwr_cible_max", 1.3))
    unite = charge["unite"]
    arr = (lambda v: round(v, 1)) if unite == "km" else (lambda v: round(v))

    cible_min   = arr(chro * acwr_min)
    cible_ideal = arr(chro * acwr_ideal)
    cible_haute = arr(chro * acwr_haute)
    plafond     = arr(chro * acwr_max)

    phrase = (f"Charge cible semaine : {cible_min}–{cible_haute} {unite} "
              f"(idéal ~{cible_ideal}). Plafond à ne pas dépasser : {plafond} {unite}.")
    return {
        "disponible":  True,
        "source":      charge["source"],
        "unite":       unite,
        "chronique":   chro,
        "acwr_actuel": charge["acwr"],
        "cible_min":   cible_min,
        "cible_ideal": cible_ideal,
        "cible_haute": cible_haute,
        "plafond":     plafond,
        "phrase":      phrase,
    }


def _signal2_detail(joueur_id: UUID, types: tuple, label_groupe: str,
                    cfg: dict, conn) -> tuple:
    """
    Signal 2 enrichi — 3 sous-signaux sur les 10 dernières séances du groupe (≤ 60 jours) :
      A — m/min global          → fatigue générale
      B — vitesse max           → fatigue neuromusculaire explosive
      C — ratio dist >19 km/h   → fatigue neuromusculaire intensive
    Seuils lus depuis la configuration.
    """
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
            ORDER BY s.date DESC
            LIMIT 10
        """, (str(joueur_id), list(types)))
        rows = cur.fetchall()

    if len(rows) < 4:
        return 0, None, []

    sous_signaux = []

    s_mmin_prob = cfg.get("seuil_mmin_probable", 0.80)
    s_mmin_poss = cfg.get("seuil_mmin_possible", 0.88)
    s_vmax_prob = cfg.get("seuil_vmax_probable", 0.88)
    s_vmax_poss = cfg.get("seuil_vmax_possible", 0.94)
    s_hi_prob   = cfg.get("seuil_hi_probable",   0.75)
    s_hi_poss   = cfg.get("seuil_hi_possible",   0.85)

    # ── A : m/min global ──
    ratios_a = [float(r[0]) / float(r[1]) for r in rows]
    ra = sum(ratios_a[:2]) / 2
    ha = sum(ratios_a[2:]) / len(ratios_a[2:])
    if ha > 0:
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
                   f"({round(ra,1)} m/min, réf. {round(ha,1)})"
        })

    # ── B : vitesse max ──
    vmax_rows = [r for r in rows if r[2] is not None]
    if len(vmax_rows) >= 4:
        rb = sum(float(r[2]) for r in vmax_rows[:2]) / 2
        hb = sum(float(r[2]) for r in vmax_rows[2:]) / len(vmax_rows[2:])
        if hb > 0:
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
                       f"({round(rb,1)} km/h, réf. {round(hb,1)})"
            })

    # ── C : ratio dist >19 km/h / distance totale ──
    hi_rows = [r for r in rows if r[3] is not None and float(r[0]) > 0]
    if len(hi_rows) >= 4:
        ratios_c = [float(r[3]) / float(r[0]) for r in hi_rows]
        rc = sum(ratios_c[:2]) / 2
        hc = sum(ratios_c[2:]) / len(ratios_c[2:])
        if hc > 0:
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
                       f"({rc_pct}% vs {hc_pct}% de la dist.)"
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
    # Échelle de saisie : 1 = excellent → 5 = très mauvais pour TOUS les items.
    # Composite bien-être 0..100 (plus haut = mieux) : on inverse les 5 items.
    composite = round(((6 - sommeil) + (6 - humeur) + (6 - fatigue_i) + (6 - douleur) + (6 - stress)) / 5 * 20)

    # Items dégradés à signaler (haut = mauvais pour fatigue/douleur/stress ; bas = mauvais pour sommeil/humeur).
    soucis = []
    if fatigue_i >= 4: soucis.append("fatigue élevée")
    if douleur >= 4:   soucis.append("courbatures")
    if stress >= 4:    soucis.append("stress")
    if sommeil <= 2:   soucis.append("sommeil dégradé")
    if humeur <= 2:    soucis.append("humeur basse")
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
    ACWR sur la charge ressentie : aiguë (7 j) vs chronique hebdo (jours 8-28 / 3).
    Complète la charge GPS (utile notamment pour les séances sans GPS, ex. techniques).
    Renvoie (0, None) si données insuffisantes ou table absente.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    SUM(CASE WHEN date >= CURRENT_DATE - INTERVAL '7 days'
                             THEN charge ELSE 0 END) AS aigue,
                    SUM(CASE WHEN date >= CURRENT_DATE - INTERVAL '28 days'
                             AND date  < CURRENT_DATE - INTERVAL '7 days'
                             THEN charge ELSE 0 END) / 3.0 AS chronique_hebdo
                FROM rpe_seance
                WHERE joueur_id = %s
                  AND date >= CURRENT_DATE - INTERVAL '28 days'
                  AND charge IS NOT NULL
            """, (str(joueur_id),))
            row = cur.fetchone()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return 0, None

    if not row or row[1] is None or float(row[1]) == 0:
        return 0, None

    aigue     = float(row[0] or 0)
    chronique = float(row[1])
    if aigue <= 0:
        return 0, None

    ratio = aigue / chronique
    pct   = round((ratio - 1) * 100)
    seuil_prob = cfg.get("seuil_srpe_probable", 1.50)
    seuil_poss = cfg.get("seuil_srpe_possible", 1.30)

    if ratio >= seuil_prob:
        return 25, (f"charge ressentie (sRPE) +{pct}% vs habituel"
                    f" · type suggéré : surcharge subjective probable")
    elif ratio >= seuil_poss:
        return 12, (f"charge ressentie (sRPE) élevée +{pct}%"
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
    with conn.cursor() as cur:
        cur.execute("""
            SELECT ts.code, dg.distance_totale_m,
                   s.date >= CURRENT_DATE - INTERVAL '7 days' AS est_recent
            FROM donnee_gps dg
            JOIN seance s ON dg.seance_id = s.id
            JOIN type_seance ts ON s.type_seance_id = ts.id
            WHERE dg.joueur_id = %s
              AND s.date >= CURRENT_DATE - INTERVAL '28 days'
              AND dg.distance_totale_m > 0
        """, (str(joueur_id),))
        rows_charge = cur.fetchall()

    charge_7j  = sum(float(r[1]) * _poids_seance(r[0], cfg) for r in rows_charge if r[2])
    charge_21j = sum(float(r[1]) * _poids_seance(r[0], cfg) for r in rows_charge if not r[2])
    charge_chrono_hebdo = charge_21j / 3 if charge_21j > 0 else None

    s1_score  = 0
    s1_raison = None
    seuil_prob = cfg.get("seuil_surcharge_probable", 1.40)
    seuil_poss = cfg.get("seuil_surcharge_possible", 1.20)

    if charge_chrono_hebdo and charge_chrono_hebdo > 0 and charge_7j > 0:
        ratio_charge = charge_7j / charge_chrono_hebdo
        pct          = round((ratio_charge - 1) * 100)
        km_7j        = round(charge_7j / 1000, 1)
        km_normal    = round(charge_chrono_hebdo / 1000, 1)
        if ratio_charge >= seuil_prob:
            s1_score  = 45
            s1_raison = (
                f"surcharge hebdomadaire +{pct}% ({km_7j} km pondérés vs {km_normal} km normal)"
                f" · type suggéré : surcharge métabolique probable"
            )
        elif ratio_charge >= seuil_poss:
            s1_score  = 25
            s1_raison = (
                f"charge hebdomadaire élevée +{pct}% ({km_7j} km pondérés vs {km_normal} km normal)"
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

    return {"score": round(score, 1), "niveau": _niveau_fatigue(score), "raison": raison}


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
    # Tous les items 1=excellent..5=très mauvais → inversés pour « plus haut = mieux ».
    composite = round(((6 - sommeil) + (6 - humeur) + (6 - fatigue_i) + (6 - douleur) + (6 - stress)) / 5 * 20)
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
                "phrase": phrase, "facteur_dominant": None, "tendance": "STABLE", "source": None}

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

    if risque["acwr"] is None:
        phrase = "Données de charge insuffisantes pour estimer le risque."
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
                fatigue = {"score": 0.0, "niveau": "NOMINAL", "raison": raison}
            else:
                fatigue = _calcul_fatigue(joueur_id, cfg, conn)

        return NiveauFatigue(
            joueur_id=joueur_id,
            nom=joueur[1],
            prenom=joueur[2],
            score_fatigue=fatigue["score"],
            niveau=fatigue["niveau"],
            raison=fatigue["raison"],
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
                          x_contexte_equipes: str | None = Header(default=None),
                          x_contexte_club: str | None = Header(default=None)):
    """
    Charge collective (km) par semaine glissante sur les `semaines` dernières
    semaines (4, 8 ou 12). Index 0 = la plus ancienne, dernier = semaine en cours.
    """
    semaines = semaines if semaines in (4, 8, 12) else 4
    jours = semaines * 7
    try:
        with get_connection() as conn:
            scope = _equipes_scope(x_contexte_equipes, x_contexte_club, conn)
            extra = ""
            qp: list = [semaines, jours]
            if scope:
                extra = " AND s.equipe_id = ANY(%s)"; qp.append(scope)
            with conn.cursor() as cur:
                # bucket : 0 = semaine la plus ancienne … (semaines-1) = semaine en cours
                cur.execute(f"""
                    SELECT
                        %s - 1 - FLOOR((CURRENT_DATE - s.date) / 7)::int AS semaine_idx,
                        ROUND(SUM(dg.distance_totale_m) / 1000.0, 1) AS total_km
                    FROM donnee_gps dg
                    JOIN seance s ON dg.seance_id = s.id
                    JOIN joueur j ON j.id = dg.joueur_id
                    WHERE s.date >= CURRENT_DATE - (%s || ' days')::interval
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
                           s.objectif_distance_haute_intensite_m
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

            # Durée de référence de la séance = somme des durées des exercices
            # (override seance_exercice sinon valeur de l'exercice). Sert au prorata
            # de l'objectif d'équipe par joueur selon son temps de jeu réel.
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT SUM(COALESCE(se.duree_minutes, e.duree_minutes))
                    FROM seance_exercice se
                    JOIN exercice e ON e.id = se.exercice_id
                    WHERE se.seance_id = %s
                """, (str(seance_id),))
                ref_row = cur.fetchone()
            duree_reference = float(ref_row[0]) if ref_row and ref_row[0] else None

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

            sous_norme_pct = cfg.get("seuil_sous_norme_pct", 20.0)
            sur_norme_pct  = cfg.get("seuil_sur_norme_pct",  20.0)
            corr_pct_kg    = cfg.get("correction_surpoids_pct_par_kg",  2.0)
            corr_pct_max   = cfg.get("correction_surpoids_plafond_pct", 20.0)

            lignes = []
            for p in players:
                joueur_id     = p[0]
                poste         = _normaliser_poste(p[3] or "")
                dist_reelle   = float(p[4]) if p[4] is not None else None
                duree_reelle  = float(p[5]) if p[5] is not None else None
                poids_actuel, poids_cible = _poids_a_date(joueur_id, seance_date, conn)

                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT AVG(sub.ratio) FROM (
                            SELECT dg.distance_totale_m / NULLIF(dg.duree_minutes, 0) AS ratio
                            FROM donnee_gps dg
                            JOIN seance s ON dg.seance_id = s.id
                            WHERE dg.joueur_id = %s
                              AND s.type_seance_id = %s
                              AND dg.seance_id != %s
                              AND dg.duree_minutes > 0
                              AND dg.distance_totale_m > 0
                            ORDER BY s.date DESC
                            LIMIT 10
                        ) sub
                    """, (str(joueur_id), str(type_seance_id), str(seance_id)))
                    avg_row = cur.fetchone()

                avg_ratio    = float(avg_row[0]) if avg_row and avg_row[0] is not None else None
                dist_attendue = round(avg_ratio * duree_reelle, 0) if avg_ratio and duree_reelle else None

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
                    "distance_attendue":       dist_attendue,
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
            scope = _equipes_scope(x_contexte_equipes, x_contexte_club, conn)

            # Historique des ratios par (joueur, type), du plus récent au plus ancien.
            # Baseline d'une séance = moyenne des 10 plus récentes du même type, HORS séance
            # courante (même logique que le rapport par séance, sans correction météo).
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT dg.joueur_id, s.type_seance_id, dg.seance_id,
                           dg.distance_totale_m / NULLIF(dg.duree_minutes, 0) AS ratio
                    FROM donnee_gps dg
                    JOIN seance s ON s.id = dg.seance_id
                    WHERE dg.duree_minutes > 0 AND dg.distance_totale_m > 0
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


def _joueurs_resume(conn, scope=None):
    """
    Périmètre du résumé d'équipe : l'effectif des saisons EN_COURS si la notion de
    saison/effectif existe ET est renseignée ; sinon repli LEGACY sur tous les joueurs
    actifs (non-breaking tant qu'aucune saison n'a été ouverte).
    `scope` (liste d'equipe_id du contexte) restreint aux équipes ciblées.
    """
    try:
        with conn.cursor() as cur:
            extra = ""; params: list = []
            if scope:
                extra = " AND j.equipe_id = ANY(%s)"; params.append(scope)
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
        if rows:
            return rows
    except Exception:
        try: conn.rollback()
        except Exception: pass
    # Repli legacy
    with conn.cursor() as cur:
        extra = ""; params = []
        if scope:
            extra = " AND equipe_id = ANY(%s)"; params.append(scope)
        cur.execute(f"""
            SELECT id, nom, prenom, poste_principal
            FROM joueur WHERE statut != 'inactif'{extra} ORDER BY nom, prenom
        """, params)
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
                ))

        return resultats
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
