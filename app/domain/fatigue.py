"""
Agrégation du score de fatigue à partir des signaux élémentaires.

Le score est une SOMME saturée à 100 de signaux de poids inégaux (S1 45 · S2 55 · S3 25 ·
S4 40 · Hooper 25 · sRPE 25 · blessure 20 · congestion 20). Les niveaux coupent à 30 et 60.

⚠ Le signal 1 recalcule ici sa propre charge hebdomadaire de référence, avec le même
diviseur adaptatif que `charge.py` mais une pondération PAR TYPE DE SÉANCE que les
fonctions de charge n'appliquent pas — c'est ce qui interdit de les fusionner. Les trois
endroits qui calculent une charge de référence doivent être corrigés ENSEMBLE.
"""
from uuid import UUID
from datetime import date as _date

from app.core.config import _poids_seance, TYPES_MATCH, TYPES_INTENSIF
from app.domain.temps import _lundi
from app.domain.fatigue_signaux import (_signal2_detail, _calcul_signal3, _calcul_signal4,
                                        _signal_wellness, _signal_srpe,
                                        _bonus_blessure, _bonus_congestion)

# Séparateur entre le FAIT MESURÉ et l'ÉTIQUETTE physiologique dans les raisons de fatigue.
# `_calcul_fatigue` s'en sert pour découper chaque signal en deux champs exploitables par le
# front. ⚠ Les producteurs de raisons (`_signal2_detail`, `_calcul_signal3/4`, `_signal_wellness`,
# `_signal_srpe`, `_bonus_blessure`, `_bonus_congestion` et le signal 1) écrivent ce même littéral
# dans leurs f-strings : le modifier ici sans le modifier là-bas ne casse rien mais l'étiquette
# resterait collée au fait au lieu d'être isolée.
MARQUEUR_TYPE = " · type suggéré : "


def _plafonner_charges(s1_score, sr_score):
    """
    Empêche la même surcharge d'être comptée deux fois.

    Le signal 1 mesure la surcharge au GPS (jusqu'à 45 points), le signal sRPE la mesure au
    ressenti (jusqu'à 25). Chez un joueur qui dispose des DEUX sources, un unique épisode de
    surcharge peut déclencher les deux et peser 70 points sur 100 — non parce qu'il y a deux
    problèmes, mais parce qu'il y a deux capteurs. On ne retient donc que le plus fort.

    Le second n'est pas effacé : il reste affiché à 0 point (cf. `_marquer_absorbe`), pour que
    le staff voie que les deux sources concordent — la concordance est une information, elle ne
    doit simplement pas gonfler le score.

    Renvoie (s1, sr, absorbe) où `absorbe` vaut 'srpe' | 'gps' | None.

    ⚠ Sur les données de mise au point (2026-08-02), le cas ne se produisait JAMAIS : sur
    14 joueurs disposant des deux sources, aucun ne déclenchait les deux signaux, et l'écart
    absolu moyen entre les deux ACWR atteignait 0.37. Ce garde-fou est donc préventif et n'a
    pas pu être validé sur des données réelles — d'où sa forme de fonction pure, testable seule.
    """
    if s1_score > 0 and sr_score > 0:
        if s1_score >= sr_score:
            return s1_score, 0, "srpe"
        return 0, sr_score, "gps"
    return s1_score, sr_score, None


def _marquer_absorbe(texte):
    """Signale une raison dont les points ont été absorbés par l'autre source de charge.
    La mention est insérée AVANT `MARQUEUR_TYPE` pour ne pas polluer l'étiquette physiologique."""
    if not texte:
        return texte
    fait, sep, type_suggere = texte.partition(MARQUEUR_TYPE)
    return f"{fait} — concordant avec l'autre source de charge, non recompté{sep}{type_suggere}"


def _calcul_fatigue(joueur_id: UUID, cfg: dict, conn, date_ref=None) -> dict:
    """
    Signal 1 — Charge hebdomadaire pondérée vs semaine normale
    Signal 2 — Baisse de performance GPS sur MATCH/INTENSIF
    Signal 3 — Indice de monotonie Foster (8 semaines)
    Signal 4 — Espacement insuffisant entre séances haute intensité
    Bonus  B — Blessure récente
    Bonus  C — Congestion de matchs
    Tous les seuils sont lus depuis la configuration.

    `date_ref` (date simulée) est propagée à TOUS les signaux : n'en ancrer qu'une partie
    donnerait un score composite mélangeant deux dates, plus trompeur que pas de simulation
    du tout. L'en-tête X-Date-Simulee arrivait pourtant jusqu'ici depuis toujours, il était
    simplement lu par la route puis abandonné.
    """
    ref = date_ref or _date.today()

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
                   s.date >= %s::date - INTERVAL '7 days' AS est_recent
            FROM donnee_gps dg
            JOIN seance s ON dg.seance_id = s.id
            JOIN type_seance ts ON s.type_seance_id = ts.id
            WHERE dg.joueur_id = %s
              AND s.date >= %s::date - INTERVAL '{jours_ref} days'
              AND s.date <= %s::date
              AND dg.distance_totale_m > 0
        """, (ref, str(joueur_id), ref, ref))
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
    s2_sc_m, s2_ra_m, s2_det_m = _signal2_detail(joueur_id, TYPES_MATCH,    "de match",   cfg, conn, ref)
    s2_sc_i, s2_ra_i, s2_det_i = _signal2_detail(joueur_id, TYPES_INTENSIF, "intensives", cfg, conn, ref)

    if s2_sc_m >= s2_sc_i:
        s2_score, s2_raison, s2_details = s2_sc_m, s2_ra_m, s2_det_m
    else:
        s2_score, s2_raison, s2_details = s2_sc_i, s2_ra_i, s2_det_i

    # ── Signal 3 ──
    s3_score, s3_raison = _calcul_signal3(joueur_id, cfg, conn, ref)

    # ── Signal 4 ──
    s4_score, s4_raison = _calcul_signal4(joueur_id, cfg, conn, ref)

    # ── Signal wellness (ressenti subjectif) ──
    w_score, w_raison = _signal_wellness(joueur_id, cfg, conn, ref)

    # ── Signal sRPE (charge ressentie) ──
    sr_score, sr_raison = _signal_srpe(joueur_id, cfg, conn, ref)

    # ── Bonus blessure ──
    b_score, b_raison = _bonus_blessure(joueur_id, cfg, conn, ref)

    # ── Bonus congestion ──
    c_score, c_raison = _bonus_congestion(joueur_id, cfg, conn, ref)

    # ── Garde-fou anti double comptage des deux surcharges ──
    s1_score, sr_score, absorbe = _plafonner_charges(s1_score, sr_score)
    if absorbe == "srpe":
        sr_raison = _marquer_absorbe(sr_raison)
    elif absorbe == "gps":
        s1_raison = _marquer_absorbe(s1_raison)

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

def _niveau_fatigue(score: float) -> str:
    if score < 30:
        return "NOMINAL"
    elif score < 60:
        return "VIGILANCE"
    return "ALERTE"
