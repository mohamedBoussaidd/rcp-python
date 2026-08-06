"""
Lecture du référentiel de charge adopté par un club (« Attendu »).

Répond à « qu'est-ce qui est NORMAL pour ce joueur, à son poste, au niveau de son équipe ? ».
C'est la seule référence de l'application qui soit EXTÉRIEURE au joueur : tout le reste — charge
cible, ACWR, dérives — se calcule sur son propre historique, et sait donc dire « il s'entraîne
comme d'habitude » sans jamais pouvoir dire « il s'entraîne comme il faudrait ».

Tolérant à l'absence des tables (migrations V101/V102 non passées) : on renvoie alors des
dictionnaires vides et les écrans retombent exactement sur leur comportement d'avant.
"""
from uuid import UUID

from app.core.config import POSTE_ALIASES, TYPES_MATCH

# Postes de la fiche joueur → postes de RÉFÉRENCE (rabattement 11 → 6).
# Les référentiels du métier ne distinguent pas un latéral droit d'un gauche, ni un milieu
# défensif d'un offensif : les exigences sont les mêmes. Sans ce rabattement, un seed par poste
# de référence ne couvrirait que la moitié d'un effectif.
POSTE_VERS_REFERENCE: dict[str, str] = {
    "gardien":           "gardien",
    "defenseur_central": "defenseur_central",
    "lateral_droit":     "lateral",
    "lateral_gauche":    "lateral",
    "milieu_defensif":   "milieu_axial",
    "milieu_central":    "milieu_axial",
    "milieu_offensif":   "milieu_axial",
    "ailier_droit":      "ailier",
    "ailier_gauche":     "ailier",
    "attaquant":         "attaquant",
    "avant_centre":      "attaquant",
}

# Métriques de charge, dans le même vocabulaire que l'enum Java `MetriqueCharge`.
# `expo_vmax` est un PIC, pas un cumul : sa cible s'exprime en % du record personnel.
METRIQUES_CUMUL = ("distance_totale", "distance_15", "distance_19",
                   "distance_24_28", "distance_28", "nb_sprints")
METRIQUE_EXPO = "expo_vmax"

# Expression SQL de chaque métrique sur `donnee_gps`. La tranche 24-28 est la seule dérivée :
# la base stocke des seuils cumulatifs, le document de référence raisonne en tranche.
SQL_METRIQUE: dict[str, str] = {
    "distance_totale": "COALESCE(dg.distance_totale_m, 0)",
    "distance_15":     "COALESCE(dg.distance_15kmh_m, 0)",
    "distance_19":     "COALESCE(dg.distance_19kmh_m, 0)",
    "distance_24_28":  "GREATEST(COALESCE(dg.distance_sprint_24kmh_m, 0) "
                       "- COALESCE(dg.distance_sprint_28kmh_m, 0), 0)",
    "distance_28":     "COALESCE(dg.distance_sprint_28kmh_m, 0)",
    "nb_sprints":      "COALESCE(dg.nb_sprints_24kmh, 0)",
}


def _poste_reference(poste: str | None) -> str | None:
    """Poste de référence d'un joueur, ou None si le poste est vide ou inconnu.

    Les abréviations (« MC », « LD », « DC »…) passent d'abord par `POSTE_ALIASES`, la même table
    que le reste des calculs : sans elle, un effectif importé en abrégé n'aurait aucun « Attendu ».

    None est une réponse légitime : un poste absent est une donnée manquante, pas un défenseur
    central. L'appelant n'affichera alors aucune colonne « Attendu » pour ce joueur.
    """
    if not poste:
        return None
    cle = poste.strip().lower().replace(" ", "_").replace("-", "_")
    cle = POSTE_ALIASES.get(cle, cle)
    return POSTE_VERS_REFERENCE.get(cle)


