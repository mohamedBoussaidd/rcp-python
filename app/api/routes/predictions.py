"""
Endpoints de prédiction — couche HTTP UNIQUEMENT.

Ce module ne contient plus aucun calcul : il lit les en-têtes de contexte, appelle
`app.domain` et met en forme la réponse. Toute logique ajoutée ici doit partir dans un
module de domaine — c'est en laissant le métier s'installer dans les handlers que ce
fichier avait atteint 2573 lignes, avec des erreurs de calcul invisibles à l'intérieur.
"""
from fastapi import APIRouter, HTTPException, Header
from uuid import UUID
from datetime import date as _date
from typing import List

from app.core.database import get_connection
from app.core.config import _load_config
from app.core.scope import _equipes_scope
from app.domain.temps import _parse_date_simulee
from app.domain.contexte import _contexte_joueur
from app.domain.risque import _risque_probabiliste
from app.domain.fatigue import _calcul_fatigue
from app.domain.objectif import _charge_cible, _simulation_seance_data, _objectif_hebdo_data
from app.domain.rapport_seance import rapport_seance
from app.domain.derives import derives
from app.domain.equipe import charge_equipe, briefing, resume_equipe
from app.schemas.schemas import (RisqueBlessure, NiveauFatigue, ResumeJoueur, ChargeCible,
                                 SimulationSeanceRequete)

router = APIRouter()


