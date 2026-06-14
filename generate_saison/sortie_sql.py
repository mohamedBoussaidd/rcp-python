"""
Sortie SQL optionnelle (LOCAL uniquement) — interdite en --env prod.

Écrit un fichier .sql idempotent couvrant le cœur GPS : joueurs, séances et
données GPS (colonnes confirmées, cf. seed_gps_test.py / import_gps.py). Le
ressenti subjectif (wellness/RPE), les pesées, blessures et le module tactique
passent par l'API (champs calculés / saisie côté joueur) et ne sont pas inclus
ici. Voir README.

Si --equipe-id est fourni, joueurs et séances sont rattachés à cette équipe
(pour apparaître dans les vues multi-tenant) ; sinon equipe_id = NULL.
"""

from __future__ import annotations

import uuid
from datetime import date

from . import catalog
from .simulation import SaisonSimulee


def _sql_str(v) -> str:
    if v is None:
        return "NULL"
    return "'" + str(v).replace("'", "''") + "'"


def _sql_num(v) -> str:
    return "NULL" if v is None else str(v)


def ecrire_sql(saison: SaisonSimulee, chemin: str, equipe_id: str | None = None) -> str:
    eq = _sql_str(equipe_id)
    lignes: list[str] = [
        "-- Jeu de données démo (cœur GPS) — généré par generate_saison",
        "-- Local uniquement. Wellness/RPE/pesées/blessures/tactique = via l'API.",
        "BEGIN;",
        "",
    ]

    # UUID stables pour relier les FK dans le fichier.
    jid = {j.nom_complet: str(uuid.uuid4()) for j in saison.effectif}
    sid = {id(s): str(uuid.uuid4()) for s in saison.seances}

    lignes.append("-- ── Joueurs ──")
    for j in saison.effectif:
        lignes.append(
            "INSERT INTO joueur (id, nom, prenom, date_naissance, poids_actuel, "
            "poids_forme_cible, taille, pied_fort, poste_principal, profil_athletique, "
            "statut, date_arrivee_club, equipe_id) VALUES ("
            f"{_sql_str(jid[j.nom_complet])}, {_sql_str(j.nom)}, {_sql_str(j.prenom)}, "
            f"{_sql_str(j.date_naissance.isoformat())}, {_sql_num(j.poids_forme_kg)}, "
            f"{_sql_num(j.poids_forme_kg)}, {_sql_num(j.taille_cm)}, {_sql_str(j.pied_fort)}, "
            f"{_sql_str(catalog.POSTE_DB[j.poste])}, {_sql_str(j.profil_athletique)}, 'actif', "
            f"{_sql_str(saison.params.debut_saison.isoformat())}, {eq});"
        )

    lignes.append("\n-- ── Séances ──")
    for s in saison.seances:
        type_sub = f"(SELECT id FROM type_seance WHERE code = {_sql_str(s.type_code)})"
        adv = _sql_str(s.adversaire) if s.est_match else "NULL"
        dom = _sql_str("DOMICILE" if s.domicile else "EXTERIEUR") if s.est_match else "NULL"
        comp = _sql_str("Championnat") if s.est_match else "NULL"
        lignes.append(
            "INSERT INTO seance (id, type_seance_id, date, statut, equipe_id, "
            "adversaire, competition, domicile_exterieur) VALUES ("
            f"{_sql_str(sid[id(s)])}, {type_sub}, {_sql_str(s.date.isoformat())}, "
            f"'REALISEE', {eq}, {adv}, {comp}, {dom});"
        )

    lignes.append("\n-- ── Données GPS ──")
    for g in saison.gps:
        lignes.append(
            "INSERT INTO donnee_gps (joueur_id, seance_id, duree_minutes, distance_totale_m, "
            "distance_15kmh_m, distance_19kmh_m, distance_sprint_24kmh_m, distance_sprint_28kmh_m, "
            "nb_sprints_24kmh, vitesse_max_kmh, nb_accelerations, nb_freinages, ratio_distance_min) "
            "VALUES ("
            f"{_sql_str(jid[g.joueur.nom_complet])}, {_sql_str(sid[id(g.seance)])}, "
            f"{_sql_num(g.duree_minutes)}, {_sql_num(g.distance_totale_m)}, "
            f"{_sql_num(g.distance_15kmh_m)}, {_sql_num(g.distance_19kmh_m)}, "
            f"{_sql_num(g.distance_sprint_24kmh_m)}, {_sql_num(g.distance_sprint_28kmh_m)}, "
            f"{_sql_num(g.nb_sprints_24kmh)}, {_sql_num(g.vitesse_max_kmh)}, "
            f"{_sql_num(g.nb_accelerations)}, {_sql_num(g.nb_freinages)}, {_sql_num(g.ratio_distance_min)});"
        )

    lignes.append("\nCOMMIT;")
    contenu = "\n".join(lignes)
    with open(chemin, "w", encoding="utf-8") as f:
        f.write(contenu)
    return chemin
