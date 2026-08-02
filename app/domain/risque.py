"""
Risque de blessure — score explicable 0-100 puis probabilité à 7 jours.

Le score n'est PAS un pourcentage : il agrège des contributions pondérées (charge,
antécédents, poids) que `contributions` expose une à une pour que l'interface puisse
justifier le chiffre. La conversion en probabilité passe par des ancres calibrées, et
elle est volontairement COUPÉE quand aucune charge n'est disponible — afficher « ~5 % »
à côté de « données insuffisantes » revenait à inventer une mesure.

Le contexte du joueur (blessé, hors saison, inactif) neutralise l'alerte en amont :
c'est `_contexte_joueur` qui décide, pas ce module.
"""
from uuid import UUID
from datetime import date as _date

from app.domain.contexte import _contexte_joueur, _poids_a_date
from app.domain.charge import _charge_acwr_unifiee, _ecart_sources

# Ancres (score 0-100 → probabilité % de blessure à 7 jours). Mapping monotone,
# calibrable plus tard sur les blessures observées — AUCUN apprentissage ici.
_PROBA_ANCRES = [(0, 2), (20, 5), (30, 8), (45, 14), (60, 24), (80, 42), (100, 60)]


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

def _niveau_risque(score: float) -> str:
    if score < 30:
        return "FAIBLE"
    elif score < 60:
        return "MODERE"
    return "ELEVE"

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
