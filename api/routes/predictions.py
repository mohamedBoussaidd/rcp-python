from fastapi import APIRouter, HTTPException
from uuid import UUID
from api.database import get_connection
from api.models.schemas import RisqueBlessure, NiveauFatigue, ResumeJoueur
from typing import List

router = APIRouter()

# Ratios minimum par poste (m/min) — valables pour MATCH et MATCH_AMICAL uniquement
RATIO_OBJECTIF: dict[str, dict[str, float]] = {
    "MATCH": {
        "attaquant":           100.0,
        "avant_centre":        100.0,
        "ailier_droit":        105.0,
        "ailier_gauche":       105.0,
        "milieu_offensif":     108.0,
        "milieu_central":      110.0,
        "milieu_defensif":     108.0,
        "lateral_droit":       105.0,
        "lateral_gauche":      105.0,
        "defenseur_central":    95.0,
        "gardien":              55.0,
    },
    "MATCH_AMICAL": {
        "attaquant":           100.0,
        "avant_centre":        100.0,
        "ailier_droit":        105.0,
        "ailier_gauche":       105.0,
        "milieu_offensif":     108.0,
        "milieu_central":      110.0,
        "milieu_defensif":     108.0,
        "lateral_droit":       105.0,
        "lateral_gauche":      105.0,
        "defenseur_central":    95.0,
        "gardien":              55.0,
    },
}


def _calcul_score_risque(joueur_id: UUID, conn) -> float:
    """
    ACWR (Acute:Chronic Workload Ratio) avec fenêtres NON chevauchantes :
      - Charge aiguë  = SUM distances 7 derniers jours
      - Charge chronique hebdo = SUM distances jours 8-28 / 3 semaines
    ACWR élevé (>1.3) indique un risque de blessure.
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                SUM(CASE WHEN s.date >= CURRENT_DATE - INTERVAL '7 days'
                         THEN dg.distance_totale_m ELSE 0 END) AS charge_aigue,
                SUM(CASE WHEN s.date >= CURRENT_DATE - INTERVAL '28 days'
                         AND s.date  < CURRENT_DATE - INTERVAL '7 days'
                         THEN dg.distance_totale_m ELSE 0 END) / 3.0 AS charge_chronique_hebdo,
                COUNT(CASE WHEN b.date_blessure >= CURRENT_DATE - INTERVAL '90 days'
                           THEN 1 END) AS blessures_recentes
            FROM donnee_gps dg
            JOIN seance s ON dg.seance_id = s.id
            LEFT JOIN blessure b ON b.joueur_id = dg.joueur_id
            WHERE dg.joueur_id = %s
              AND s.date >= CURRENT_DATE - INTERVAL '28 days'
        """, (str(joueur_id),))
        row = cur.fetchone()

    if not row or row[1] is None or float(row[1]) == 0:
        return 20.0

    charge_aigue = float(row[0] or 0)
    charge_chronique = float(row[1])
    blessures_recentes = int(row[2] or 0)

    acwr = charge_aigue / charge_chronique
    if acwr < 0.8:
        score = 15.0
    elif acwr <= 1.3:
        score = 20.0 + (acwr - 0.8) * 20
    else:
        score = 30.0 + min((acwr - 1.3) * 50, 50.0)

    score += blessures_recentes * 15
    return min(round(score, 1), 100.0)


# Poids par type de séance pour le calcul de charge hebdomadaire
POIDS_TYPE: dict[str, float] = {
    "MATCH":        1.00,
    "MATCH_AMICAL": 1.00,
    "INTENSIF":     0.85,
    "FORCE":        0.70,
    "TECHNIQUE":    0.60,
    "PRE_MATCH":    0.50,
    "REPRISE":      0.30,
}

# Groupes de types pour le Signal 2 (comparés séparément)
TYPES_MATCH      = ("MATCH", "MATCH_AMICAL")
TYPES_INTENSIF   = ("INTENSIF",)


