"""
Suivi des objectifs dans le TEMPS : la trajectoire d'un joueur sur une période, et le bilan
d'une période une fois qu'elle est passée.

Le panneau hebdomadaire (`objectif.py`) répond à « où en est-on cette semaine ». Ces deux
lectures-ci répondent à « où en est-on dans la période » : la première pour un joueur, la
seconde pour l'équipe. Elles partagent la même ossature — les semaines d'une période, le
prescrit de chaque semaine, le réalisé en face — d'où ce module commun.

Rien n'est stocké : un bilan figé en base deviendrait faux au premier import GPS arrivé en
retard, et il faudrait un job de clôture pour le rattraper. Tout se recalcule à l'appel.
"""
from datetime import date as _date, timedelta as _timedelta

from app.domain.temps import _lundi
from app.domain.referentiel import (
    METRIQUES_CUMUL, SQL_METRIQUE,
    _poste_reference, _referentiel_equipe, _attendus_par_poste,
    _periode_de, _periode_par_id, _trajectoire_periode, _objectif_periode_postes,
    _deltas_semaine, _matchs_semaine,
)

# La chronique de l'application est une moyenne 4 semaines : « Habituel » doit être la même
# grandeur que celle du panneau hebdo, sinon les deux écrans se contredisent sur le même joueur.
SEMAINES_CHRONIQUE = 4


def _semaines_de(debut, fin) -> list:
    """Lundis couvrant une période, du premier au dernier inclus."""
    if not debut or not fin:
        return []
    cur, res = _lundi(debut), []
    dernier = _lundi(fin)
    while cur <= dernier:
        res.append(cur)
        cur = cur + _timedelta(days=7)
    return res


def _realise_par_semaine(conn, lundis, joueur_id=None, scope=None) -> dict:
    """
    Cumul GPS par semaine : {lundi: {metrique: valeur}} pour un joueur, ou
    {lundi: {joueur_id: {metrique: valeur}}} quand on interroge une équipe.

    Une seule requête pour toute la période — les semaines sont bucketées en SQL, comme la
    charge collective, plutôt qu'une requête par semaine.
    """
    if not lundis:
        return {}
    debut, fin = lundis[0], lundis[-1] + _timedelta(days=6)
    colonnes = ", ".join(f"SUM({SQL_METRIQUE[m]}) AS {m}" for m in METRIQUES_CUMUL)
    where = ["s.date BETWEEN %s::date AND %s::date"]
    params: list = [debut, fin]
    if joueur_id:
        where.append("dg.joueur_id = %s"); params.append(str(joueur_id))
    if scope:
        where.append("s.equipe_id = ANY(%s)"); params.append(scope)

    res: dict = {}
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT date_trunc('week', s.date)::date AS lundi, dg.joueur_id, {colonnes}
            FROM donnee_gps dg JOIN seance s ON s.id = dg.seance_id
            WHERE {' AND '.join(where)}
            GROUP BY 1, 2
        """, params)
        for row in cur.fetchall():
            lundi, jid = row[0], str(row[1])
            valeurs = {m: float(row[i + 2] or 0.0) for i, m in enumerate(METRIQUES_CUMUL)}
            if joueur_id:
                res[lundi] = valeurs
            else:
                res.setdefault(lundi, {})[jid] = valeurs
    return res


def _milieu(bornes) -> int | None:
    """Valeur centrale d'une fourchette (min, max) du référentiel."""
    if not bornes:
        return None
    vmin, vmax = bornes
    if vmin is None and vmax is None:
        return None
    if vmin is None:
        return vmax
    if vmax is None:
        return vmin
    return (vmin + vmax) // 2


# ══════════════════════════════════════════════════════════════════════════════
# Trajectoire d'un joueur
# ══════════════════════════════════════════════════════════════════════════════

