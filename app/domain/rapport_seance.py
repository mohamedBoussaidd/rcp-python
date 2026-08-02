"""
Rapport d'une séance : ce qui était prévu face à ce qui a été fait.

Part de QUI A FAIT LA SÉANCE (l'appel), pas de qui portait un capteur : un joueur présent
mais non équipé disparaissait autrefois du rapport alors que le moteur comptait déjà sa
charge par repli sRPE. Le trou n'était que d'affichage, il est comblé ici.

`HTTPException` est importée volontairement dans ce module métier : la remonter au routeur
imposerait de retraduire les codes et les messages, donc d'en changer — ce que la découpe
s'interdit. À revoir seulement si le besoin d'un second appelant non-HTTP apparaît.
"""
from fastapi import HTTPException
from uuid import UUID
from datetime import date as _date

from app.core.database import get_connection
from app.core.config import _load_config, _normaliser_poste, _objectif_poste, TYPES_MATCH, TYPES_INTENSIF
from app.domain.temps import _parse_date_simulee
from app.domain.contexte import _poids_a_date


def rapport_seance(seance_id: UUID, x_date_simulee: str | None = None):
    # La date du jour décide si la séance a eu lieu (cf. `seance_passee` plus bas) : elle doit
    # suivre l'horloge simulée, sinon un test à une date passée verrait toutes ses séances
    # « futures » et le rapport se viderait.
    date_ref = _parse_date_simulee(x_date_simulee) or _date.today()
    try:
        with get_connection() as conn:
            cfg = _load_config(conn)

            with conn.cursor() as cur:
                cur.execute("""
                    SELECT s.id, s.date, ts.code, ts.libelle, s.type_seance_id,
                           s.objectif, s.objectif_distance_m, s.objectif_intensite,
                           s.objectif_distance_haute_intensite_m,
                           s.duree_minutes, ts.duree_theorique_min
                    FROM seance s
                    JOIN type_seance ts ON ts.id = s.type_seance_id
                    WHERE s.id = %s
                """, (str(seance_id),))
                seance = cur.fetchone()

            if not seance:
                raise HTTPException(status_code=404, detail="Séance introuvable")

            type_seance_id = seance[4]
            type_code      = seance[2]
            seance_date    = seance[1]

            # Objectif d'équipe saisi par le préparateur (Phase 1), tous types
            objectif_texte             = seance[5]
            objectif_distance_m        = int(seance[6]) if seance[6] is not None else None
            objectif_intensite         = int(seance[7]) if seance[7] is not None else None
            objectif_distance_hi_m     = int(seance[8]) if seance[8] is not None else None

            # Durée de référence de la séance, pour proratiser l'objectif d'équipe par
            # joueur selon son temps de jeu réel. Chaîne de repli (jamais nulle en pratique) :
            # durée planifiée de la séance → somme du déroulé d'exercices →
            # durée théorique du type → (dernier repli) max des durées réelles GPS (plus bas).
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT SUM(COALESCE(se.duree_minutes, e.duree_minutes))
                    FROM seance_exercice se
                    JOIN exercice e ON e.id = se.exercice_id
                    WHERE se.seance_id = %s
                """, (str(seance_id),))
                ref_row = cur.fetchone()
            duree_planifiee = float(seance[9])  if seance[9]  else None
            duree_deroule   = float(ref_row[0]) if ref_row and ref_row[0] else None
            duree_type      = float(seance[10]) if seance[10] else None
            duree_reference = duree_planifiee or duree_deroule or duree_type

            # Le rapport part de QUI A FAIT LA SÉANCE, pas de qui portait un capteur.
            # Auparavant la liste sortait de `donnee_gps` : un joueur présent à l'appel mais non
            # équipé disparaissait purement et simplement du rapport, même s'il avait rempli son
            # RPE. Le moteur, lui, savait déjà compter sa charge (repli GPS↔sRPE) — le trou était
            # uniquement d'affichage.
            # Une séance qui N'A PAS ENCORE EU LIEU n'a pas de participants. L'appel se fait par
            # exception (aucune ligne en base = présent), donc sur une séance future le COALESCE
            # ci-dessous renverrait TOUT L'EFFECTIF en « présent sans capteur » : un tableau plein
            # de joueurs et de zéros, qui se lit comme une panne. On se limite alors aux porteurs
            # de données réelles — il peut y en avoir (import anticipé, RPE saisi d'avance), et
            # une donnée mesurée ne disparaît jamais.
            seance_passee = seance_date is not None and seance_date <= date_ref
            branche_effectif = """
                        UNION
                        -- …plus l'effectif que l'appel dit présent. Un statut non participant
                        -- (absent, excusé, au soin) sort de la liste — sauf s'il a des données GPS,
                        -- auquel cas la branche du dessus le rattrape : c'est une CONTRADICTION à
                        -- montrer, pas à masquer (erreur d'appel ou mauvais appariement de nom).
                        SELECT es.joueur_id
                        FROM effectif_saison es
                        JOIN saison sa ON sa.id = es.saison_id AND sa.statut = 'EN_COURS'
                        LEFT JOIN presence pr ON pr.seance_id = %s AND pr.joueur_id = es.joueur_id
                        WHERE es.equipe_id = (SELECT equipe_id FROM seance WHERE id = %s)
                          AND COALESCE(pr.statut, 'PRESENT') NOT IN ('ABSENT', 'EXCUSE', 'SOIN')
            """ if seance_passee else ""
            # 3 %s dans la branche effectif (présence, séance) + 1 en tête + 3 dans les jointures.
            nb_params = 6 if seance_passee else 4

            with conn.cursor() as cur:
                cur.execute(f"""
                    WITH participants AS (
                        -- Tout joueur porteur de données sur la séance, même hors effectif : on ne
                        -- fait jamais disparaître une donnée mesurée.
                        SELECT dg.joueur_id AS jid FROM donnee_gps dg WHERE dg.seance_id = %s
                        {branche_effectif}
                    )
                    SELECT j.id, j.nom, j.prenom, j.poste_principal,
                           dg.distance_totale_m, dg.duree_minutes,
                           dg.vitesse_max_kmh, dg.nb_sprints_24kmh,
                           COALESCE(pr.statut, 'PRESENT') AS statut_appel,
                           r.charge, r.rpe
                    FROM participants p
                    JOIN joueur j ON j.id = p.jid
                    LEFT JOIN presence  pr ON pr.seance_id = %s AND pr.joueur_id = j.id
                    LEFT JOIN donnee_gps dg ON dg.seance_id = %s AND dg.joueur_id = j.id
                    LEFT JOIN rpe_seance r  ON r.seance_id  = %s AND r.joueur_id  = j.id
                    ORDER BY j.nom, j.prenom
                """, (str(seance_id),) * nb_params)
                players = cur.fetchall()

            # Dernier repli : sans déroulé ni durée planifiée (séance-coquille GPS ou match),
            # on prend la plus longue durée réelle jouée (≈ un joueur ayant fait toute la séance).
            if not duree_reference and players:
                duree_reference = max((float(p[5]) for p in players if p[5]), default=None)

            sous_norme_pct = cfg.get("seuil_sous_norme_pct", 20.0)
            sur_norme_pct  = cfg.get("seuil_sur_norme_pct",  20.0)
            corr_pct_kg    = cfg.get("correction_surpoids_pct_par_kg",  2.0)
            corr_pct_max   = cfg.get("correction_surpoids_plafond_pct", 20.0)
            recence_j      = int(cfg.get("baseline_recence_jours", 90))

            lignes = []
            for p in players:
                joueur_id     = p[0]
                poste         = _normaliser_poste(p[3] or "")
                dist_reelle   = float(p[4]) if p[4] is not None else None
                duree_reelle  = float(p[5]) if p[5] is not None else None
                poids_actuel, poids_cible = _poids_a_date(joueur_id, seance_date, conn)

                # Baseline personnelle = m/min moyen des 10 dernières séances RÉALISÉES (H.5) du
                # même type, dans la fenêtre de récence (écarte une baseline trop ancienne, ex.
                # d'avant une longue blessure). `_n` = taille réelle. La variante « toutes séances
                # confondues » sert de repli affichable quand le type est trop mince.
                def _baseline_rapport(meme_type: bool):
                    filtre_type = "AND s.type_seance_id = %s" if meme_type else ""
                    params = [str(joueur_id)]
                    if meme_type:
                        params.append(str(type_seance_id))
                    params += [str(seance_id), seance_date]
                    with conn.cursor() as cur:
                        cur.execute(f"""
                            SELECT AVG(sub.ratio), COUNT(*) FROM (
                                SELECT dg.distance_totale_m / NULLIF(dg.duree_minutes, 0) AS ratio
                                FROM donnee_gps dg
                                JOIN seance s ON dg.seance_id = s.id
                                WHERE dg.joueur_id = %s
                                  {filtre_type}
                                  AND dg.seance_id != %s
                                  AND s.statut = 'REALISEE'
                                  AND s.date >= %s::date - INTERVAL '{recence_j} days'
                                  AND dg.duree_minutes > 0
                                  AND dg.distance_totale_m > 0
                                  -- Une séance où le joueur a été VOLONTAIREMENT ménagé ne peut pas
                                  -- servir de norme : elle abaisserait sa baseline, et son premier
                                  -- retour à la normale ressortirait ensuite en surcharge.
                                  AND NOT EXISTS (
                                      SELECT 1 FROM presence pr
                                      WHERE pr.seance_id = s.id
                                        AND pr.joueur_id = dg.joueur_id
                                        AND pr.statut = 'ADAPTE'
                                  )
                                ORDER BY s.date DESC
                                LIMIT 10
                            ) sub
                        """, params)
                        r = cur.fetchone()
                    ratio = float(r[0]) if r and r[0] is not None else None
                    n     = int(r[1])   if r and r[1] else 0
                    return ratio, n

                avg_ratio,   baseline_n   = _baseline_rapport(True)
                avg_ratio_g, baseline_n_g = _baseline_rapport(False)
                dist_attendue   = round(avg_ratio   * duree_reelle, 0) if avg_ratio   and duree_reelle else None
                dist_attendue_g = round(avg_ratio_g * duree_reelle, 0) if avg_ratio_g and duree_reelle else None

                delta_m = delta_pct = None
                statut  = "SANS_BASELINE"

                if dist_reelle is not None and dist_attendue and dist_attendue > 0:
                    delta_m   = round(dist_reelle - dist_attendue, 0)
                    delta_pct = round((delta_m / dist_attendue) * 100, 1)
                    statut    = ("SOUS_NORME" if delta_pct < -sous_norme_pct
                                 else "SUR_NORME" if delta_pct > sur_norme_pct
                                 else "DANS_NORME")

                # Objectif séance (équipe) au prorata du temps joué — tous types
                objectif_seance_m = atteint_objectif_seance = None
                if objectif_distance_m and duree_reference and duree_reelle:
                    objectif_seance_m = round(objectif_distance_m * (duree_reelle / duree_reference), 0)
                    if dist_reelle is not None:
                        atteint_objectif_seance = dist_reelle >= objectif_seance_m

                objectif_m = ratio_objectif = ratio_objectif_original = None
                correction_poids_pct = ecart_poids_kg = atteint_objectif = None

                if type_code in ('MATCH', 'MATCH_AMICAL') and duree_reelle:
                    ratio_objectif_original = _objectif_poste(poste, cfg)
                    if ratio_objectif_original is not None:
                        correction_poids_pct = None
                        if poids_actuel is not None and poids_cible is not None:
                            ecart_kg = max(0.0, poids_actuel - poids_cible)
                            if ecart_kg >= 0.5:
                                correction_poids_pct = round(min(ecart_kg * corr_pct_kg, corr_pct_max), 1)
                                ecart_poids_kg       = round(ecart_kg, 1)
                        coeff          = 1.0 - (correction_poids_pct or 0.0) / 100.0
                        ratio_objectif = round(ratio_objectif_original * coeff, 2)
                        objectif_m     = round(ratio_objectif * duree_reelle, 0)
                        atteint_objectif = dist_reelle >= objectif_m if dist_reelle is not None else None

                lignes.append({
                    "joueur_id":               str(joueur_id),
                    "nom":                     p[1],
                    "prenom":                  p[2],
                    "poste":                   p[3] or "",
                    "duree_minutes":           int(duree_reelle) if duree_reelle else None,
                    "distance_reelle":         dist_reelle,
                    "distance_attendue":         dist_attendue,
                    "baseline_n":                baseline_n,
                    "distance_attendue_globale": dist_attendue_g,
                    "baseline_n_globale":        baseline_n_g,
                    "ratio_reel":              round(dist_reelle / duree_reelle, 1) if dist_reelle and duree_reelle else None,
                    "delta_m":                 delta_m,
                    "delta_pct":               delta_pct,
                    "statut":                  statut,
                    "vitesse_max":             float(p[6]) if p[6] is not None else None,
                    "nb_sprints":              int(p[7])   if p[7] is not None else None,
                    "objectif_m":              objectif_m,
                    "ratio_objectif":          ratio_objectif,
                    "ratio_objectif_original": ratio_objectif_original,
                    "correction_poids_pct":    correction_poids_pct,
                    "ecart_poids_kg":          ecart_poids_kg,
                    "atteint_objectif":        atteint_objectif,
                    "objectif_seance_m":       objectif_seance_m,
                    "atteint_objectif_seance": atteint_objectif_seance,
                    # Contexte d'appel : de quoi expliquer une ligne sans kilomètres au lieu de la
                    # faire disparaître. `sans_capteur` = présent mais aucune donnée GPS.
                    # `contradiction` = déclaré non participant ALORS QU'il a des données : à
                    # vérifier (erreur d'appel, ou mauvais rattachement de nom à l'import).
                    "statut_appel":            p[8],
                    "sans_capteur":            p[4] is None,
                    "contradiction":           p[8] in ('ABSENT', 'EXCUSE', 'SOIN') and p[4] is not None,
                    "charge_rpe":              float(p[9]) if p[9] is not None else None,
                    "intensite_rpe":           int(p[10]) if p[10] is not None else None,
                })

        return {
            "seance_id":    str(seance_id),
            "date":         str(seance[1]),
            "type_code":    type_code,
            "type_libelle": seance[3],
            "nb_joueurs":   len(lignes),
            # `nb_joueurs` = participants (appel), `nb_porteurs` = lignes réellement mesurées.
            # Toute moyenne GPS doit se diviser par nb_porteurs : depuis que les présents sans
            # capteur figurent dans la liste, diviser par nb_joueurs ferait chuter les moyennes
            # d'équipe sans qu'aucun joueur n'ait moins couru.
            "nb_porteurs":  sum(1 for l in lignes if not l["sans_capteur"]),
            "nb_sans_capteur":  sum(1 for l in lignes if l["sans_capteur"]),
            "nb_contradictions": sum(1 for l in lignes if l["contradiction"]),
            # Objectif d'équipe de la séance (cible prépa, tous types)
            "objectif":                            objectif_texte,
            "objectif_distance_m":                 objectif_distance_m,
            "objectif_intensite":                  objectif_intensite,
            "objectif_distance_haute_intensite_m": objectif_distance_hi_m,
            "duree_reference_minutes":             int(duree_reference) if duree_reference else None,
            "lignes":       lignes,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