@router.get("/risque/{joueur_id}", response_model=RisqueBlessure)
def get_risque_blessure(joueur_id: UUID, x_date_simulee: str | None = Header(default=None)):
    date_ref = _parse_date_simulee(x_date_simulee)
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, nom, prenom FROM joueur WHERE id = %s",
                    (str(joueur_id),)
                )
                joueur = cur.fetchone()

            if not joueur:
                raise HTTPException(status_code=404, detail="Joueur introuvable")

            cfg = _load_config(conn)
            r   = _risque_probabiliste(joueur_id, cfg, conn, date_ref=date_ref)

        return RisqueBlessure(
            joueur_id=joueur_id,
            nom=joueur[1],
            prenom=joueur[2],
            score_risque=r["score"],
            niveau=r["niveau"],
            probabilite=r["probabilite"],
            phrase=r["phrase"],
            facteur_dominant=r["facteur_dominant"],
            tendance=r["tendance"],
            source=r["source"],
            etat=r.get("etat"),
            periode_type=r.get("periode_type"),
            periode_libelle=r.get("periode_libelle"),
            jours_inactif=r.get("jours_inactif"),
            contributions=r.get("contributions") or [],
            acwr=r.get("acwr"),
            acwr_gps=r.get("acwr_gps"),
            acwr_rpe=r.get("acwr_rpe"),
            semaines_gps=r.get("semaines_gps"),
            semaines_rpe=r.get("semaines_rpe"),
            ecart_sources=r.get("ecart_sources"),
            provisoire=r.get("provisoire"),
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/charge-cible/{joueur_id}", response_model=ChargeCible)
def get_charge_cible(joueur_id: UUID, x_date_simulee: str | None = Header(default=None)):
    date_ref = _parse_date_simulee(x_date_simulee)
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM joueur WHERE id = %s", (str(joueur_id),))
                if not cur.fetchone():
                    raise HTTPException(status_code=404, detail="Joueur introuvable")
            cfg = _load_config(conn)
            c   = _charge_cible(joueur_id, cfg, conn, date_ref)

        return ChargeCible(joueur_id=joueur_id, **c)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/fatigue/{joueur_id}", response_model=NiveauFatigue)
def get_fatigue(joueur_id: UUID, x_date_simulee: str | None = Header(default=None)):
    date_ref = _parse_date_simulee(x_date_simulee)
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, nom, prenom FROM joueur WHERE id = %s",
                    (str(joueur_id),)
                )
                joueur = cur.fetchone()

            if not joueur:
                raise HTTPException(status_code=404, detail="Joueur introuvable")

            cfg = _load_config(conn)
            ctx = _contexte_joueur(joueur_id, cfg, conn, date_ref)
            if ctx["silence"]:
                ji = ctx["jours_inactif"]
                depuis = f" depuis {ji} j" if ji is not None else ""
                libelle_periode = ctx["periode_libelle"] or "trêve / intersaison"
                raison = {
                    "HORS_CHARGE": f"Hors charge ({libelle_periode}) — pas de suivi de fatigue.",
                    "HORS_SAISON": "Aucune saison en cours — pas de suivi de fatigue.",
                    "INACTIF":     f"Aucune donnée récente{depuis} — fatigue non évaluée.",
                    "BLESSE":      "Joueur en cours de blessure — fatigue d'entraînement non évaluée.",
                }.get(ctx["etat"], "Fatigue non évaluée.")
                fatigue = {"score": 0.0, "niveau": "NOMINAL", "raison": raison,
                           "signaux": [], "indicatifs": [], "donnees": False}
            else:
                fatigue = _calcul_fatigue(joueur_id, cfg, conn, date_ref)

        return NiveauFatigue(
            joueur_id=joueur_id,
            nom=joueur[1],
            prenom=joueur[2],
            score_fatigue=fatigue["score"],
            niveau=fatigue["niveau"],
            raison=fatigue["raison"],
            signaux=fatigue.get("signaux") or [],
            indicatifs=fatigue.get("indicatifs") or [],
            donnees=fatigue.get("donnees"),
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/charge-collective")
def get_charge_collective(semaines: int = 4,
                          x_date_simulee: str | None = Header(default=None),
                          x_contexte_equipes: str | None = Header(default=None),
                          x_contexte_club: str | None = Header(default=None)):
    """
    Charge collective (km) par semaine glissante sur les `semaines` dernières
    semaines (4, 8 ou 12). Index 0 = la plus ancienne, dernier = semaine en cours.
    La « semaine en cours » est ancrée sur la date simulée (X-Date-Simulee, super-admin)
    quand elle est fournie, sinon sur la date réelle ; les séances postérieures sont exclues.
    """
    semaines = semaines if semaines in (4, 8, 12) else 4
    jours = semaines * 7
    ref = _parse_date_simulee(x_date_simulee) or _date.today()
    try:
        with get_connection() as conn:
            scope = _equipes_scope(x_contexte_equipes, x_contexte_club, conn)
            extra = ""
            # Ordre des paramètres = ordre des %s dans la requête (ref utilisée 3×).
            qp: list = [semaines, ref, ref, jours, ref]
            if scope:
                extra = " AND s.equipe_id = ANY(%s)"; qp.append(scope)
            with conn.cursor() as cur:
                # bucket : 0 = semaine la plus ancienne … (semaines-1) = semaine en cours (= ref)
                cur.execute(f"""
                    SELECT
                        %s - 1 - FLOOR((%s::date - s.date) / 7)::int AS semaine_idx,
                        ROUND(SUM(dg.distance_totale_m) / 1000.0, 1) AS total_km
                    FROM donnee_gps dg
                    JOIN seance s ON dg.seance_id = s.id
                    JOIN joueur j ON j.id = dg.joueur_id
                    WHERE s.date >= %s::date - (%s || ' days')::interval
                      AND s.date <= %s::date
                      AND j.statut != 'inactif'{extra}
                    GROUP BY 1
                    ORDER BY 1
                """, tuple(qp))
                rows = cur.fetchall()

        data = [0.0] * semaines
        for row in rows:
            idx = int(row[0])
            if 0 <= idx < semaines:
                data[idx] = float(row[1])

        labels = [f"S-{semaines - i}" for i in range(semaines)]
        return {"labels": labels, "data": data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/seance/{seance_id}/rapport")
def get_rapport_seance(seance_id: UUID, x_date_simulee: str | None = Header(default=None)):
    return rapport_seance(seance_id, x_date_simulee)


@router.get("/equipe/derives")
def get_derives(x_contexte_equipes: str | None = Header(default=None),
                x_contexte_club: str | None = Header(default=None),
                x_date_simulee: str | None = Header(default=None)):
    return derives(x_contexte_equipes, x_contexte_club, x_date_simulee)


@router.get("/equipe/charge")
def get_charge_equipe(debut: str | None = None, fin: str | None = None, types: str | None = None,
                      x_contexte_equipes: str | None = Header(default=None),
                      x_contexte_club: str | None = Header(default=None),
                      x_date_simulee: str | None = Header(default=None)):
    return charge_equipe(debut, fin, types, x_contexte_equipes, x_contexte_club, x_date_simulee)


@router.get("/equipe/briefing")
def get_briefing(x_contexte_equipes: str | None = Header(default=None),
                 x_contexte_club: str | None = Header(default=None),
                 x_date_simulee: str | None = Header(default=None)):
    return briefing(x_contexte_equipes, x_contexte_club, x_date_simulee)


@router.get("/equipe", response_model=List[ResumeJoueur])
def get_resume_equipe(x_date_simulee: str | None = Header(default=None),
                      x_contexte_equipes: str | None = Header(default=None),
                      x_contexte_club: str | None = Header(default=None)):
    return resume_equipe(x_date_simulee, x_contexte_equipes, x_contexte_club)


@router.get("/equipe/objectif-hebdo")
def get_objectif_hebdo(x_contexte_equipes: str | None = Header(default=None),
                       x_contexte_club: str | None = Header(default=None),
                       x_date_simulee: str | None = Header(default=None)):
    """
    Panneau « Objectif de la semaine » (semaine ISO en cours, lundi → aujourd'hui).
    Par joueur de l'effectif : cumul de distance de la semaine, cible A.5 (« suggestion
    intelligente »), objectif retenu (manuel d'équipe si défini, sinon la cible A.5) et atteinte.
    L'objectif manuel n'est lu que si le contexte cible UNE seule équipe.
    """
    try:
        with get_connection() as conn:
            cfg   = _load_config(conn)
            scope = _equipes_scope(x_contexte_equipes, x_contexte_club, conn)
            return _objectif_hebdo_data(conn, cfg, scope, _parse_date_simulee(x_date_simulee))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/equipe/simulation")
def post_simulation_seance(requete: SimulationSeanceRequete,
                           x_contexte_equipes: str | None = Header(default=None),
                           x_contexte_club: str | None = Header(default=None),
                           x_date_simulee: str | None = Header(default=None)):
    """
    Simulation « et si… » — scénario « une séance ». À partir d'une séance HYPOTHÉTIQUE (type +
    durée), projette pour chaque joueur la distance attendue (baseline m/min sur ce type de séance)
    et recalcule son ACWR en ajoutant cette distance à la charge aiguë → qui basculerait au-dessus
    du plafond si la séance avait lieu.

    POST parce qu'on envoie un corps, mais l'opération est en LECTURE SEULE : aucune séance n'est
    créée, aucune donnée n'est écrite. Le score de risque officiel n'est pas affecté.
    """
    try:
        with get_connection() as conn:
            cfg   = _load_config(conn)
            scope = _equipes_scope(x_contexte_equipes, x_contexte_club, conn)
            return _simulation_seance_data(conn, cfg, scope,
                                           requete.type_seance_id, requete.duree_minutes,
                                           _parse_date_simulee(x_date_simulee))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