def _referentiel_equipe(conn, club_id, equipe_id) -> str | None:
    """
    Référentiel appliqué à une équipe : surcharge de l'équipe, sinon défaut du club, sinon rien.

    « Rien » est un cas normal (club qui n'a rien adopté, module désactivé) et doit rester
    silencieux : aucune colonne « Attendu », jamais une valeur inventée.
    """
    if club_id is None:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT referentiel_id FROM club_referentiel
                WHERE club_id = %s AND (equipe_id = %s OR equipe_id IS NULL)
                ORDER BY equipe_id NULLS LAST
                LIMIT 1
            """, (str(club_id), str(equipe_id) if equipe_id else None))
            row = cur.fetchone()
    except Exception:
        try: conn.rollback()
        except Exception: pass
        return None            # tables V101 absentes → mode legacy
    return str(row[0]) if row else None


def _attendus_par_poste(conn, referentiel_id, contexte: str = "SEMAINE") -> dict:
    """
    {poste_reference: {metrique: (min, max)}} pour un contexte donné.

    Le contexte SEMAINE INCLUT le match — c'est la lecture du document de référence. L'entraînement
    n'est donc jamais stocké : il se dérive (semaine − minutes réellement jouées).
    """
    if not referentiel_id:
        return {}
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT poste, metrique, valeur_min, valeur_max
                FROM referentiel_objectif_valeur
                WHERE referentiel_id = %s AND contexte = %s
            """, (referentiel_id, contexte))
            rows = cur.fetchall()
    except Exception:
        try: conn.rollback()
        except Exception: pass
        return {}
    res: dict = {}
    for poste, metrique, vmin, vmax in rows:
        res.setdefault(poste, {})[metrique] = (
            int(vmin) if vmin is not None else None,
            int(vmax) if vmax is not None else None,
        )
    return res


def _objectif_periode_courant(conn, club_id, equipe_id, date_ref) -> dict:
    """
    Objectif PRESCRIT de la semaine, s'il en existe un : la trajectoire posée sur la période en
    cours, à la semaine où l'on se trouve.

    Renvoie {metrique: {"min", "max", "priorite", "phase"}}, ou {} si aucune période de la saison
    en cours ne porte d'objectif à cette date. Trêve et intersaison n'en portent jamais : le
    joueur n'y est pas censé être en charge.
    """
    if club_id is None or equipe_id is None:
        return {}
    try:
        with conn.cursor() as cur:
            # La semaine retenue est celle dont le lundi est le plus proche AVANT `date_ref` :
            # une trajectoire est figée à la semaine, elle ne s'interpole pas au jour le jour.
            cur.execute("""
                SELECT v.metrique, v.valeur_min, v.valeur_max, v.priorite, v.phase_nom
                FROM objectif_periode o
                JOIN periode_saison p ON p.id = o.periode_id
                JOIN saison s         ON s.id = p.saison_id AND s.statut = 'EN_COURS'
                JOIN objectif_periode_valeur v ON v.objectif_periode_id = o.id
                WHERE o.club_id = %s AND p.equipe_id = %s
                  AND %s::date BETWEEN p.date_debut AND p.date_fin
                  AND v.no_semaine IS NOT NULL
                  AND v.date_lundi = (
                      SELECT MAX(v2.date_lundi) FROM objectif_periode_valeur v2
                      WHERE v2.objectif_periode_id = o.id AND v2.date_lundi <= %s::date
                  )
            """, (str(club_id), str(equipe_id), date_ref, date_ref))
            rows = cur.fetchall()
    except Exception:
        try: conn.rollback()
        except Exception: pass
        return {}
    return {
        m: {"min": int(vmin) if vmin is not None else None,
            "max": int(vmax) if vmax is not None else None,
            "priorite": prio, "phase": phase}
        for (m, vmin, vmax, prio, phase) in rows
    }


