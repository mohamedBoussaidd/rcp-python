"""
Purge du tenant démo (confinée à l'équipe démo) — teardown complet du CONTENU.

Ordre pensé pour respecter les contraintes FK :
  matchs → blessures → séances (→ GPS en cascade) → fiches joueurs
  (→ wellness, RPE, pesées, conseils perso, présences EN CASCADE) → conseils
  d'équipe restants → formations/schémas → comptes JOUEUR.

La suppression des FICHES joueurs efface wellness/RPE/pesées via les FK
ON DELETE CASCADE (il n'existe pas d'endpoint DELETE direct pour wellness/RPE).
Comme utilisateur.joueur_id est en SET NULL, on supprime aussi les comptes JOUEUR
pour qu'une réinjection les recrée correctement liés aux nouvelles fiches.

Conservés : l'équipe démo, les comptes workers et le président (socle léger).

Note : pour un simple rafraîchissement, ne PAS purger — relancer la génération
suffit (séances réutilisées par date, données upsertées, aucun doublon).
"""

from __future__ import annotations

from . import config
from .bootstrap import BootstrapContext


def purger(ctx: BootstrapContext, log=print) -> None:
    coach = ctx.workers["entraineur"]
    prepa = ctx.workers["preparateur"]
    medic = ctx.workers["medical"]

    # 1) Matchs (module tactique)
    matchs = coach.get("/api/matchs") or []
    for m in matchs:
        coach.delete(f"/api/matchs/{m['id']}")
    log(f"  {len(matchs)} matchs supprimés")

    # 2) Blessures (FK joueur sans cascade → avant la suppression des fiches)
    blessures = medic.get("/api/blessures") or []
    for b in blessures:
        medic.delete(f"/api/blessures/{b['id']}")
    log(f"  {len(blessures)} blessures supprimées")

    # 3) Séances (GPS supprimé en cascade côté service ; FK gps→joueur sans cascade)
    seances = prepa.get("/api/seances") or []
    for s in seances:
        prepa.delete(f"/api/seances/{s['id']}")
    log(f"  {len(seances)} séances supprimées (+ GPS en cascade)")

    # 4) Fiches joueurs → cascade wellness / RPE / pesées / conseils perso / présences
    joueurs = prepa.get("/api/joueurs/tous") or []
    for j in joueurs:
        prepa.delete(f"/api/joueurs/{j['id']}")
    log(f"  {len(joueurs)} fiches joueurs supprimées (+ wellness/RPE/pesées en cascade)")

    # 5) Conseils d'équipe restants (joueur_id null → non cascadés)
    conseils = medic.get("/api/conseils") or []
    for c in conseils:
        medic.delete(f"/api/conseils/{c['id']}")
    log(f"  {len(conseils)} conseils d'équipe supprimés")

    # 6) Formations & schémas (niveau club)
    nb = 0
    for f in coach.get("/api/formations") or []:
        coach.delete(f"/api/formations/{f['id']}")
        nb += 1
    for s in coach.get("/api/schemas") or []:
        coach.delete(f"/api/schemas/{s['id']}")
        nb += 1
    log(f"  {nb} éléments tactiques supprimés")

    # 7) Comptes JOUEUR (pour re-création liée à la réinjection)
    membres = ctx.president.get("/api/mon-club/membres") or []
    nb_comptes = 0
    for m in membres:
        if (m.get("email") or "").lower().endswith("@" + config.JOUEUR_EMAIL_DOMAIN):
            ctx.president.delete(f"/api/membres/{m['id']}")
            nb_comptes += 1
    log(f"  {nb_comptes} comptes JOUEUR supprimés")


def nettoyer_episodiques(ctx: BootstrapContext, log=print) -> None:
    """Supprime ce qui n'a pas d'upsert naturel (blessures, conseils, matchs) afin
    qu'une réinjection ne crée pas de doublons. Les séances (et donc GPS/RPE) sont
    conservées et réutilisées (idempotence par date)."""
    coach = ctx.workers["entraineur"]
    prepa = ctx.workers["preparateur"]
    medic = ctx.workers["medical"]

    for b in medic.get("/api/blessures") or []:
        medic.delete(f"/api/blessures/{b['id']}")
    for m in coach.get("/api/matchs") or []:
        coach.delete(f"/api/matchs/{m['id']}")
    ids = {c["id"] for c in (medic.get("/api/conseils") or [])}
    for j in prepa.get("/api/joueurs/tous") or []:
        for c in medic.get("/api/conseils", params={"joueurId": j["id"]}) or []:
            ids.add(c["id"])
    for cid in ids:
        medic.delete(f"/api/conseils/{cid}")
