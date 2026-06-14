"""
Pushers API : envoient la saison simulée vers le backend, chacun avec le compte
(rôle) autorisé à écrire le type de donnée concerné.

Ordre important :
  exercices → séances → (exercices de séance) → GPS (passe la séance en RÉALISÉE)
  → pesées → wellness/RPE (côté joueur) → blessures/RTP → conseils → plan de jeu
  → matchs → formations/schémas.
"""

from __future__ import annotations

import numpy as np

from .bootstrap import BootstrapContext
from .simulation import SaisonSimulee


# ─────────────────────────── Exercices (catalogue club) ───────────────────────────

_EXERCICES = [
    ("Échauffement dynamique", "Physique", "PHYSIQUE", 15, "Activation", 2, 1200),
    ("Fractionné 30/30", "Physique", "PHYSIQUE", 20, "Capacité aérobie", 5, 3500),
    ("Circuit force", "Physique", "PHYSIQUE", 25, "Force", 4, 600),
    ("Conservation 4v4", "Technique", "TECHNIQUE", 20, "Conservation", 3, None),
    ("Jeu de position", "Tactique", "MIXTE", 25, "Animation offensive", 3, 1800),
    ("Travail devant le but", "Technique", "TECHNIQUE", 20, "Finition", 3, None),
    ("Transitions", "Tactique", "MIXTE", 20, "Transition off/def", 4, 2200),
    ("Retour au calme", "Physique", "PHYSIQUE", 10, "Récupération", 1, 800),
]


def pousser_exercices(ctx: BootstrapContext) -> dict[str, str]:
    """Crée le catalogue d'exercices ; renvoie nom → id (idempotent par nom)."""
    coach = ctx.worker("exercices")
    existants = {e["nom"]: e["id"] for e in coach.get("/api/exercices")}
    ids: dict[str, str] = {}
    for nom, cat, typ, duree, objectif, intensite, dist in _EXERCICES:
        if nom in existants:
            ids[nom] = existants[nom]
            continue
        payload = {
            "nom": nom, "categorie": cat, "type": typ, "dureeMinutes": duree,
            "objectif": objectif, "intensite": intensite, "description": objectif,
        }
        if dist is not None:
            payload["distanceAttendueM"] = dist
        ids[nom] = coach.post("/api/exercices", json=payload)["id"]
    return ids


# Exercices types attachés à une séance selon son type.
_CONTENU_TYPE = {
    "REPRISE": ["Échauffement dynamique", "Retour au calme"],
    "TECHNIQUE": ["Échauffement dynamique", "Conservation 4v4", "Travail devant le but"],
    "INTENSIF": ["Échauffement dynamique", "Fractionné 30/30", "Circuit force"],
    "PRE_MATCH": ["Échauffement dynamique", "Jeu de position"],
    "MATCH": [],
}


# ─────────────────────────── Séances ───────────────────────────

def pousser_seances(ctx: BootstrapContext, saison: SaisonSimulee, exercices_ids: dict[str, str]) -> int:
    prepa = ctx.worker("seances")
    # Idempotence : une seule séance par date dans notre calendrier → on réutilise
    # la séance existante (même date) plutôt que d'en recréer une (évite les doublons
    # et garde des ids stables, donc GPS/RPE/wellness s'upsertent proprement).
    existantes = {s["date"]: s for s in (prepa.get("/api/seances") or [])}
    n = 0
    for s in saison.seances:
        deja = existantes.get(s.date.isoformat())
        if deja:
            s.backend_id = deja["id"]
            statut = deja.get("statut")
        else:
            payload = {
                "typeSeance": {"id": ctx.type_seance_ids[s.type_code]},
                "date": s.date.isoformat(),
                "statut": "PLANIFIEE",
                "dureeMinutes": _duree_type(s.type_code),
                "responsable": "Staff démo",
            }
            if s.est_match:
                payload["adversaire"] = s.adversaire
                payload["domicileExterieur"] = "DOMICILE" if s.domicile else "EXTERIEUR"
                payload["competition"] = "Championnat"
            cree = prepa.post("/api/seances", json=payload)
            s.backend_id = cree["id"]
            statut = "PLANIFIEE"
            n += 1

        # Contenu (exercices) — uniquement tant que la séance n'est pas réalisée.
        noms = _CONTENU_TYPE.get(s.type_code, [])
        if noms and statut != "REALISEE":
            lignes = [{"exerciceId": exercices_ids[nm]} for nm in noms if nm in exercices_ids]
            if lignes:
                prepa.put(f"/api/seances/{s.backend_id}/exercices", json={"exercices": lignes})
    return n


