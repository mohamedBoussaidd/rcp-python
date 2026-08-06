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
from app.domain.referentiel import (
    METRIQUES_CUMUL, METRIQUE_EXPO, SQL_METRIQUE,
    _poste_reference, _referentiel_equipe, _attendus_par_poste,
    _objectif_periode_courant, _objectif_periode_postes, _club_equipe_du_scope,
    _cout_match, _matchs_semaine, _arbitrage_semaine, _deltas_semaine, _origines_report,
)
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

def _trajectoire_rattrapage(chronique_m: float, attendu_m: int, acwr_max: float,
                            max_semaines: int = 8) -> list:
    """
    Chemin pour rejoindre l'« Attendu » SANS jamais dépasser le plafond d'ACWR.

    Le normatif dit où le joueur devrait être, l'ACWR dit ce qu'il peut encaisser maintenant :
    quand les deux se contredisent, prescrire l'écart d'un coup serait prescrire une blessure, et
    l'ignorer serait laisser le joueur sous le niveau de sa division indéfiniment. On rend donc le
    chemin, semaine par semaine.

    La charge chronique étant une moyenne sur 4 semaines, elle ne monte que d'un quart de l'écart
    à chaque semaine — c'est ce qui donne au rattrapage sa durée réelle plutôt qu'optimiste.
    Renvoie [] si l'attendu est déjà atteignable dès cette semaine.
    """
    if chronique_m <= 0 or attendu_m <= 0:
        return []
    chro = float(chronique_m)
    if attendu_m <= chro * acwr_max:
        return []                                  # atteignable tout de suite : pas de trajectoire
    etapes = []
    for _ in range(max_semaines):
        plafond = chro * acwr_max
        cible = min(float(attendu_m), plafond)
        etapes.append(round(cible))
        if cible >= attendu_m * 0.98:
            break
        chro = chro + (cible - chro) / 4.0         # la chronique est une moyenne 4 semaines
    return etapes


def _cumuls_semaine(conn, scope, ref) -> tuple:
    """
    Cumul de la semaine en cours (lundi → `ref`) par joueur, sur les 6 métriques cumulatives,
    plus le pic de vitesse de la semaine et le record de la saison (pour l'exposition).

    Renvoie ({joueur: {metrique: valeur}}, {joueur: (vmax_semaine, vmax_record)}).
    """
    where = ["s.date >= date_trunc('week', %s::date)::date", "s.date <= %s::date"]
    params: list = [ref, ref]
    if scope:
        where.append("s.equipe_id = ANY(%s)"); params.append(scope)
    colonnes = ", ".join(f"SUM({SQL_METRIQUE[m]}) AS {m}" for m in METRIQUES_CUMUL)

    cumul: dict = {}
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT dg.joueur_id, {colonnes}, MAX(COALESCE(dg.vitesse_max_kmh, 0))
            FROM donnee_gps dg JOIN seance s ON s.id = dg.seance_id
            WHERE {' AND '.join(where)}
            GROUP BY dg.joueur_id
        """, params)
        for row in cur.fetchall():
            jid = str(row[0])
            cumul[jid] = {m: float(row[i + 1] or 0.0) for i, m in enumerate(METRIQUES_CUMUL)}
            cumul[jid]["_vmax_semaine"] = float(row[len(METRIQUES_CUMUL) + 1] or 0.0)

    # Record personnel : la référence de l'exposition. Un pourcentage n'a de sens que rapporté au
    # meilleur du joueur lui-même — « 32 km/h » ne veut rien dire pour qui plafonne à 30.
    records: dict = {}
    rwhere = ["s.date <= %s::date"]
    rparams: list = [ref]
    if scope:
        rwhere.append("s.equipe_id = ANY(%s)"); rparams.append(scope)
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT dg.joueur_id, MAX(COALESCE(dg.vitesse_max_kmh, 0))
            FROM donnee_gps dg JOIN seance s ON s.id = dg.seance_id
            WHERE {' AND '.join(rwhere)}
            GROUP BY dg.joueur_id
        """, rparams)
        for jid, vmax in cur.fetchall():
            records[str(jid)] = float(vmax or 0.0)
    return cumul, records