def _signal2_groupe(joueur_id: UUID, types: tuple, label: str, conn) -> tuple:
    """Signal 2 pour un groupe de types de séances homogènes."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT dg.distance_totale_m, dg.duree_minutes
            FROM donnee_gps dg
            JOIN seance s ON dg.seance_id = s.id
            JOIN type_seance ts ON s.type_seance_id = ts.id
            WHERE dg.joueur_id = %s
              AND ts.code = ANY(%s)
              AND dg.distance_totale_m > 0
              AND dg.duree_minutes > 0
            ORDER BY s.date DESC
            LIMIT 10
        """, (str(joueur_id), list(types)))
        rows = cur.fetchall()

    if len(rows) < 4:
        return 0, None

    ratios = [float(r[0]) / float(r[1]) for r in rows]
    avg_recent = sum(ratios[:2]) / 2
    avg_histo  = sum(ratios[2:]) / len(ratios[2:])

    if avg_histo <= 0:
        return 0, None

    ratio_perf  = avg_recent / avg_histo
    pct_baisse  = round((1 - ratio_perf) * 100)
    mmin_recent = round(avg_recent, 1)
    mmin_histo  = round(avg_histo, 1)

    if ratio_perf <= 0.80:
        return 55, (
            f"baisse significative sur les 2 dernières séances {label} (-{pct_baisse}% d'intensité) "
            f"— {mmin_recent} m/min en moyenne contre {mmin_histo} m/min habituellement"
        )
    elif ratio_perf <= 0.88:
        return 30, (
            f"légère baisse sur les 2 dernières séances {label} (-{pct_baisse}% d'intensité) "
            f"— {mmin_recent} m/min en moyenne contre {mmin_histo} m/min habituellement"
        )
    return 0, None


def _calcul_fatigue(joueur_id: UUID, conn) -> dict:
    """
    Deux signaux combinés :
      Signal 1 — Charge hebdomadaire pondérée vs semaine normale (toutes séances)
      Signal 2 — Baisse de performance sur les séances intensives (MATCH/INTENSIF)
    Retourne score (0-100), niveau et une phrase explicative.
    """
    # ── Signal 1 : charge pondérée 7j vs moyenne hebdo 28j ──
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

    charge_7j   = sum(float(r[1]) * POIDS_TYPE.get(r[0], 0.60) for r in rows_charge if r[2])
    charge_21j  = sum(float(r[1]) * POIDS_TYPE.get(r[0], 0.60) for r in rows_charge if not r[2])
    charge_chrono_hebdo = charge_21j / 3 if charge_21j > 0 else None

    s1_score = 0
    s1_raison = None
    if charge_chrono_hebdo and charge_chrono_hebdo > 0 and charge_7j > 0:
        ratio_charge = charge_7j / charge_chrono_hebdo
        pct = round((ratio_charge - 1) * 100)
        km_7j     = round(charge_7j / 1000, 1)
        km_normal = round(charge_chrono_hebdo / 1000, 1)
        if ratio_charge >= 1.40:
            s1_score = 45
            s1_raison = (
                f"surcharge hebdomadaire importante (+{pct}% vs semaine normale) "
                f"— {km_7j} km pondérés cette semaine contre {km_normal} km habituellement"
            )
        elif ratio_charge >= 1.20:
            s1_score = 25
            s1_raison = (
                f"charge hebdomadaire élevée (+{pct}% vs semaine normale) "
                f"— {km_7j} km pondérés cette semaine contre {km_normal} km habituellement"
            )

    # ── Signal 2 : baisse d'intensité (m/min) — MATCH et INTENSIF séparément ──
    s2_score_match,    s2_raison_match    = _signal2_groupe(joueur_id, TYPES_MATCH,    "de match",                conn)
    s2_score_intensif, s2_raison_intensif = _signal2_groupe(joueur_id, TYPES_INTENSIF, "d'entraînement intensif", conn)

    if s2_score_match >= s2_score_intensif:
        s2_score, s2_raison = s2_score_match, s2_raison_match
    else:
        s2_score, s2_raison = s2_score_intensif, s2_raison_intensif

    score = min(s1_score + s2_score, 100.0)

    # ── Phrase explicative ──
    raisons = [r for r in [s1_raison, s2_raison] if r]
    if raisons:
        raison = "Détecté : " + " et ".join(raisons) + "."
    elif not rows_charge:
        raison = "Données insuffisantes pour l'analyse."
    else:
        raison = "Charge normale, aucune baisse de performance détectée."

    return {"score": round(score, 1), "niveau": _niveau_fatigue(score), "raison": raison}


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