def _duree_type(code: str) -> int:
    return {"MATCH": 95, "INTENSIF": 80, "TECHNIQUE": 70, "PRE_MATCH": 50, "REPRISE": 55}[code]


# ─────────────────────────── GPS ───────────────────────────

def pousser_gps(ctx: BootstrapContext, saison: SaisonSimulee) -> int:
    prepa = ctx.worker("gps")
    par_seance: dict[str, list] = {}
    for g in saison.gps:
        par_seance.setdefault(g.seance.backend_id, []).append(g)

    total = 0
    for seance_id, mesures in par_seance.items():
        lignes = [{
            "joueurId": m.joueur.backend_id,
            "dureeMinutes": m.duree_minutes,
            "distanceTotaleM": m.distance_totale_m,
            "distance15kmhM": m.distance_15kmh_m,
            "distance19kmhM": m.distance_19kmh_m,
            "distanceSprint24kmhM": m.distance_sprint_24kmh_m,
            "distanceSprint28kmhM": m.distance_sprint_28kmh_m,
            "nbSprints24kmh": m.nb_sprints_24kmh,
            "vitesseMaxKmh": m.vitesse_max_kmh,
            "nbAccelerations": m.nb_accelerations,
            "nbFreinages": m.nb_freinages,
            "ratioDistanceMin": m.ratio_distance_min,
        } for m in mesures]
        prepa.post("/api/import/excel/confirmer",
                   json={"seanceId": seance_id, "resolutions": [], "lignes": lignes})
        total += len(lignes)
    return total


# ─────────────────────────── Pesées ───────────────────────────

def pousser_pesees(ctx: BootstrapContext, saison: SaisonSimulee) -> int:
    prepa = ctx.worker("pesees")
    for p in saison.pesees:
        prepa.post("/api/pesees", json={
            "joueurId": p.joueur.backend_id,
            "date": p.date.isoformat(),
            "poids": p.poids_kg,
        })
    return len(saison.pesees)


# ─────────────────────────── Wellness + RPE (côté joueur) ───────────────────────────

def pousser_wellness(ctx: BootstrapContext, saison: SaisonSimulee) -> int:
    n = 0
    for w in saison.wellness:
        client = ctx.joueurs_clients.get(w.joueur.nom_complet)
        if client is None:
            continue
        payload = {
            "date": w.date.isoformat(),
            "sommeil": w.sommeil, "fatigue": w.fatigue, "douleur": w.douleur,
            "stress": w.stress, "humeur": w.humeur,
        }
        if w.gene_zone:
            payload["geneZone"] = w.gene_zone
            payload["geneIntensite"] = w.gene_intensite
            payload["geneMoment"] = "REPOS"   # vocabulaire DB : EFFORT | APRES | REPOS
        client.post("/api/moi/wellness", json=payload)
        n += 1
    return n


