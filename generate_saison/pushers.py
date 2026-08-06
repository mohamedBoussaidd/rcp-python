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

from .api_client import ApiError
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
        # `/api/import/confirmer` : le contrôleur a perdu son segment « excel » à la restructuration
        # feature-first (le format n'a jamais été lié à Excel — le corps est du JSON pur).
        prepa.post("/api/import/confirmer",
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
    # Depuis V104, poser une séance de type MATCH crée DÉJÀ son dossier de match. En reposter un
    # ici créerait une seconde séance (le back en crée une pour tout match qui n'en a pas) : on
    # récupère donc le dossier existant à la même date et on se contente de le compléter.
    existants = {}
    try:
        for m in coach.get("/api/matchs") or []:
            if m.get("dateMatch"):
                existants.setdefault(m["dateMatch"], m)
    except ApiError:
        pass   # module Match absent de ce pack : on retombe sur la création, qui échouera proprement
    n = 0
    for s in saison.seances:
        if not s.est_match:
            continue
        cree = existants.get(s.date.isoformat()) or coach.post("/api/matchs", json={
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
        # Compo puis feuille de match : sans elles l'onglet Compétition reste vide, et le temps
        # de jeu n'a qu'une seule source (le GPS) au lieu des trois qu'il sait afficher.
        _pousser_compo_et_feuille(coach, ctx, cree["id"], bf, rng)
        n += 1
    return n


# 4-3-3 : un titulaire par ligne du terrain, puis les remplaçants. Le rabattement suit les postes
# réels de l'effectif simulé — une compo tirée au hasard produirait 4 gardiens et aucun défenseur.
_LIGNES_433 = [
    (["GK"], 1, [(50, 92)]),
    (["LB", "DC", "DC", "RB"], 4, [(18, 72), (38, 76), (62, 76), (82, 72)]),
    (["MDC", "MC", "MC"], 3, [(32, 52), (50, 56), (68, 52)]),
    (["AG", "ATT", "AD"], 3, [(20, 28), (50, 22), (80, 28)]),
]


def _pousser_compo_et_feuille(coach, ctx: BootstrapContext, match_id: str,
                              buts_pour: int, rng) -> None:
    """Compo (11 titulaires + remplaçants) et feuille de match cohérente avec le score."""
    dispos = [j for j in ctx.effectif if j.backend_id]
    if len(dispos) < 11:
        return

    restants = list(dispos)
    titulaires: list = []
    placements: list = []
    for postes, nb, coords in _LIGNES_433:
        for i in range(nb):
            poste = postes[i] if i < len(postes) else postes[-1]
            choix = next((j for j in restants if j.poste == poste), None)
            if choix is None:                      # effectif incomplet à ce poste : on prend au champ
                choix = next((j for j in restants if j.poste != "GK"), None)
            if choix is None:
                continue
            restants.remove(choix)
            titulaires.append(choix)
            x, y = coords[i]
            placements.append({"joueurId": choix.backend_id, "x": x, "y": y,
                               "statut": "TITULAIRE", "consigne": None})

    remplacants = restants[:7]
    for r in remplacants:
        placements.append({"joueurId": r.backend_id, "x": 0, "y": 0,
                           "statut": "REMPLACANT", "consigne": None})

    try:
        coach.put(f"/api/matchs/{match_id}/compo", json={"placements": placements})
    except Exception:
        return   # module tactique absent sur ce tier : on n'interrompt jamais le run

    # ── Feuille : qui a joué, combien de temps, et ce qu'il y a fait ──
    entrants = list(rng.choice(len(remplacants), size=min(3, len(remplacants)), replace=False)) \
        if remplacants else []
    sortants = list(rng.choice(11, size=len(entrants), replace=False)) if entrants else []

    lignes = []
    for i, j in enumerate(titulaires):
        sort = i in sortants
        minute_sortie = int(rng.integers(55, 85)) if sort else 90
        lignes.append({
            "joueurId": j.backend_id, "entreEnJeu": True,
            "minuteEntree": 0, "minuteSortie": minute_sortie,
            "buts": 0, "passesDecisives": 0, "cartonsJaunes": 0, "cartonRouge": False,
        })
    for rang, idx in enumerate(entrants):
        j = remplacants[int(idx)]
        # Il entre quand un titulaire sort : les minutes des deux côtés restent cohérentes.
        minute = lignes[int(sortants[rang])]["minuteSortie"]
        lignes.append({
            "joueurId": j.backend_id, "entreEnJeu": True,
            "minuteEntree": minute, "minuteSortie": 90,
            "buts": 0, "passesDecisives": 0, "cartonsJaunes": 0, "cartonRouge": False,
        })
    for rang, r in enumerate(remplacants):
        if rang in [int(i) for i in entrants]:
            continue
        lignes.append({
            "joueurId": r.backend_id, "entreEnJeu": False,
            "minuteEntree": None, "minuteSortie": None,
            "buts": 0, "passesDecisives": 0, "cartonsJaunes": 0, "cartonRouge": False,
        })

    # Les buts du score se répartissent sur ceux qui étaient sur le terrain, offensifs d'abord —
    # sinon la feuille annoncerait 3-1 avec zéro buteur, et l'onglet Compétition serait incohérent.
    joueurs_en_jeu = [k for k, l in enumerate(lignes) if l["entreEnJeu"]]
    offensifs = [k for k in joueurs_en_jeu
                 if _poste_de(ctx, lignes[k]["joueurId"]) in ("ATT", "AG", "AD", "MC")]
    cible = offensifs or joueurs_en_jeu
    for _ in range(buts_pour):
        k = int(rng.choice(cible))
        lignes[k]["buts"] += 1
        passeurs = [p for p in joueurs_en_jeu if p != k]
        if passeurs and rng.random() < 0.7:
            lignes[int(rng.choice(passeurs))]["passesDecisives"] += 1

    # ~2,5 jaunes par match, rouge rare — et jamais un rouge sans les deux jaunes qui l'imposent.
    for _ in range(int(rng.integers(1, 4))):
        k = int(rng.choice(joueurs_en_jeu))
        lignes[k]["cartonsJaunes"] = min(2, lignes[k]["cartonsJaunes"] + 1)
        if lignes[k]["cartonsJaunes"] == 2:
            lignes[k]["cartonRouge"] = True

    try:
        coach.put(f"/api/matchs/{match_id}/feuille", json={"lignes": lignes})
    except Exception:
        pass   # module stats_competition absent : la compo reste, c'est déjà l'essentiel


def _poste_de(ctx: BootstrapContext, backend_id: str) -> str:
    for j in ctx.effectif:
        if j.backend_id == backend_id:
            return j.poste
    return ""


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


# ─────────────────────────── Présence (appel) ───────────────────────────

def pousser_presence(ctx: BootstrapContext, saison: SaisonSimulee) -> int:
    """Feuille d'appel des ENTRAÎNEMENTS : PRÉSENT par défaut, seules les déviations sont stockées."""
    prepa = ctx.worker("presence")
    rng = np.random.default_rng(saison.params.seed + 13)
    total = 0
    for s in saison.seances:
        if s.est_match or not s.backend_id:
            continue
        lignes = []
        for j in saison.effectif:
            if not j.backend_id:
                continue
            r = rng.random()
            if r < 0.06:
                statut, note = "ABSENT", ["", "Raison personnelle", "Souffrant"][int(rng.integers(0, 3))]
            elif r < 0.09:
                statut, note = "EXCUSE", "Excusé"
            elif r < 0.12:
                statut, note = "RETARD", "Arrivé en retard"
            else:
                continue  # PRÉSENT → non stocké
            lignes.append({"joueurId": j.backend_id, "statut": statut, "note": note or None})
        if lignes:
            prepa.put(f"/api/seances/{s.backend_id}/presence", json={"lignes": lignes})
            total += len(lignes)
    return total


# ─────────────────────────── Diaporama (TV / vidéoprojecteur) ───────────────────────────

def pousser_diaporama(ctx: BootstrapContext) -> int:
    """Un diaporama de briefing publié (idempotent par titre). Best-effort (n'interrompt jamais le run)."""
    coach = ctx.workers.get("entraineur")
    if coach is None:
        return 0
    titre = "Briefing tactique (démo)"
    try:
        if any(d.get("titre") == titre for d in (coach.get("/api/diaporamas") or [])):
            return 0
        did = coach.post("/api/diaporamas", json={"titre": titre})["id"]
        coach.post(f"/api/diaporamas/{did}/slides",
                   json={"type": "TEXTE", "titre": "Objectifs du match",
                         "texte": "Bloc médian, pressing coordonné à la perte, largeur offensive."})
        coach.post(f"/api/diaporamas/{did}/slides",
                   json={"type": "SCHEMA", "titre": "Organisation 4-3-3", "schemaJson": _FORMATION_433})
        coach.put(f"/api/diaporamas/{did}",
                  json={"titre": titre, "visibilite": "EQUIPE", "statut": "PUBLIE"})
        return 1
    except Exception:
        return 0


# ─────────────────────────── Notifications (chat staff → joueurs) ───────────────────────────

def pousser_notifications(ctx: BootstrapContext) -> int:
    """Quelques messages d'équipe (→ notifications joueurs). Idempotent : rien si déjà envoyés."""
    coach = ctx.workers.get("entraineur")
    if coach is None:
        return 0
    try:
        if coach.get("/api/notifications/messages/envoyes"):
            return 0
        messages = [
            ("Convocation match", "Rendez-vous samedi 13h au stade, tenue complète."),
            ("Récupération", "Séance de récupération demain matin — présence importante."),
            ("Rappel wellness", "Pensez à remplir votre ressenti quotidien et à signaler toute gêne."),
        ]
        for titre, corps in messages:
            coach.post("/api/notifications/messages", json={"titre": titre, "corps": corps})
        return len(messages)
    except Exception:
        return 0


# ─────────────────────────── Orchestration ───────────────────────────

def pousser_tier(ctx: BootstrapContext, saison: SaisonSimulee, log=print) -> None:
    """Pousse les données d'UNE équipe en filtrant selon le PACK du club (cf. ProfilClub).
    Un niveau bas ne génère ni GPS, ni tactique, ni médical → aucune donnée fantôme masquée."""
    from .purge import nettoyer_episodiques
    p = ctx.profil
    log(f"→ [{p.nom} / {ctx.equipe_nom}] injection…")
    nettoyer_episodiques(ctx, saison, log)

    ex = pousser_exercices(ctx) if p.tactique else {}
    log(f"  séances : {pousser_seances(ctx, saison, ex)} créées"
        + (f", {len(ex)} exercices" if p.tactique else " (sans contenu tactique)"))
    log(f"  présence : {pousser_presence(ctx, saison)} déviations")
    if p.gps:
        log(f"  GPS : {pousser_gps(ctx, saison)} lignes")
    log(f"  pesées : {pousser_pesees(ctx, saison)}")
    log(f"  wellness : {pousser_wellness(ctx, saison)} · RPE : {pousser_rpe(ctx, saison)}")
    log(f"  matchs : {pousser_matchs(ctx, saison)}")
    if p.medical:
        log(f"  blessures : {pousser_blessures(ctx, saison)} · conseils : {pousser_conseils(ctx, saison)}")
    if p.tactique:
        log(f"  plan de jeu : {pousser_plan_de_jeu(ctx)} sections · "
            f"tactique : {pousser_formations_et_schemas(ctx)} · diaporama : {pousser_diaporama(ctx)}")
    if p.notifications:
        log(f"  notifications : {pousser_notifications(ctx)} messages")


def pousser_tout(ctx: BootstrapContext, saison: SaisonSimulee, inclure_tactique: bool = True,
                 log=print) -> None:
    # Réinjection propre : on retire d'abord les éléments sans upsert (blessures,
    # conseils, matchs). Séances réutilisées par date → pas de doublons GPS/RPE.
    from .purge import nettoyer_episodiques
    log("→ Nettoyage des éléments non-idempotents…")
    nettoyer_episodiques(ctx, saison, log)
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