def _objectif_periode_postes(conn, club_id, equipe_id, date_ref) -> dict:
    """
    Cibles de COMPÉTITION de la période en cours : {poste: {metrique: {...}}}.

    En championnat la cible est un régime, pas une montée : elle est portée par poste et vaut
    pour toute la période, d'où l'absence de numéro de semaine.
    """
    if club_id is None or equipe_id is None:
        return {}
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT v.poste, v.metrique, v.valeur_min, v.valeur_max, v.priorite, v.phase_nom
                FROM objectif_periode o
                JOIN periode_saison p ON p.id = o.periode_id
                JOIN saison s         ON s.id = p.saison_id AND s.statut = 'EN_COURS'
                JOIN objectif_periode_valeur v ON v.objectif_periode_id = o.id
                WHERE o.club_id = %s AND p.equipe_id = %s
                  AND %s::date BETWEEN p.date_debut AND p.date_fin
                  AND v.poste IS NOT NULL
            """, (str(club_id), str(equipe_id), date_ref))
            rows = cur.fetchall()
    except Exception:
        try: conn.rollback()
        except Exception: pass
        return {}
    res: dict = {}
    for poste, metrique, vmin, vmax, prio, phase in rows:
        res.setdefault(poste, {})[metrique] = {
            "min": int(vmin) if vmin is not None else None,
            "max": int(vmax) if vmax is not None else None,
            "priorite": prio, "phase": phase,
        }
    return res


def _cout_match(conn, referentiel_id) -> dict:
    """
    Ce que « coûte » un match par métrique, en unité de la métrique : moyenne des postes du
    référentiel (contexte MATCH).

    Sert à dériver la part d'entraînement d'une semaine — la cible hebdo INCLUT le match, donc
    sans cette soustraction le préparateur croit disposer de 34 km d'entraînement là où il lui en
    reste 14. Moyenne équipe et non par poste : on planifie une séance pour un groupe.
    """
    attendus = _attendus_par_poste(conn, referentiel_id, "MATCH")
    if not attendus:
        return {}
    sommes: dict = {}
    for valeurs in attendus.values():
        for metrique, (vmin, vmax) in valeurs.items():
            milieu = None
            if vmin is not None and vmax is not None:
                milieu = (vmin + vmax) // 2
            elif vmin is not None:
                milieu = vmin
            elif vmax is not None:
                milieu = vmax
            if milieu is not None:
                sommes.setdefault(metrique, []).append(milieu)
    return {m: round(sum(v) / len(v)) for m, v in sommes.items() if v}


def _matchs_semaine(conn, equipe_id, lundi) -> list:
    """
    Dates des matchs de l'équipe dans la semaine du `lundi` (lundi → dimanche).

    Lu sur les SÉANCES, pas sur `match_prepa` : le dossier de match appartient au module MATCH,
    qui est un add-on distinct d'OBJECTIFS_PERFORMANCE. Un club qui n'aurait que le second aurait
    eu un arbitrage double match structurellement aveugle. La séance, elle, est du socle — et
    depuis V104 les deux sont tenus synchronisés, donc les deux lectures coïncident. C'est aussi
    la source du badge MD-x (`SeanceFicheService`) et du signal de fatigue de match : une seule
    réponse à « quand joue-t-on ».
    """
    if equipe_id is None:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT s.date
                FROM seance s JOIN type_seance t ON t.id = s.type_seance_id
                WHERE s.equipe_id = %s AND t.code = ANY(%s)
                  AND s.date BETWEEN %s::date AND %s::date + 6
                ORDER BY s.date
            """, (str(equipe_id), list(TYPES_MATCH), lundi, lundi))
            return [r[0] for r in cur.fetchall() if r[0] is not None]
    except Exception:
        try: conn.rollback()
        except Exception: pass
        return []


def _arbitrage_semaine(conn, equipe_id, lundi) -> dict:
    """
    Décision prise sur cette semaine ({} si aucune) : {"choix", "nb_matchs", "note"}.

    Écrite par Java (l'arbitrage est un geste, pas un calcul) ; ici on ne fait que la lire pour
    dire à l'écran ce qui a été décidé.
    """
    if equipe_id is None:
        return {}
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT choix, nb_matchs, note FROM arbitrage_semaine
                WHERE equipe_id = %s AND date_lundi = %s::date
            """, (str(equipe_id), lundi))
            row = cur.fetchone()
    except Exception:
        try: conn.rollback()
        except Exception: pass
        return {}
    if not row:
        return {}
    return {"choix": row[0], "nb_matchs": int(row[1] or 0), "note": row[2]}


def _deltas_semaine(conn, equipe_id, lundi) -> dict:
    """
    Somme des reports qui CIBLENT cette semaine, par métrique — d'où qu'ils viennent.

    C'est ce qui permet de ne jamais réécrire l'objectif de période : le Retenu vaut « prescrit +
    deltas », le prescrit reste lisible, et retirer un arbitrage rétablit la trajectoire d'origine.
    Une semaine peut recevoir le report de plusieurs arbitrages (deux semaines à deux matchs
    rapprochées) : on additionne.
    """
    if equipe_id is None:
        return {}
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT r.metrique, SUM(r.delta)
                FROM arbitrage_semaine_report r
                JOIN arbitrage_semaine a ON a.id = r.arbitrage_id
                WHERE a.equipe_id = %s AND r.date_lundi_cible = %s::date
                GROUP BY r.metrique
            """, (str(equipe_id), lundi))
            return {m: int(d or 0) for m, d in cur.fetchall()}
    except Exception:
        try: conn.rollback()
        except Exception: pass
        return {}


