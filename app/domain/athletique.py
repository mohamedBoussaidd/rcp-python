"""
Indicateurs athlétiques individuels : fraîcheur, monotonie, capacité de vitesse.

Trois lectures indépendantes du score de fatigue, exposées telles quelles sur la fiche
joueur et /etat-effectif :
  - readiness  : composite Hooper 0-100 (bien-être déclaré, convention 1=bon → 10=mauvais)
  - monotonie  : indice de Foster — une charge trop RÉGULIÈRE est un facteur de risque
  - sprint     : marqueur neuromusculaire, volontairement NON diagnostique (message
                 d'orientation, jamais une conclusion médicale)
"""
from uuid import UUID
from datetime import date as _date

from app.core.config import _poids_seance
from app.domain.temps import _lundi


def _readiness_joueur(joueur_id: UUID, conn, date_ref=None) -> tuple:
    """
    Readiness = dernier composite de bien-être (indice de Hooper, saisie joueur),
    0..100, plus haut = mieux. Fenêtre de 7 jours pour rester informatif sur le
    dashboard. Renvoie (composite|None, date_iso|None).

    `date_ref` (date simulée) ancre la fenêtre à une autre date ; la borne HAUTE est
    indispensable, sans elle un voyage dans la saison lirait des saisies postérieures.
    """
    ref = date_ref or _date.today()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT sommeil, fatigue, douleur, stress, humeur, date
                FROM wellness_quotidien
                WHERE joueur_id = %s
                  AND date >= %s::date - INTERVAL '7 days'
                  AND date <= %s::date
                ORDER BY date DESC
                LIMIT 1
            """, (str(joueur_id), ref, ref))
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

def _monotonie_joueur(joueur_id: UUID, cfg: dict, conn, date_ref=None) -> float | None:
    """
    Indice de monotonie de Foster (8 semaines glissantes) — valeur brute.
    Monotonie = moyenne(charges hebdo pondérées) / écart-type(charges hebdo).
    Renvoie None si données insuffisantes. Isolé du scoring de fatigue.

    `date_ref` (date simulée) déplace les 8 semaines ; la même référence sert au
    découpage hebdomadaire côté Python, sinon les deux se désaccorderaient.
    """
    today = date_ref or _date.today()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT ts.code, dg.distance_totale_m, s.date
                FROM donnee_gps dg
                JOIN seance s ON dg.seance_id = s.id
                JOIN type_seance ts ON s.type_seance_id = ts.id
                WHERE dg.joueur_id = %s
                  AND s.date >= %s::date - INTERVAL '56 days'
                  AND s.date <= %s::date
                  AND dg.distance_totale_m > 0
            """, (str(joueur_id), today, today))
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

def _sprint_neuromusculaire(joueur_id: UUID, cfg: dict, conn, date_ref=None) -> dict:
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
    ref = date_ref or _date.today()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT s.date, dg.duree_minutes, dg.vitesse_max_kmh, dg.distance_sprint_28kmh_m
                FROM donnee_gps dg
                JOIN seance s ON dg.seance_id = s.id
                JOIN type_seance ts ON s.type_seance_id = ts.id
                WHERE dg.joueur_id = %s
                  AND ts.code = ANY(%s)
                  AND s.date >= %s::date - INTERVAL '35 days'
                  AND s.date <= %s::date
                  AND dg.distance_totale_m > 0
                  AND dg.duree_minutes > 0
                ORDER BY s.date DESC
            """, (str(joueur_id), ['MATCH', 'MATCH_AMICAL', 'INTENSIF'], ref, ref))
            rows = cur.fetchall()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return {"niveau": None, "message": None}

    today = ref

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
