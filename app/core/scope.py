"""
Résolution de la portée d'une vue d'équipe à partir des en-têtes de contexte.

Le back Java a DÉJÀ résolu la portée autorisée (ScopeResolver) avant de nous appeler :
Python ne fait aucun contrôle d'accès, il filtre seulement ce qu'on lui demande.
"""


def _equipes_scope(x_contexte_equipes, x_contexte_club, conn):
    """
    Équipes sur lesquelles scoper une vue d'équipe, d'après les en-têtes de contexte transmis
    par le BACK Java (qui a déjà résolu la portée autorisée via ScopeResolver — Python ne fait
    que filtrer ce que le back lui demande) :
      X-Contexte-Equipes (CSV d'ids) prioritaire, sinon toutes les équipes de X-Contexte-Club.
    Retourne une liste d'equipe_id (str) ou None = pas de scoping (le back n'a rien transmis).
    """
    if x_contexte_equipes:
        ids = [e.strip() for e in x_contexte_equipes.split(",") if e.strip()]
        return ids or None
    if x_contexte_club and x_contexte_club.strip():
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM equipe WHERE club_id = %s", (x_contexte_club.strip(),))
                ids = [str(r[0]) for r in cur.fetchall()]
            return ids or None
        except Exception:
            try: conn.rollback()
            except Exception: pass
    return None
