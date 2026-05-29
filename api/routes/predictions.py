from fastapi import APIRouter, HTTPException
from uuid import UUID
from datetime import date as _date
from api.database import get_connection
from api.models.schemas import RisqueBlessure, NiveauFatigue, ResumeJoueur
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


def _calcul_score_risque(joueur_id: UUID, cfg: dict, conn) -> float:
    """
    ACWR (Acute:Chronic Workload Ratio) avec fenêtres NON chevauchantes :
      - Charge aiguë  = SUM distances 7 derniers jours
      - Charge chronique hebdo = SUM distances jours 8-28 / 3 semaines
    ACWR élevé (>1.3) indique un risque de blessure.
    Bonus poids et blessures récentes configurables.
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

    charge_aigue      = float(row[0] or 0)
    charge_chronique  = float(row[1])
    blessures_recentes = int(row[2] or 0)

    acwr = charge_aigue / charge_chronique
    if acwr < 0.8:
        score = 15.0
    elif acwr <= 1.3:
        score = 20.0 + (acwr - 0.8) * 20
    else:
        score = 30.0 + min((acwr - 1.3) * 50, 50.0)

    score += blessures_recentes * 15

    poids, poids_cible = _poids_a_date(joueur_id, _date.today(), conn)
    if poids is not None and poids_cible is not None:
        ecart_kg = poids - poids_cible
        if ecart_kg > 0:
            pts_par_kg = cfg.get("correction_surpoids_pts_par_kg", 5.0)
            plafond    = cfg.get("correction_surpoids_plafond_pts", 20.0)
            score += min(ecart_kg * pts_par_kg, plafond)

    return min(round(score, 1), 100.0)


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


def _bonus_blessure(joueur_id: UUID, cfg: dict, conn) -> tuple:
    """Bonus si blessure récente — fenêtre et score configurables."""
    fenetre = int(cfg.get("fenetre_blessure_fatigue_jours", 56))
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT COUNT(*)
            FROM blessure
            WHERE joueur_id = %s
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

    # ── Bonus blessure ──
    b_score, b_raison = _bonus_blessure(joueur_id, cfg, conn)

    # ── Bonus congestion ──
    c_score, c_raison = _bonus_congestion(joueur_id, cfg, conn)

    score = min(s1_score + s2_score + s3_score + s4_score + b_score + c_score, 100.0)

    # ── Message ──
    parties = [r for r in [s1_raison, s2_raison, s3_raison, s4_raison, b_raison, c_raison] if r]
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

            cfg   = _load_config(conn)
            score = _calcul_score_risque(joueur_id, cfg, conn)

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

            cfg     = _load_config(conn)
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
                    JOIN joueur j ON j.id = dg.joueur_id
                    WHERE s.date >= CURRENT_DATE - INTERVAL '28 days'
                      AND j.statut != 'inactif'
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
            cfg = _load_config(conn)

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
            type_code      = seance[2]
            seance_date    = seance[1]

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
                })

        return {
            "seance_id":    str(seance_id),
            "date":         str(seance[1]),
            "type_code":    type_code,
            "type_libelle": seance[3],
            "nb_joueurs":   len(lignes),
            "lignes":       lignes,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/equipe", response_model=List[ResumeJoueur])
def get_resume_equipe():
    try:
        with get_connection() as conn:
            cfg = _load_config(conn)

            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, nom, prenom, poste_principal
                    FROM joueur
                    WHERE statut != 'inactif'
                    ORDER BY nom, prenom
                """)
                joueurs = cur.fetchall()

            resultats = []
            for j in joueurs:
                joueur_id    = UUID(str(j[0]))
                score_risque = _calcul_score_risque(joueur_id, cfg, conn)
                fatigue      = _calcul_fatigue(joueur_id, cfg, conn)
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