def trajectoire_joueur(conn, joueur_id, periode_id=None, scope=None,
                       date_ref=None, objectifs_actifs: bool = False) -> dict:
    """
    Les trois courbes du joueur sur une période : Habituel, Attendu, Retenu — plus le réalisé.

    Habituel n'est PAS le réalisé de la semaine : c'est la moyenne des quatre semaines qui la
    précèdent, la même grandeur que la charge chronique du reste de l'application. C'est ce qui
    permet de lire « il fait comme d'habitude, et d'habitude c'est 25 % sous son poste ».

    Sans le module, la fonction répond une enveloppe vide plutôt qu'un objet à moitié rempli :
    l'onglet n'existe pas dans ce cas, et un demi-résultat inviterait à l'afficher quand même.
    """
    ref = date_ref or _date.today()
    if not objectifs_actifs:
        return {"disponible": False, "semaines": []}

    with conn.cursor() as cur:
        # Le club est porté par la fiche (`j.club_id`) ; l'équipe NE L'EST PLUS depuis la Phase 4
        # (plus de cache `j.equipe_id`) — elle se dérive de l'effectif de la saison EN_COURS,
        # exactement comme `contexte.py`. Toute autre lecture renverrait une colonne inexistante.
        cur.execute("""
            SELECT j.nom, j.prenom, j.poste_principal, j.club_id,
                   (SELECT es.equipe_id FROM effectif_saison es
                      JOIN saison s ON s.id = es.saison_id AND s.statut = 'EN_COURS'
                     WHERE es.joueur_id = j.id
                     ORDER BY es.date_entree DESC NULLS LAST LIMIT 1) AS equipe_id
            FROM joueur j WHERE j.id = %s
        """, (str(joueur_id),))
        row = cur.fetchone()
    if not row:
        return {"disponible": False, "semaines": [], "erreur": "Joueur introuvable"}
    nom, prenom, poste, club_id, equipe_id = row
    equipe_id = str(equipe_id) if equipe_id else None
    club_id = str(club_id) if club_id else None

    # Anti-IDOR : la portée résolue par Java fait foi. Un joueur hors périmètre ressort vide,
    # jamais avec ses données.
    if scope and (equipe_id is None or equipe_id not in [str(e) for e in scope]):
        return {"disponible": False, "semaines": [], "erreur": "Hors périmètre"}

    periode = _periode_par_id(conn, periode_id) if periode_id else _periode_de(conn, equipe_id, ref)
    if not periode:
        return {"disponible": False, "semaines": [],
                "erreur": "Aucune période de saison ne couvre cette date."}

    # Une période appartient à UNE équipe : quand elle est ciblée nommément (saison passée,
    # joueur qui a changé d'équipe), c'est la sienne qui commande les deltas et les matchs —
    # sinon on lirait ceux de l'équipe courante du joueur sur une période qui ne la concerne pas.
    if periode.get("equipe_id"):
        equipe_periode = str(periode["equipe_id"])
        if scope and equipe_periode not in [str(e) for e in scope]:
            return {"disponible": False, "semaines": [], "erreur": "Hors périmètre"}
        equipe_id = equipe_periode

    lundis = _semaines_de(periode["date_debut"], periode["date_fin"])
    # On remonte 4 semaines avant le début : sans elles, l'Habituel de la première semaine de
    # période serait nul et la courbe démarrerait par une fausse chute.
    lundis_etendus = [lundis[0] - _timedelta(days=7 * i) for i in range(SEMAINES_CHRONIQUE, 0, -1)] + lundis
    realise = _realise_par_semaine(conn, lundis_etendus, joueur_id=joueur_id)

    referentiel_id = _referentiel_equipe(conn, club_id, equipe_id)
    attendus = _attendus_par_poste(conn, referentiel_id, "SEMAINE")
    pref = _poste_reference(poste)
    attendu_poste = attendus.get(pref, {}) if pref else {}

    trajectoire = _trajectoire_periode(conn, periode.get("objectif_periode_id"))
    postes_cibles = _objectif_periode_postes(conn, club_id, equipe_id, ref) if not trajectoire else {}
    cibles_poste = postes_cibles.get(pref, {}) if pref else {}

    semaines = []
    for i, lundi in enumerate(lundis):
        idx = lundis_etendus.index(lundi)
        # Habituel : moyenne des 4 semaines précédentes, telles qu'elles ont été réellement
        # réalisées. Les semaines sans donnée comptent pour 0 — un joueur absent a bien une
        # charge chronique qui baisse.
        precedentes = lundis_etendus[max(0, idx - SEMAINES_CHRONIQUE):idx]
        habituel = None
        if precedentes:
            habituel = round(sum(realise.get(l, {}).get("distance_totale", 0.0)
                                 for l in precedentes) / len(precedentes))

        prescrit = trajectoire.get(lundi, {}).get("distance_totale") or cibles_poste.get("distance_totale")
        deltas = _deltas_semaine(conn, equipe_id, lundi)
        retenu = None
        if prescrit and prescrit.get("min") is not None:
            retenu = max(0, prescrit["min"] + deltas.get("distance_totale", 0))

        cum = round(realise.get(lundi, {}).get("distance_totale", 0.0))
        semaines.append({
            "date_lundi":  lundi.isoformat(),
            "no_semaine":  i + 1,
            "phase":       (trajectoire.get(lundi, {}).get("distance_totale") or {}).get("phase"),
            "habituel_m":  habituel,
            "attendu_m":   _milieu(attendu_poste.get("distance_totale")),
            "attendu_min_m": (attendu_poste.get("distance_totale") or (None, None))[0],
            "attendu_max_m": (attendu_poste.get("distance_totale") or (None, None))[1],
            "retenu_m":    retenu,
            "realise_m":   cum,
            "nb_matchs":   len(_matchs_semaine(conn, equipe_id, lundi)),
            # Une semaine future n'a pas de réalisé : l'écran doit arrêter la courbe du réalisé
            # là plutôt que de la faire plonger à zéro sur les semaines à venir.
            "passee":      lundi + _timedelta(days=6) <= ref,
            "metriques": {
                m: {
                    "realise": round(realise.get(lundi, {}).get(m, 0.0)),
                    "attendu": _milieu(attendu_poste.get(m)),
                    "retenu":  (lambda p: max(0, p["min"] + deltas.get(m, 0))
                                if p and p.get("min") is not None else None)(
                        trajectoire.get(lundi, {}).get(m) or cibles_poste.get(m)),
                } for m in METRIQUES_CUMUL
            },
        })

    passees = [s for s in semaines if s["passee"]]
    avec_cible = [s for s in passees if s["retenu_m"]]
    return {
        "disponible": True,
        "joueur": {"id": str(joueur_id), "nom": nom, "prenom": prenom,
                   "poste": poste or "", "poste_reference": pref},
        "periode": {"id": periode["id"], "libelle": periode["libelle"], "type": periode["type"],
                    "date_debut": periode["date_debut"].isoformat(),
                    "date_fin": periode["date_fin"].isoformat()},
        "referentiel_actif": referentiel_id is not None,
        "nb_semaines": len(semaines),
        "nb_semaines_tenues": sum(1 for s in avec_cible if s["realise_m"] >= s["retenu_m"]),
        "nb_semaines_evaluees": len(avec_cible),
        "semaines": semaines,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Bilan d'une période
# ══════════════════════════════════════════════════════════════════════════════

def bilan_periode(conn, periode_id, scope=None, date_ref=None,
                  objectifs_actifs: bool = False) -> dict:
    """
    Ce que la période a produit : prescrit contre réalisé, par métrique et par semaine.

    Le taux d'atteinte est calculé sur les joueurs QUI ONT DES DONNÉES cette semaine-là, jamais
    sur l'effectif entier : compter un joueur blessé comme « objectif non atteint » ferait
    chuter le bilan pour une raison qui n'a rien à voir avec l'entraînement.
    """
    ref = date_ref or _date.today()
    if not objectifs_actifs:
        return {"disponible": False, "semaines": [], "metriques": []}

    periode = _periode_par_id(conn, periode_id)
    if not periode:
        return {"disponible": False, "semaines": [], "metriques": [],
                "erreur": "Période introuvable"}

    equipe_id = periode.get("equipe_id")
    if scope and (equipe_id is None or equipe_id not in [str(e) for e in scope]):
        return {"disponible": False, "semaines": [], "metriques": [], "erreur": "Hors périmètre"}

    with conn.cursor() as cur:
        cur.execute("SELECT club_id FROM equipe WHERE id = %s", (equipe_id,))
        row = cur.fetchone()
    club_id = str(row[0]) if row else None

    lundis = _semaines_de(periode["date_debut"], periode["date_fin"])
    lundis = [l for l in lundis if l <= _lundi(ref)]      # une semaine à venir n'a rien à bilan
    realise = _realise_par_semaine(conn, lundis, scope=[equipe_id] if equipe_id else None)

    trajectoire   = _trajectoire_periode(conn, periode.get("objectif_periode_id"))
    postes_cibles = _objectif_periode_postes(conn, club_id, equipe_id, periode["date_debut"])
    referentiel_id = _referentiel_equipe(conn, club_id, equipe_id)

    # Cible d'équipe pour une période de compétition : moyenne des postes, faute de mieux — les
    # cibles y sont par poste, or un bilan d'équipe se lit sur une seule ligne.
    def cible_equipe(metrique, lundi):
        v = trajectoire.get(lundi, {}).get(metrique)
        if v and v.get("min") is not None:
            deltas = _deltas_semaine(conn, equipe_id, lundi)
            return max(0, v["min"] + deltas.get(metrique, 0))
        valeurs = [c[metrique]["min"] for c in postes_cibles.values()
                   if c.get(metrique) and c[metrique].get("min") is not None]
        return round(sum(valeurs) / len(valeurs)) if valeurs else None

    semaines, totaux_prescrit, totaux_realise = [], {}, {}
    for i, lundi in enumerate(lundis):
        joueurs_sem = realise.get(lundi, {})
        cible = cible_equipe("distance_totale", lundi)
        valeurs = [v.get("distance_totale", 0.0) for v in joueurs_sem.values()]
        moyenne = round(sum(valeurs) / len(valeurs)) if valeurs else 0
        atteints = sum(1 for v in valeurs if cible and v >= cible)

        for m in METRIQUES_CUMUL:
            c = cible_equipe(m, lundi)
            if c:
                totaux_prescrit[m] = totaux_prescrit.get(m, 0) + c
            vals = [v.get(m, 0.0) for v in joueurs_sem.values()]
            if vals:
                totaux_realise[m] = totaux_realise.get(m, 0.0) + sum(vals) / len(vals)

        semaines.append({
            "date_lundi":  lundi.isoformat(),
            "no_semaine":  i + 1,
            "phase":       (trajectoire.get(lundi, {}).get("distance_totale") or {}).get("phase"),
            "prescrit_m":  cible,
            "realise_moyen_m": moyenne,
            "ecart_pct":   round((moyenne - cible) / cible * 100, 1) if cible else None,
            "nb_joueurs":  len(valeurs),
            "nb_atteint":  atteints,
            "nb_matchs":   len(_matchs_semaine(conn, equipe_id, lundi)),
        })

    metriques = []
    for m in METRIQUES_CUMUL:
        prescrit = totaux_prescrit.get(m)
        reel = round(totaux_realise.get(m, 0.0))
        metriques.append({
            "metrique":   m,
            "prescrit":   prescrit,
            "realise":    reel,
            "ecart_pct":  round((reel - prescrit) / prescrit * 100, 1) if prescrit else None,
        })

    # Les joueurs les plus en écart : c'est ce qu'on regarde en réunion de fin de bloc, pas la
    # moyenne d'équipe qui masque toujours les deux extrêmes.
    par_joueur: dict = {}
    for lundi, joueurs_sem in realise.items():
        cible = cible_equipe("distance_totale", lundi)
        for jid, v in joueurs_sem.items():
            agg = par_joueur.setdefault(jid, {"realise": 0.0, "prescrit": 0, "semaines": 0})
            agg["realise"] += v.get("distance_totale", 0.0)
            agg["semaines"] += 1
            if cible:
                agg["prescrit"] += cible

    noms = {}
    if par_joueur:
        with conn.cursor() as cur:
            cur.execute("SELECT id, nom, prenom, poste_principal FROM joueur WHERE id = ANY(%s)",
                        (list(par_joueur.keys()),))
            noms = {str(r[0]): (r[1], r[2], r[3]) for r in cur.fetchall()}

    joueurs = []
    for jid, agg in par_joueur.items():
        n = noms.get(jid, ("", "", ""))
        joueurs.append({
            "joueur_id": jid, "nom": n[0], "prenom": n[1], "poste": n[2] or "",
            "realise_m": round(agg["realise"]),
            "prescrit_m": agg["prescrit"] or None,
            "ecart_pct": round((agg["realise"] - agg["prescrit"]) / agg["prescrit"] * 100, 1)
                         if agg["prescrit"] else None,
            "nb_semaines": agg["semaines"],
        })
    joueurs.sort(key=lambda j: (j["ecart_pct"] is None, j["ecart_pct"] or 0))

    sem_evaluees = [s for s in semaines if s["prescrit_m"]]
    return {
        "disponible": True,
        "periode": {"id": periode["id"], "libelle": periode["libelle"], "type": periode["type"],
                    "date_debut": periode["date_debut"].isoformat(),
                    "date_fin": periode["date_fin"].isoformat()},
        "terminee": periode["date_fin"] < ref,
        "referentiel_actif": referentiel_id is not None,
        "objectif_defini": bool(trajectoire or postes_cibles),
        "nb_semaines": len(semaines),
        "nb_semaines_evaluees": len(sem_evaluees),
        "nb_semaines_tenues": sum(1 for s in sem_evaluees
                                  if s["ecart_pct"] is not None and s["ecart_pct"] >= -5),
        "semaines": semaines,
        "metriques": metriques,
        "joueurs": joueurs,
    }