def _avec_delta(valeur, delta: int):
    """Prescrit + report d'arbitrage, planché à 0. `None` reste `None` : pas de prescrit, pas de
    delta à appliquer — un report ne CRÉE jamais un objectif là où il n'y en avait pas."""
    if valeur is None:
        return None
    return max(0, int(valeur) + int(delta or 0))


def _objectif_hebdo_data(conn, cfg, scope, date_ref=None, objectifs_actifs: bool = False) -> dict:
    """
    Cœur du panneau « Objectif de la semaine » (extrait pour être réutilisé par la carte briefing
    sans repasser par la couche HTTP). Par joueur : cumul de la semaine en cours, cible A.5,
    objectif retenu (manuel d'équipe si défini et scope = 1 équipe, sinon cible A.5) et atteinte.

    « La semaine en cours » suit `date_ref` : en voyage dans la saison, le panneau doit montrer
    la semaine où l'on se place, pas celle du calendrier réel.

    `objectifs_actifs` = add-on « Objectifs de performance » actif pour le club, tranché par Java
    (en-tête X-Module-Objectifs) : Python ne connaît ni l'utilisateur ni l'abonnement. À False, le
    référentiel adopté — qui reste en base quand le club perd le module —, les objectifs prescrits
    de la période ET le plafonnement ACWR sortent du calcul : on retombe exactement sur le
    comportement d'avant l'add-on. Défaut False : un appelant qui oublie le drapeau ne rallume
    jamais un module par accident.
    """
    ref = date_ref or _date.today()
    objectif_m = None
    if scope and len(scope) == 1:
        with conn.cursor() as cur:
            cur.execute("SELECT objectif_distance_m FROM objectif_hebdo WHERE equipe_id = %s",
                        (scope[0],))
            row = cur.fetchone()
            objectif_m = int(row[0]) if row and row[0] is not None else None

    cumul, records = _cumuls_semaine(conn, scope, ref)

    # ── « Attendu » : la seule référence extérieure au joueur ────────────────
    # Sans elle, un latéral à 24 km/semaine depuis un mois est affiché VERT parce qu'il fait sa
    # moyenne — alors qu'un latéral de National en fait 31 à 36. L'application savait dire
    # « il s'entraîne comme d'habitude », jamais « il s'entraîne comme il faudrait ».
    # Module coupé → aucune de ces trois lectures : pas de norme de poste, pas de prescrit, et
    # donc pas de rattrapage ni d'écart au niveau attendu plus bas dans la boucle.
    referentiel_id, attendus = None, {}
    prescrit_semaine, prescrit_postes = {}, {}
    cout_match, deltas, arbitrage, origines = {}, {}, {}, []
    matchs = []
    lundi = _lundi(ref)
    if objectifs_actifs:
        club_id, equipe_id = _club_equipe_du_scope(conn, scope)
        referentiel_id = _referentiel_equipe(conn, club_id, equipe_id)
        attendus = _attendus_par_poste(conn, referentiel_id, "SEMAINE")
        prescrit_semaine = _objectif_periode_courant(conn, club_id, equipe_id, ref)
        prescrit_postes  = _objectif_periode_postes(conn, club_id, equipe_id, ref)
        # ── Semaine à deux matchs ────────────────────────────────────────────
        # La cible hebdo INCLUT le match : deux rencontres ne relèvent pas la semaine, elles
        # mangent la part d'entraînement. On la dérive ici pour que l'écran cesse de laisser
        # croire que 34 km de cible = 34 km d'entraînement.
        cout_match = _cout_match(conn, referentiel_id)
        matchs     = _matchs_semaine(conn, equipe_id, lundi)
        arbitrage  = _arbitrage_semaine(conn, equipe_id, lundi)
        deltas     = _deltas_semaine(conn, equipe_id, lundi)
        origines   = _origines_report(conn, equipe_id, lundi)

    acwr_max = float(cfg.get("acwr_cible_max", 1.3))
    seuil_expo = float(cfg.get("expo_vmax_pct", 90))

    joueurs = []
    somme_ideal = 0.0
    nb_ideal = 0
    sem_dispo: list = []   # semaines de données réellement disponibles (fiabilité de la suggestion)
    for (jid, nom, prenom, poste) in _joueurs_resume(conn, scope):
        jid = str(jid)
        cible = _charge_cible(jid, cfg, conn, date_ref)
        cible_ideal_m = None
        chronique_m = None
        if cible.get("disponible") and cible.get("unite") == "km" and cible.get("cible_ideal") is not None:
            cible_ideal_m = round(cible["cible_ideal"] * 1000)
            chronique_m = round(cible["chronique"] * 1000) if cible.get("chronique") is not None else None
            somme_ideal += cible_ideal_m
            nb_ideal += 1
            if cible.get("semaines") is not None:
                sem_dispo.append(cible["semaines"])

        mesures = cumul.get(jid, {})
        cum = round(mesures.get("distance_totale", 0.0))
        plafond_m = round(cible["plafond"] * 1000) if cible_ideal_m is not None and cible.get("plafond") is not None else None

        # Attendu du poste (norme) — None si le poste est absent du référentiel (gardien) ou
        # inconnu : mieux vaut ne rien afficher qu'une cible fausse.
        pref = _poste_reference(poste)
        attendu_poste = attendus.get(pref, {}) if pref else {}
        att = attendu_poste.get("distance_totale")
        attendu_m = None
        if att:
            attendu_m = att[0] if att[1] is None else (att[0] + att[1]) // 2 if att[0] is not None else att[1]

        # ── Cascade du RETENU : prescrit → manuel → suggestion intelligente ──
        # Un seul point de décision, et c'est ce qui rend le module débranchable : sans objectif
        # prescrit ni objectif manuel, on retombe exactement sur le comportement d'avant.
        prescrit = None
        source = None
        pres = prescrit_semaine.get("distance_totale") or \
               (prescrit_postes.get(pref, {}).get("distance_totale") if pref else None)
        if pres and pres.get("min") is not None:
            # Le delta d'arbitrage s'ajoute au prescrit sans jamais le réécrire : la trajectoire
            # d'origine reste lisible, et retirer l'arbitrage rétablit tout.
            prescrit = max(0, pres["min"] + deltas.get("distance_totale", 0))
            source = "PRESCRIT"
        if prescrit is not None:
            obj = prescrit
        elif objectif_m is not None:
            obj = objectif_m; source = "MANUEL"
        else:
            obj = cible_ideal_m
            source = "INTELLIGENT" if cible_ideal_m is not None else None

        # Le prescrit ne dispense JAMAIS du plafond de sécurité : on affiche l'écart et on propose
        # un chemin plutôt que de prescrire une surcharge. Le plafonnement est né avec l'add-on :
        # sans lui, l'objectif d'équipe manuel repart brut, comme avant.
        retenu = obj
        bride = False
        if objectifs_actifs and obj is not None and plafond_m is not None and obj > plafond_m:
            retenu = plafond_m
            bride = True

        atteint = (cum >= retenu) if retenu else None
        reste   = max(0, retenu - cum) if retenu else None

        # Part d'entraînement = ce qui reste une fois les matchs de la semaine retirés de la
        # cible. N'a de sens que si la semaine porte au moins un match ET qu'on sait ce qu'un
        # match coûte (donc qu'un référentiel est adopté).
        entrainement_m = None
        cout_dt = cout_match.get("distance_totale")
        if retenu is not None and matchs and cout_dt:
            entrainement_m = max(0, retenu - len(matchs) * cout_dt)

        ecart_pct = None
        if attendu_m and chronique_m:
            ecart_pct = round((chronique_m - attendu_m) / attendu_m * 100, 1)
        trajectoire = _trajectoire_rattrapage(chronique_m or 0, attendu_m or 0, acwr_max) \
            if (attendu_m and chronique_m) else []

        # Exposition à la vitesse max : un PIC rapporté au record du joueur, pas un cumul.
        record = records.get(jid, 0.0)
        vmax_sem = mesures.get("_vmax_semaine", 0.0)
        expo_pct = round(vmax_sem / record * 100) if record > 0 and vmax_sem > 0 else None

        joueurs.append({
            "joueur_id":     jid,
            "nom":           nom,
            "prenom":        prenom,
            "poste":         poste or "",
            "poste_reference": pref,
            "cumul_m":       cum,
            "cible_ideal_m": cible_ideal_m,
            "cible_min_m":   round(cible["cible_min"]   * 1000) if cible_ideal_m is not None and cible.get("cible_min")   is not None else None,
            "cible_haute_m": round(cible["cible_haute"] * 1000) if cible_ideal_m is not None and cible.get("cible_haute") is not None else None,
            "plafond_m":     plafond_m,
            "objectif_m":    retenu,
            "source":        source,
            "atteint":       atteint,
            "reste_m":       reste,
            # ── Habituel / Attendu / Retenu ──
            "habituel_m":    chronique_m,
            "attendu_m":     attendu_m,
            "attendu_min_m": att[0] if att else None,
            "attendu_max_m": att[1] if att else None,
            "retenu_m":      retenu,
            "entrainement_m": entrainement_m,
            "bride_acwr":    bride,
            "ecart_attendu_pct": ecart_pct,
            "rattrapage_semaines": len(trajectoire),
            "rattrapage": trajectoire,
            "phase":         (pres or {}).get("phase") if pres else None,
            # ── Les 6 autres métriques : cumul, attendu et priorité ──
            "metriques": {
                m: {
                    "cumul": round(mesures.get(m, 0.0)),
                    "attendu_min": (attendu_poste.get(m) or (None, None))[0],
                    "attendu_max": (attendu_poste.get(m) or (None, None))[1],
                    # Même règle que pour le volume : prescrit + delta d'arbitrage, jamais réécrit.
                    "retenu": _avec_delta((prescrit_semaine.get(m) or
                                           (prescrit_postes.get(pref, {}).get(m) if pref else None)
                                           or {}).get("min"), deltas.get(m, 0)),
                    "priorite": (prescrit_semaine.get(m) or
                                 (prescrit_postes.get(pref, {}).get(m) if pref else None) or {}).get("priorite"),
                } for m in METRIQUES_CUMUL
            },
            "expo_vmax_pct":     expo_pct,
            "expo_vmax_cible":   (prescrit_semaine.get(METRIQUE_EXPO) or {}).get("min")
                                 or (attendu_poste.get(METRIQUE_EXPO) or (None, None))[0]
                                 or round(seuil_expo),
            "vitesse_max_semaine": round(vmax_sem, 1) if vmax_sem else None,
            "vitesse_max_record":  round(record, 1) if record else None,
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

    phase = next((v.get("phase") for v in prescrit_semaine.values() if v.get("phase")), None)
    return {
        "objectif_distance_m":  objectif_m,
        "suggestion_moyenne_m": round(somme_ideal / nb_ideal) if nb_ideal else None,
        "suggestion_semaines":  min(sem_dispo) if sem_dispo else None,
        "suggestion_provisoire": bool(sem_dispo) and min(sem_dispo) < 4,
        "multi_equipes":        bool(scope) and len(scope) > 1,
        "nb_atteint":           nb_atteint,
        "nb_concernes":         nb_concernes,
        "meilleur":             meilleur,
        # Contexte du prescrit : ce qui permet à l'écran de dire « Semaine 3 — Accumulation »
        # plutôt qu'un chiffre sans origine.
        "referentiel_actif":    referentiel_id is not None,
        "prescrit_actif":       bool(prescrit_semaine) or bool(prescrit_postes),
        "phase":                phase,
        "nb_sous_attendu":      sum(1 for j in joueurs
                                    if j["ecart_attendu_pct"] is not None and j["ecart_attendu_pct"] < -5),
        "nb_rattrapage":        sum(1 for j in joueurs if j["rattrapage_semaines"] > 0),
        # ── Semaine à deux matchs ────────────────────────────────────────────
        # `arbitre` distingue « on n'a pas encore décidé » de « on a décidé d'alléger », deux
        # situations que l'écran ne doit surtout pas confondre : la première appelle une action.
        "semaine": {
            "date_lundi":   lundi.isoformat(),
            "nb_matchs":    len(matchs),
            "dates_matchs": [d.isoformat() for d in matchs],
            "cout_match_m": cout_match.get("distance_totale"),
            "arbitre":      bool(arbitrage),
            "choix":        arbitrage.get("choix"),
            "note":         arbitrage.get("note"),
            # Le calendrier a-t-il bougé depuis la décision ? Un report devenu sans objet doit se
            # voir, pas s'appliquer en silence.
            "calendrier_change": bool(arbitrage) and arbitrage.get("nb_matchs") != len(matchs),
            "deltas":       deltas,
            "origines":     [{"semaine_source": o["semaine_source"].isoformat(),
                              "choix": o["choix"], "delta": o["delta"]} for o in origines],
        },
        "joueurs":              joueurs,
    }
