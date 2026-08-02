"""
Qui est dans le périmètre ? — résolution de l'effectif à considérer.

Module volontairement bas dans la chaîne de dépendances : la simulation, l'objectif
hebdomadaire et le résumé d'équipe s'appuient tous les trois dessus. Le ranger dans
« vues d'équipe » créait une dépendance à contre-sens.

La portée (`scope`) arrive déjà résolue par le back Java (cf. `core/scope.py`) : ici on
ne fait que filtrer, jamais autoriser.
"""


def _joueurs_resume(conn, scope=None):
    """
    Périmètre du résumé d'équipe : l'effectif des saisons EN_COURS si la notion de
    saison/effectif existe ET est renseignée ; sinon repli LEGACY sur tous les joueurs
    actifs (non-breaking tant qu'aucune saison n'a été ouverte).
    `scope` (liste d'equipe_id du contexte) restreint aux équipes ciblées. Quand un
    scope est transmis, il fait TOUJOURS foi : résultat scopé même vide (club neuf sans
    effectif), jamais de repli plateforme entière.
    """
    try:
        with conn.cursor() as cur:
            extra = ""; params: list = []
            if scope:
                # Scope par l'équipe d'EFFECTIF (es.equipe_id), pas le cache j.equipe_id :
                # gère le multi-équipe et n'exclut jamais un joueur au cache périmé. Le JOIN
                # sur effectif_saison écarte nativement les fiches staff (aucun effectif).
                extra = " AND es.equipe_id = ANY(%s)"; params.append(scope)
            cur.execute(f"""
                SELECT j.id, j.nom, j.prenom, j.poste_principal
                FROM joueur j
                JOIN effectif_saison es ON es.joueur_id = j.id
                JOIN saison s ON s.id = es.saison_id AND s.statut = 'EN_COURS'
                WHERE j.statut != 'inactif'{extra}
                GROUP BY j.id, j.nom, j.prenom, j.poste_principal
                ORDER BY j.nom, j.prenom
            """, params)
            rows = cur.fetchall()
        if scope:
            return rows
        if rows:
            return rows
    except Exception:
        try: conn.rollback()
        except Exception: pass
        if scope:
            return []
    # Repli legacy (AUCUN scope transmis et aucun effectif renseigné) : la colonne
    # joueur.equipe_id n'existe plus (V51), on ne peut plus scoper par équipe ici
    # → tous les joueurs actifs (chemin pré-effectif only).
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, nom, prenom, poste_principal
            FROM joueur WHERE statut != 'inactif' ORDER BY nom, prenom
        """)
        return cur.fetchall()