def pousser_rpe(ctx: BootstrapContext, saison: SaisonSimulee) -> int:
    rng = np.random.default_rng(saison.params.seed + 7)
    n = 0
    for r in saison.rpe:
        # Tous les RPE ne sont pas saisis (taux de retour).
        if rng.random() > saison.params.taux_saisie_rpe:
            continue
        client = ctx.joueurs_clients.get(r.joueur.nom_complet)
        if client is None:
            continue
        client.post("/api/moi/rpe", json={
            "seanceId": r.seance.backend_id,
            "seanceType": r.type_rpe,
            "rpe": r.rpe,
            "dureeMinutes": r.duree_minutes,
        })
        n += 1
    return n


# ─────────────────────────── Blessures + RTP ───────────────────────────

def pousser_blessures(ctx: BootstrapContext, saison: SaisonSimulee) -> int:
    medic = ctx.worker("blessures")
    for b in saison.blessures:
        payload = {
            "joueurId": b.joueur.backend_id,
            "dateBlessure": b.debut.isoformat(),
            "dateRetourPrevue": b.fin.isoformat(),
            "dateRetourEffectif": b.fin.isoformat(),
            "statut": "RETABLI",        # vocabulaire DB : INDISPONIBLE | EN_REPRISE | RETABLI
            "typeBlessure": b.type_blessure,
            "zoneCorporelle": b.zone_corporelle,
            "gravite": b.gravite,
            "causeProbable": "contact" if b.survenue_en_match else "surcharge",
            "recidive": False,
            "commentaire": f"{b.libelle_humain} (épisode simulé).",
        }
        cree = medic.post("/api/blessures", json=payload)
        # Initialise le protocole de retour au jeu (étapes RTP).
        try:
            medic.post(f"/api/blessures/{cree['id']}/rtp")
        except Exception:
            pass
    return len(saison.blessures)


# ─────────────────────────── Conseils staff ───────────────────────────

def pousser_conseils(ctx: BootstrapContext, saison: SaisonSimulee) -> int:
    medic = ctx.worker("conseils")
    for c in saison.conseils:
        payload = {"titre": c.titre, "texte": c.message}
        if c.cible_joueur is not None:
            payload["joueurId"] = c.cible_joueur.backend_id
        medic.post("/api/conseils", json=payload)
    return len(saison.conseils)


# ─────────────────────────── Plan de jeu (document d'identité) ───────────────────────────

_PLAN_TEXTES = [
    "Bloc médian, pressing à la perte sur les 6 premières secondes.",
    "Construction depuis la défense à 3, latéraux haut.",
    "Animation offensive : largeur par les ailiers, appels en profondeur de l'avant-centre.",
    "Phase défensive : bloc compact, orientation du jeu vers l'extérieur.",
    "Coups de pied arrêtés offensifs : 2 joueurs au premier poteau.",
    "Transitions : verticalité immédiate après récupération.",
]


def pousser_plan_de_jeu(ctx: BootstrapContext) -> int:
    coach = ctx.worker("plan_de_jeu")
    plan = coach.get("/api/plan-de-jeu")  # crée les sections par défaut au 1er appel
    sections = plan.get("sections", [])
    n = 0
    for sec, texte in zip(sections, _PLAN_TEXTES):
        coach.put(f"/api/plan-de-jeu/sections/{sec['id']}",
                  json={"titre": sec["titre"], "texte": texte})
        n += 1
    return n


# ─────────────────────────── Matchs (module tactique avant/après) ───────────────────────────

def pousser_matchs(ctx: BootstrapContext, saison: SaisonSimulee) -> int:
    coach = ctx.worker("matchs")
    rng = np.random.default_rng(saison.params.seed + 11)
    n = 0
    for s in saison.seances:
        if not s.est_match:
            continue
        cree = coach.post("/api/matchs", json={
            "adversaire": s.adversaire,
            "dateMatch": s.date.isoformat(),
            "competition": "Championnat",
            "domicile": bool(s.domicile),
        })
        bf, ba = int(rng.integers(0, 4)), int(rng.integers(0, 4))
        resultat = "V" if bf > ba else ("N" if bf == ba else "D")
        coach.put(f"/api/matchs/{cree['id']}/debrief", json={
            "resultat": resultat, "score": f"{bf}-{ba}",
            "notesDebrief": "Débrief simulé : analyse des phases clés et axes de travail.",
        })
        n += 1
    return n


