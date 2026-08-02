"""
Charge cible, objectif hebdomadaire et simulation « et si… ».

Répond à « combien ce joueur peut-il encaisser cette semaine ? » en partant de sa charge
chronique et des bornes d'ACWR, puis projette une séance HYPOTHÉTIQUE : la simulation
n'écrit jamais rien en base, elle ajoute une distance attendue à la charge aiguë pour
voir qui basculerait au-dessus du plafond.

La distance attendue vient de `_baseline_ratio` (m/min moyen des 10 dernières séances du
même type) — le même socle que le rapport de séance, pour que les deux écrans racontent
la même chose.
"""
from uuid import UUID
from datetime import date as _date, timedelta as _timedelta

from app.core.config import _poids_seance, _normaliser_poste, _objectif_poste
from app.domain.temps import _lundi
from app.domain.effectif import _joueurs_resume
from app.domain.charge import (_charge_gps, _charge_rpe, _charge_acwr_unifiee,
                               _moyenne_hebdo_gps, _moyenne_hebdo_rpe, _baseline_ratio)


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

def _simuler_acwr(joueur_id: UUID, cfg: dict, conn, delta_m: float = 0.0, date_ref=None) -> dict:
    """
    Recalcule l'ACWR du joueur EN AJOUTANT `delta_m` mètres à sa charge AIGUË (une séance à venir
    tombe dans la fenêtre aiguë ; la fenêtre chronique, elle, ne bouge pas — d'où un simple delta
    sur l'aiguë).

    N'ALTÈRE AUCUNE fonction existante : réutilise `_charge_gps` / `_charge_rpe` en lecture et
    refait la combinaison pondérée à l'identique de `_charge_acwr_unifiee`, sur les valeurs BRUTES
    (pas les valeurs arrondies renvoyées par celle-ci). Le score de risque officiel reste inchangé.

    Renvoie {source, acwr_avant, acwr_apres, aigue_avant_km, aigue_apres_km, chronique_km}.
    """
    gps = _charge_gps(joueur_id, cfg, conn, date_ref)
    rpe = _charge_rpe(joueur_id, conn, date_ref, cfg)
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

def _simulation_seance_data(conn, cfg, scope, type_seance_id, duree_minutes: int,
                            date_ref=None) -> dict:
    """
    Cœur du scénario « une séance » de la simulation. Pour chaque joueur de l'effectif :
    distance attendue (baseline m/min du même type × durée), ACWR avant/après ajout de cette
    distance, zone avant/après, et bascule éventuelle vers la surcharge.

    Purement en LECTURE : aucune séance n'est créée, aucune donnée n'est écrite.
    """
    recence_j = int(cfg.get("baseline_recence_jours", 90))
    duree = max(1, int(duree_minutes or 0))

    libelle_type = None
    profil_type = "TERRAIN"
    if type_seance_id:
        with conn.cursor() as cur:
            cur.execute("SELECT libelle, profil FROM type_seance WHERE id = %s", (str(type_seance_id),))
            row = cur.fetchone()
            if row:
                libelle_type = row[0]
                profil_type = row[1] or "TERRAIN"

    # Un type qui ne produit PAS de déplacement mesuré (musculation, piscine, vidéo) n'a pas de
    # distance attendue — et surtout pas celle des séances de terrain. Sans cette garde, le repli
    # « baseline globale » ci-dessous annonçait tranquillement « 6,2 km attendus » pour une séance
    # de squats, puis recalculait un ACWR sur cette distance imaginaire.
    if profil_type != "TERRAIN":
        return {
            "seance": {
                "type_seance_id": str(type_seance_id) if type_seance_id else None,
                "type_libelle":   libelle_type,
                "profil":         profil_type,
                "duree_minutes":  duree,
            },
            "synthese": {
                "nb_evalues": 0, "nb_sans_baseline": 0,
                "nb_surcharge_avant": 0, "nb_surcharge_apres": 0,
                "nb_bascule": 0, "km_attendu_moyen": None,
                "nb_peu_fiable": 0,
            },
            "joueurs": [],
            "non_applicable": (
                f"La simulation de distance ne s'applique pas à une séance « {libelle_type or profil_type} » : "
                "ce type ne produit pas de déplacement mesuré au GPS. Sa charge est comptée via le sRPE "
                "une fois la séance notée par les joueurs."
            ),
        }

    joueurs = []
    for (jid, nom, prenom, poste) in _joueurs_resume(conn, scope):
        jid = str(jid)
        ratio, n = _baseline_ratio(jid, type_seance_id, conn, recence_j, date_ref)
        origine = "TYPE"
        if (ratio is None or n < 3) and type_seance_id:
            # Type trop mince → repli explicite sur la baseline toutes séances confondues.
            ratio_g, n_g = _baseline_ratio(jid, None, conn, recence_j, date_ref)
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
        sim = _simuler_acwr(jid, cfg, conn, delta_m=attendu_m, date_ref=date_ref)
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
            "profil":         profil_type,
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

def _objectif_hebdo_data(conn, cfg, scope, date_ref=None) -> dict:
    """
    Cœur du panneau « Objectif de la semaine » (extrait pour être réutilisé par la carte briefing
    sans repasser par la couche HTTP). Par joueur : cumul de la semaine en cours, cible A.5,
    objectif retenu (manuel d'équipe si défini et scope = 1 équipe, sinon cible A.5) et atteinte.

    « La semaine en cours » suit `date_ref` : en voyage dans la saison, le panneau doit montrer
    la semaine où l'on se place, pas celle du calendrier réel.
    """
    ref = date_ref or _date.today()
    objectif_m = None
    if scope and len(scope) == 1:
        with conn.cursor() as cur:
            cur.execute("SELECT objectif_distance_m FROM objectif_hebdo WHERE equipe_id = %s",
                        (scope[0],))
            row = cur.fetchone()
            objectif_m = int(row[0]) if row and row[0] is not None else None

    # Cumul de distance de la semaine en cours (lundi → aujourd'hui), par joueur.
    cwhere = ["s.date >= date_trunc('week', %s::date)::date", "s.date <= %s::date"]
    cparams: list = [ref, ref]
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
        cible = _charge_cible(jid, cfg, conn, date_ref)
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
