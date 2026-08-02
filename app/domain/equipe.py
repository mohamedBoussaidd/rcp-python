"""
Vues collectives : charge d'équipe, résumé de l'effectif, briefing du préparateur.

`resume_equipe` est le point d'entrée le plus lourd de tout le service : il recalcule
risque, fatigue, fraîcheur, monotonie et marqueur neuromusculaire pour CHAQUE joueur de
la portée. C'est aussi, de fait, le meilleur test de non-régression du moteur.

`briefing` n'est jamais envoyé tel quel au front : le back Java le consomme pour le mettre
en mots (LLM) ou remplir un gabarit. Il ne contient que des chiffres agrégés, jamais de
donnée brute.
"""
from fastapi import HTTPException
from uuid import UUID
from datetime import date as _date, timedelta as _timedelta

from app.core.database import get_connection
from app.core.config import _load_config, _normaliser_poste, _objectif_poste
from app.core.scope import _equipes_scope
from app.domain.temps import _parse_date_simulee
from app.domain.contexte import _contexte_joueur
from app.domain.charge import _charge_gps, _charge_rpe, _charge_acwr_unifiee, _baseline_ratio
from app.domain.risque import _risque_probabiliste, _niveau_risque, _calcul_score_risque
from app.domain.fatigue import _calcul_fatigue, _niveau_fatigue
from app.domain.athletique import _readiness_joueur, _monotonie_joueur, _sprint_neuromusculaire
from app.domain.objectif import _objectif_hebdo_data
from app.domain.effectif import _joueurs_resume
from app.schemas.schemas import ResumeJoueur


def charge_equipe(debut: str | None = None, fin: str | None = None, types: str | None = None,
                  x_contexte_equipes: str | None = None,
                  x_contexte_club: str | None = None,
                  x_date_simulee: str | None = None):
    """
    Charge externe agrégée de l'équipe sur une période.
    Renvoie deux vues :
      - seances : une ligne par séance de la période (totaux d'équipe + distance attendue) ;
      - joueurs : totaux par joueur + classement (tri par distance décroissante).
    La distance attendue réutilise la baseline du rapport par séance (ratio moyen des
    10 dernières séances de même type du joueur).

    La période affichée reste pilotée par `debut`/`fin` (explicites) ; `date_ref` ne borne que
    la construction de la BASELINE, sinon la norme d'une période passée serait bâtie sur des
    séances postérieures à celle-ci.
    """
    ref = _parse_date_simulee(x_date_simulee) or _date.today()
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
                      AND s.date >= %s::date - INTERVAL '{recence_j} days'
                      AND s.date <= %s::date
                    ORDER BY dg.joueur_id, s.type_seance_id, s.date DESC
                """, (ref, ref))
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

def briefing(x_contexte_equipes: str | None = None,
             x_contexte_club: str | None = None,
             x_date_simulee: str | None = None):
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
            oh    = _objectif_hebdo_data(conn, cfg, scope, _parse_date_simulee(x_date_simulee))

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

def resume_equipe(x_date_simulee: str | None = None,
                  x_contexte_equipes: str | None = None,
                  x_contexte_club: str | None = None):
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
                readiness, readiness_date = _readiness_joueur(joueur_id, conn, date_ref)

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
                fatigue = _calcul_fatigue(joueur_id, cfg, conn, date_ref)
                sprint  = _sprint_neuromusculaire(joueur_id, cfg, conn, date_ref)
                resultats.append(ResumeJoueur(
                    **commun,
                    score_risque=risque["score"],
                    score_fatigue=fatigue["score"],
                    niveau_risque=_niveau_risque(risque["score"]),
                    niveau_fatigue=fatigue["niveau"],
                    acwr=risque["acwr"],
                    charge_aigue_km=risque["charge_aigue_km"],
                    charge_chronique_km=risque["charge_chronique_km"],
                    monotonie=_monotonie_joueur(joueur_id, cfg, conn, date_ref),
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
