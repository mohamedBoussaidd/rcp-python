"""
Contexte temporel CENTRALISÉ (saison / période / fraîcheur / blessure).

Source UNIQUE de la règle « pas de données récentes ou hors-saison → pas
d'alerte ». Tous les indicateurs s'appuient dessus au lieu de refaire chacun
leur propre fenêtre temporelle. Tolérant aux migrations non passées (mode
legacy : si les tables saison/effectif n'existent pas, periode_type reste None
et seul le garde-fou de fraîcheur s'applique).
"""
from uuid import UUID
from datetime import date as _date

# Types de période où l'on N'ALERTE PAS (le joueur n'est pas censé être en charge).
_PERIODES_SILENCE = ("TREVE", "INTERSAISON")
# Types de période sans baseline stable → ACWR non alarmant (montée de charge attendue).
_PERIODES_NEUTRALISER_ACWR = ("PREPARATION", "REPRISE")


def _jours_depuis_derniere_donnee(joueur_id: UUID, conn, date_ref=None) -> int | None:
    """Jours écoulés depuis la dernière donnée (séance GPS ou RPE). None = jamais."""
    ref = date_ref or _date.today()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT MAX(d) FROM (
                    SELECT MAX(s.date) AS d
                      FROM donnee_gps dg JOIN seance s ON dg.seance_id = s.id
                      WHERE dg.joueur_id = %s AND dg.distance_totale_m > 0
                    UNION ALL
                    SELECT MAX(date) AS d
                      FROM rpe_seance WHERE joueur_id = %s AND charge IS NOT NULL
                ) t
            """, (str(joueur_id), str(joueur_id)))
            row = cur.fetchone()
    except Exception:
        try: conn.rollback()
        except Exception: pass
        return None
    if not row or row[0] is None:
        return None
    return (ref - row[0]).days


def _blessure_active(joueur_id: UUID, conn, date_ref=None) -> tuple:
    """(blessure_active: bool, jours_restants: int|None) — blessure non RETABLI la plus récente.
    jours_restants < 0 = date de retour prévue dépassée. Tolérant à l'absence de table."""
    ref = date_ref or _date.today()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT date_retour_prevue FROM blessure
                WHERE joueur_id = %s AND statut != 'RETABLI'
                ORDER BY date_blessure DESC LIMIT 1
            """, (str(joueur_id),))
            row = cur.fetchone()
    except Exception:
        try: conn.rollback()
        except Exception: pass
        return (False, None)
    if not row:
        return (False, None)
    drp = row[0]
    return (True, (drp - ref).days if drp else None)


def _contexte_joueur(joueur_id: UUID, cfg: dict, conn, date_ref=None) -> dict:
    """
    Contexte temporel d'un joueur : saison EN_COURS de son équipe, période courante,
    fraîcheur des données, blessure active → un ÉTAT exploitable par tous les calculs.
    `date_ref` (date simulée) permet de se placer à une autre date pour tester la
    temporalité (préparation, trêve…) ; défaut = aujourd'hui.

    États :
      EN_CHARGE   : suivi actif, alertes pleines
      REPRISE     : reprise post-trêve, ACWR neutralisé (baseline en reconstruction)
      INACTIF     : aucune donnée récente (> seuil) → indicateurs N/A, pas d'alerte
      HORS_CHARGE : trêve / intersaison → pas d'alerte
      HORS_SAISON : l'équipe utilise les saisons mais aucune n'est en cours → pas d'alerte
      BLESSE      : blessure active → pas d'alerte de charge (le joueur ne s'entraîne pas)
    """
    ref = date_ref or _date.today()
    saison_debut = None
    periode_type = None
    periode_libelle = None
    hors_saison = False

    try:
        with conn.cursor() as cur:
            # Saison au niveau CLUB (V37) : le club est porté directement par la fiche (j.club_id,
            # Phase 4 — plus de cache j.equipe_id). L'équipe COURANTE du joueur (pour la période)
            # est dérivée de son effectif dans la saison EN_COURS (effectif_saison).
            cur.execute("""
                SELECT
                  (SELECT es.equipe_id FROM effectif_saison es
                     JOIN saison s ON s.id = es.saison_id AND s.statut = 'EN_COURS'
                     WHERE es.joueur_id = j.id
                     ORDER BY es.date_entree DESC NULLS LAST LIMIT 1) AS equipe_id,
                  (SELECT s.date_debut FROM saison s
                     WHERE s.club_id = j.club_id AND s.statut = 'EN_COURS'
                     ORDER BY s.date_debut DESC LIMIT 1) AS encours_debut,
                  (SELECT s.id FROM saison s
                     WHERE s.club_id = j.club_id AND s.statut = 'EN_COURS'
                     ORDER BY s.date_debut DESC LIMIT 1) AS encours_id,
                  EXISTS (SELECT 1 FROM saison s WHERE s.club_id = j.club_id) AS a_saisons
                FROM joueur j WHERE j.id = %s
            """, (str(joueur_id),))
            row = cur.fetchone()
        if row:
            equipe_id, encours_debut, encours_id, a_saisons = row[0], row[1], row[2], row[3]
            if encours_id is not None:                    # une saison EN_COURS existe
                saison_debut = encours_debut
                with conn.cursor() as cur:
                    # Période courante de CETTE équipe dans la saison (clé saison_id + equipe_id).
                    cur.execute("""
                        SELECT type, libelle FROM periode_saison
                        WHERE saison_id = %s AND equipe_id = %s
                          AND %s::date BETWEEN date_debut AND date_fin
                        ORDER BY date_debut DESC LIMIT 1
                    """, (str(encours_id), str(equipe_id), ref))
                    pr = cur.fetchone()
                if pr:
                    periode_type, periode_libelle = pr[0], pr[1]
            elif bool(a_saisons):                         # des saisons existent mais aucune EN_COURS
                hors_saison = True
    except Exception:
        try: conn.rollback()
        except Exception: pass   # tables saison absentes → mode legacy

    jours_inactif = _jours_depuis_derniere_donnee(joueur_id, conn, ref)
    blessure_active, jours_restants = _blessure_active(joueur_id, conn, ref)
    seuil_inactif = int(cfg.get("jours_inactif_max", 10))

    if hors_saison:
        etat = "HORS_SAISON"
    elif periode_type in _PERIODES_SILENCE:
        etat = "HORS_CHARGE"
    elif blessure_active:
        etat = "BLESSE"
    elif jours_inactif is None or jours_inactif > seuil_inactif:
        etat = "INACTIF"
    elif periode_type in _PERIODES_NEUTRALISER_ACWR:
        etat = "REPRISE" if periode_type == "REPRISE" else "EN_CHARGE"
    else:
        etat = "EN_CHARGE"

    return {
        "etat": etat,
        "saison_debut": saison_debut,
        "periode_type": periode_type,
        "periode_libelle": periode_libelle,
        "jours_inactif": jours_inactif,
        "blessure_active": blessure_active,
        "blessure_jours_restants": jours_restants,
        # drapeaux dérivés (pratiques pour les appelants)
        "silence": etat in ("HORS_CHARGE", "HORS_SAISON", "INACTIF", "BLESSE"),
        "neutraliser_acwr": periode_type in _PERIODES_NEUTRALISER_ACWR,
    }


def _poids_a_date(joueur_id: UUID, date_ref, conn) -> tuple:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT hp.poids, j.poids_forme_cible
            FROM historique_poids hp
            JOIN joueur j ON j.id = hp.joueur_id
            WHERE hp.joueur_id = %s AND hp.date <= %s
            ORDER BY hp.date DESC
            LIMIT 1
        """, (str(joueur_id), date_ref))
        row = cur.fetchone()

    if row:
        return (float(row[0]), float(row[1]) if row[1] is not None else None)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT poids_actuel, poids_forme_cible FROM joueur WHERE id = %s",
            (str(joueur_id),)
        )
        row = cur.fetchone()

    if row:
        return (float(row[0]) if row[0] is not None else None,
                float(row[1]) if row[1] is not None else None)
    return (None, None)