def _origines_report(conn, equipe_id, lundi) -> list:
    """
    D'où viennent les deltas de cette semaine : [{"semaine_source", "choix", "delta"}] sur la
    métrique de volume. Sert à écrire « +2 km reportés de la semaine du 10/03 » plutôt qu'un
    chiffre sans origine — un ajustement qu'on ne sait pas expliquer n'est pas exploitable.
    """
    if equipe_id is None:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT a.date_lundi, a.choix, r.delta
                FROM arbitrage_semaine_report r
                JOIN arbitrage_semaine a ON a.id = r.arbitrage_id
                WHERE a.equipe_id = %s AND r.date_lundi_cible = %s::date
                  AND r.metrique = 'distance_totale'
                ORDER BY a.date_lundi
            """, (str(equipe_id), lundi))
            return [{"semaine_source": d, "choix": c, "delta": int(v or 0)}
                    for (d, c, v) in cur.fetchall()]
    except Exception:
        try: conn.rollback()
        except Exception: pass
        return []


def _periode_de(conn, equipe_id, date_ref) -> dict:
    """
    Période de saison de l'équipe couvrant `date_ref` — même résolution que `contexte.py`.
    {} si aucune. Renvoie aussi l'objectif de période s'il existe, pour éviter un second aller-retour.
    """
    if equipe_id is None:
        return {}
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT p.id, p.libelle, p.type, p.date_debut, p.date_fin, o.id
                FROM periode_saison p
                JOIN saison s ON s.id = p.saison_id AND s.statut = 'EN_COURS'
                LEFT JOIN objectif_periode o ON o.periode_id = p.id
                WHERE p.equipe_id = %s AND %s::date BETWEEN p.date_debut AND p.date_fin
                ORDER BY p.date_debut
                LIMIT 1
            """, (str(equipe_id), date_ref))
            row = cur.fetchone()
    except Exception:
        try: conn.rollback()
        except Exception: pass
        return {}
    if not row:
        return {}
    return {"id": str(row[0]), "libelle": row[1], "type": row[2],
            "date_debut": row[3], "date_fin": row[4],
            "objectif_periode_id": str(row[5]) if row[5] else None}


def _periode_par_id(conn, periode_id) -> dict:
    """Idem `_periode_de` mais par identifiant — le bilan cible une période nommément."""
    if not periode_id:
        return {}
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT p.id, p.libelle, p.type, p.date_debut, p.date_fin, o.id, p.equipe_id
                FROM periode_saison p
                LEFT JOIN objectif_periode o ON o.periode_id = p.id
                WHERE p.id = %s
            """, (str(periode_id),))
            row = cur.fetchone()
    except Exception:
        try: conn.rollback()
        except Exception: pass
        return {}
    if not row:
        return {}
    return {"id": str(row[0]), "libelle": row[1], "type": row[2],
            "date_debut": row[3], "date_fin": row[4],
            "objectif_periode_id": str(row[5]) if row[5] else None,
            "equipe_id": str(row[6]) if row[6] else None}


def _trajectoire_periode(conn, objectif_periode_id) -> dict:
    """
    Trajectoire prescrite d'une période : {date_lundi: {metrique: {"min","max","priorite","phase"}}}.

    Vide pour une période de compétition (ses cibles sont par poste, pas par semaine) — l'appelant
    retombe alors sur `_objectif_periode_postes`.
    """
    if not objectif_periode_id:
        return {}
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT date_lundi, metrique, valeur_min, valeur_max, priorite, phase_nom
                FROM objectif_periode_valeur
                WHERE objectif_periode_id = %s AND no_semaine IS NOT NULL AND date_lundi IS NOT NULL
                ORDER BY date_lundi
            """, (str(objectif_periode_id),))
            rows = cur.fetchall()
    except Exception:
        try: conn.rollback()
        except Exception: pass
        return {}
    res: dict = {}
    for lundi, metrique, vmin, vmax, prio, phase in rows:
        res.setdefault(lundi, {})[metrique] = {
            "min": int(vmin) if vmin is not None else None,
            "max": int(vmax) if vmax is not None else None,
            "priorite": prio, "phase": phase,
        }
    return res


def _club_equipe_du_scope(conn, scope) -> tuple:
    """(club_id, equipe_id) du scope courant. equipe_id n'est renseigné que si le scope en vise UNE.

    Un scope multi-équipes ne peut pas porter d'« Attendu » cohérent : deux équipes peuvent être
    sur deux référentiels différents. On renvoie alors club sans équipe, et l'appelant se contente
    du référentiel par défaut du club.
    """
    if not scope:
        return (None, None)
    equipe_id = scope[0] if len(scope) == 1 else None
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT club_id FROM equipe WHERE id = %s", (scope[0],))
            row = cur.fetchone()
    except Exception:
        try: conn.rollback()
        except Exception: pass
        return (None, None)
    return (str(row[0]) if row else None, equipe_id)