# ─────────────────────────── Formations & schémas tactiques ───────────────────────────

_FORMATION_433 = (
    '{"nom":"4-3-3","positions":[{"x":50,"y":92},{"x":18,"y":72},{"x":38,"y":76},'
    '{"x":62,"y":76},{"x":82,"y":72},{"x":32,"y":52},{"x":50,"y":56},{"x":68,"y":52},'
    '{"x":20,"y":28},{"x":50,"y":22},{"x":80,"y":28}]}'
)
_FORMATION_442 = (
    '{"nom":"4-4-2","positions":[{"x":50,"y":92},{"x":18,"y":72},{"x":38,"y":76},'
    '{"x":62,"y":76},{"x":82,"y":72},{"x":18,"y":50},{"x":40,"y":52},{"x":60,"y":52},'
    '{"x":82,"y":50},{"x":40,"y":24},{"x":60,"y":24}]}'
)


def pousser_formations_et_schemas(ctx: BootstrapContext) -> int:
    coach = ctx.worker("formations")
    n = 0
    existantes = {f["nom"] for f in coach.get("/api/formations")}
    for nom, couleur, pos in [("4-3-3", "#16a34a", _FORMATION_433), ("4-4-2", "#2563eb", _FORMATION_442)]:
        if nom not in existantes:
            coach.post("/api/formations", json={"nom": nom, "couleur": couleur, "positionsJson": pos})
            n += 1

    schema_coach = ctx.worker("schemas")
    existants = {s["nom"] for s in schema_coach.get("/api/schemas")}
    for nom, cat, js in [
        ("Pressing haut", "Phase défensive", _FORMATION_433),
        ("Sortie de balle", "Construction", _FORMATION_442),
    ]:
        if nom not in existants:
            schema_coach.post("/api/schemas", json={"nom": nom, "categorie": cat, "schemaJson": js})
            n += 1
    return n


# ─────────────────────────── Orchestration ───────────────────────────

def pousser_tout(ctx: BootstrapContext, saison: SaisonSimulee, inclure_tactique: bool = True,
                 log=print) -> None:
    # Réinjection propre : on retire d'abord les éléments sans upsert (blessures,
    # conseils, matchs). Séances réutilisées par date → pas de doublons GPS/RPE.
    from .purge import nettoyer_episodiques
    log("→ Nettoyage des éléments non-idempotents…")
    nettoyer_episodiques(ctx, log)
    log("→ Exercices…")
    ex = pousser_exercices(ctx)
    log(f"  {len(ex)} exercices")
    log("→ Séances…")
    log(f"  {pousser_seances(ctx, saison, ex)} séances")
    log("→ GPS…")
    log(f"  {pousser_gps(ctx, saison)} lignes GPS")
    log("→ Pesées…")
    log(f"  {pousser_pesees(ctx, saison)} pesées")
    log("→ Wellness (par joueur)…")
    log(f"  {pousser_wellness(ctx, saison)} saisies wellness")
    log("→ RPE (par joueur)…")
    log(f"  {pousser_rpe(ctx, saison)} saisies RPE")
    log("→ Blessures + RTP…")
    log(f"  {pousser_blessures(ctx, saison)} blessures")
    log("→ Conseils…")
    log(f"  {pousser_conseils(ctx, saison)} conseils")
    if inclure_tactique:
        log("→ Plan de jeu…")
        log(f"  {pousser_plan_de_jeu(ctx)} sections")
        log("→ Matchs (prépa/débrief)…")
        log(f"  {pousser_matchs(ctx, saison)} matchs")
        log("→ Formations & schémas…")
        log(f"  {pousser_formations_et_schemas(ctx)} éléments tactiques")
