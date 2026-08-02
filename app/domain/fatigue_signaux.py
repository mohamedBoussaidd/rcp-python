"""
Les signaux élémentaires du score de fatigue.

Chaque fonction renvoie `(points, raison)` et ne connaît que son propre domaine : c'est
`fatigue.py` qui les combine et sature le total. Un signal SANS DONNÉES vaut 0 — un
joueur peu suivi paraît donc nominal, ce qui est documenté dans /methodologie et n'est
pas un défaut de calcul.

⚠ Chaque raison contient le littéral « · type suggéré : » qui sépare le FAIT MESURÉ de
l'ÉTIQUETTE physiologique ; `fatigue.py` découpe dessus (constante MARQUEUR_TYPE). Ces
littéraux sont écrits en dur ici : modifier la constante sans les modifier ne casse rien
mais l'étiquette cesserait d'être isolée, en silence.
"""
from uuid import UUID
from datetime import date as _date

from app.core.config import _poids_seance, TYPES_MATCH, TYPES_INTENSIF
from app.domain.temps import _lundi
from app.domain.charge import _charge_rpe   # _signal_srpe délègue : une seule source de vérité


def _signal2_detail(joueur_id: UUID, types: tuple, label_groupe: str,
                    cfg: dict, conn, date_ref=None) -> tuple:
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
    ref     = date_ref or _date.today()

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
              AND s.date >= %s::date - INTERVAL '60 days'
              AND s.date <= %s::date
            ORDER BY s.date DESC, dg.id DESC
            LIMIT %s
        """, (str(joueur_id), list(types), ref, ref, limite))
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

def _calcul_signal3(joueur_id: UUID, cfg: dict, conn, date_ref=None) -> tuple:
    """
    Signal 3 — Indice de monotonie Foster sur 8 semaines glissantes.
    Monotonie = moyenne(charges hebdo) / écart-type(charges hebdo)
    """
    today = date_ref or _date.today()
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

def _calcul_signal4(joueur_id: UUID, cfg: dict, conn, date_ref=None) -> tuple:
    """
    Signal 4 — Espacement insuffisant entre séances haute intensité.
    """
    ref = date_ref or _date.today()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT ts.code, s.date
            FROM donnee_gps dg
            JOIN seance s ON dg.seance_id = s.id
            JOIN type_seance ts ON s.type_seance_id = ts.id
            WHERE dg.joueur_id = %s
              AND ts.code = ANY(%s)
              AND s.date >= %s::date - INTERVAL '28 days'
              AND s.date <= %s::date
              AND dg.distance_totale_m > 0
            ORDER BY s.date ASC
        """, (str(joueur_id), ['MATCH', 'MATCH_AMICAL', 'INTENSIF'], ref, ref))
        rows_hi = cur.fetchall()

    with conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(DISTINCT s.date)
            FROM donnee_gps dg
            JOIN seance s ON dg.seance_id = s.id
            WHERE dg.joueur_id = %s
              AND s.date >= %s::date - INTERVAL '14 days'
              AND s.date <= %s::date
              AND dg.distance_totale_m > 0
        """, (str(joueur_id), ref, ref))
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

def _signal_wellness(joueur_id: UUID, cfg: dict, conn, date_ref=None) -> tuple:
    """
    Signal wellness — ressenti subjectif récent (indice de Hooper, saisie joueur).
    Score de bien-être 0..100 (items négatifs inversés ; plus haut = mieux) calculé
    sur la dernière saisie (≤ 3 jours). Un score bas augmente la fatigue.
    Renvoie (0, None) si pas de saisie récente ou si la table n'existe pas encore.
    """
    ref = date_ref or _date.today()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT sommeil, fatigue, douleur, stress, humeur
                FROM wellness_quotidien
                WHERE joueur_id = %s
                  AND date >= %s::date - INTERVAL '3 days'
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

def _signal_srpe(joueur_id: UUID, cfg: dict, conn, date_ref=None) -> tuple:
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
    charge = _charge_rpe(joueur_id, conn, date_ref, cfg)
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

def _bonus_blessure(joueur_id: UUID, cfg: dict, conn, date_ref=None) -> tuple:
    """Bonus si blessure NON soldée récente — fenêtre et score configurables.
    Les blessures RETABLI sont exclues : une blessure rétablie ne doit pas maintenir
    une alerte de fatigue pendant des semaines après le retour du joueur."""
    fenetre = int(cfg.get("fenetre_blessure_fatigue_jours", 56))
    ref     = date_ref or _date.today()
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT COUNT(*)
            FROM blessure
            WHERE joueur_id = %s
              AND statut != 'RETABLI'
              AND date_blessure >= %s::date - INTERVAL '{fenetre} days'
              AND date_blessure <= %s::date
        """, (str(joueur_id), ref, ref))
        row = cur.fetchone()

    nb = int(row[0]) if row else 0
    if nb == 0:
        return 0, None

    pts = int(cfg.get("bonus_blessure_pts", 20))
    return pts, f"{nb} blessure(s) récente(s) (<{fenetre//7} sem.) · type suggéré : risque de rechute"

def _bonus_congestion(joueur_id: UUID, cfg: dict, conn, date_ref=None) -> tuple:
    """Bonus si congestion de matchs — seuils configurables."""
    ref = date_ref or _date.today()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*)
            FROM donnee_gps dg
            JOIN seance s ON dg.seance_id = s.id
            JOIN type_seance ts ON s.type_seance_id = ts.id
            WHERE dg.joueur_id = %s
              AND ts.code = ANY(%s)
              AND s.date >= %s::date - INTERVAL '15 days'
              AND s.date <= %s::date
              AND dg.distance_totale_m > 0
        """, (str(joueur_id), ['MATCH', 'MATCH_AMICAL'], ref, ref))
        row = cur.fetchone()

    nb        = int(row[0]) if row else 0
    seuil_prob = int(cfg.get("seuil_congestion_probable", 4))
    seuil_poss = int(cfg.get("seuil_congestion_possible", 3))

    if nb >= seuil_prob:
        return 20, f"{nb} matchs en 15j · type suggéré : fatigue cumulative probable"
    elif nb >= seuil_poss:
        return 10, f"{nb} matchs en 15j · type suggéré : fatigue cumulative possible"
    return 0, None