@router.get("/risque/{joueur_id}", response_model=RisqueBlessure)
def get_risque_blessure(joueur_id: UUID):
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

            score = _calcul_score_risque(joueur_id, conn)

        return RisqueBlessure(
            joueur_id=joueur_id,
            nom=joueur[1],
            prenom=joueur[2],
            score_risque=score,
            niveau=_niveau_risque(score),
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/fatigue/{joueur_id}", response_model=NiveauFatigue)
def get_fatigue(joueur_id: UUID):
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

            fatigue = _calcul_fatigue(joueur_id, conn)

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


@router.get("/charge-collective")
def get_charge_collective():
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        CASE
                            WHEN s.date >= CURRENT_DATE - INTERVAL '7 days'  THEN 3
                            WHEN s.date >= CURRENT_DATE - INTERVAL '14 days' THEN 2
                            WHEN s.date >= CURRENT_DATE - INTERVAL '21 days' THEN 1
                            ELSE 0
                        END AS semaine_idx,
                        ROUND(SUM(dg.distance_totale_m) / 1000.0, 1) AS total_km
                    FROM donnee_gps dg
                    JOIN seance s ON dg.seance_id = s.id
                    WHERE s.date >= CURRENT_DATE - INTERVAL '28 days'
                    GROUP BY 1
                    ORDER BY 1
                """)
                rows = cur.fetchall()

        data = [0.0, 0.0, 0.0, 0.0]
        for row in rows:
            data[int(row[0])] = float(row[1])

        return {"labels": ["S-4", "S-3", "S-2", "S-1"], "data": data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/seance/{seance_id}/rapport")
def get_rapport_seance(seance_id: UUID):
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT s.id, s.date, ts.code, ts.libelle, s.type_seance_id
                    FROM seance s
                    JOIN type_seance ts ON ts.id = s.type_seance_id
                    WHERE s.id = %s
                """, (str(seance_id),))
                seance = cur.fetchone()

            if not seance:
                raise HTTPException(status_code=404, detail="Séance introuvable")

            type_seance_id = seance[4]
            type_code = seance[2]

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

            lignes = []
            for p in players:
                joueur_id = p[0]
                poste = p[3] or ""
                dist_reelle = float(p[4]) if p[4] is not None else None
                duree_reelle = float(p[5]) if p[5] is not None else None

                # Ratio moyen historique (m/min) sur les 10 dernières séances du même type
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

                avg_ratio = float(avg_row[0]) if avg_row and avg_row[0] is not None else None

                # Distance attendue = ratio historique × durée réelle de cette séance
                dist_attendue = round(avg_ratio * duree_reelle, 0) if avg_ratio and duree_reelle else None

                delta_m = delta_pct = None
                statut = "SANS_BASELINE"

                if dist_reelle is not None and dist_attendue and dist_attendue > 0:
                    delta_m = round(dist_reelle - dist_attendue, 0)
                    delta_pct = round((delta_m / dist_attendue) * 100, 1)
                    statut = "SOUS_NORME" if delta_pct < -20 else ("SUR_NORME" if delta_pct > 20 else "DANS_NORME")

                # Objectif par poste (indicatif — MATCH et MATCH_AMICAL uniquement)
                objectif_m = None
                ratio_objectif = None
                atteint_objectif = None
                ratios_poste = RATIO_OBJECTIF.get(type_code, {})
                if poste in ratios_poste and duree_reelle:
                    ratio_objectif = ratios_poste[poste]
                    objectif_m = round(ratio_objectif * duree_reelle, 0)
                    atteint_objectif = dist_reelle >= objectif_m if dist_reelle is not None else None

                lignes.append({
                    "joueur_id":         str(joueur_id),
                    "nom":               p[1],
                    "prenom":            p[2],
                    "poste":             poste,
                    "duree_minutes":     int(duree_reelle) if duree_reelle else None,
                    "distance_reelle":   dist_reelle,
                    "distance_attendue": dist_attendue,
                    "ratio_reel":        round(dist_reelle / duree_reelle, 1) if dist_reelle and duree_reelle else None,
                    "delta_m":           delta_m,
                    "delta_pct":         delta_pct,
                    "statut":            statut,
                    "vitesse_max":       float(p[6]) if p[6] is not None else None,
                    "nb_sprints":        int(p[7])   if p[7] is not None else None,
                    "objectif_m":        objectif_m,
                    "ratio_objectif":    ratio_objectif,
                    "atteint_objectif":  atteint_objectif,
                })

        return {
            "seance_id":   str(seance_id),
            "date":        str(seance[1]),
            "type_code":   type_code,
            "type_libelle": seance[3],
            "nb_joueurs":  len(lignes),
            "lignes":      lignes,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/equipe", response_model=List[ResumeJoueur])
def get_resume_equipe():
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, nom, prenom, poste_principal
                    FROM joueur
                    WHERE statut = 'actif'
                    ORDER BY nom, prenom
                """)
                joueurs = cur.fetchall()

            resultats = []
            for j in joueurs:
                joueur_id = UUID(str(j[0]))
                score_risque  = _calcul_score_risque(joueur_id, conn)
                fatigue       = _calcul_fatigue(joueur_id, conn)
                resultats.append(ResumeJoueur(
                    joueur_id=joueur_id,
                    nom=j[1],
                    prenom=j[2],
                    poste=j[3],
                    score_risque=score_risque,
                    score_fatigue=fatigue["score"],
                    niveau_risque=_niveau_risque(score_risque),
                    niveau_fatigue=fatigue["niveau"],
                ))

        return resultats
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
