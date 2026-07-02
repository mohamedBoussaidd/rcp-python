"""
Purge MULTI-CLUB du jeu de démo (chantier A), confinée aux clubs de config.PROFILS.

Pour chaque club de démo, on se connecte comme son PRÉSIDENT et on supprime le CONTENU :
  matchs + blessures (par équipe) → séances (→ GPS en cascade) → fiches joueurs
  (→ wellness/RPE/pesées/présences/conseils perso en cascade) → conseils d'équipe →
  formations/schémas → comptes JOUEUR & workers de démo.

Conservés : les clubs, leurs présidents (login démo) et les équipes. Pour repartir de
zéro complet, supprimez les clubs à la main côté super-admin.

Note : pour un simple rafraîchissement, ne PAS purger — relancer la génération suffit
(séances réutilisées par date, données upsertées, épisodiques nettoyés avant re-push).
"""

from __future__ import annotations

from . import config
from .api_client import ApiClient
from .bootstrap import BootstrapContext


# ═══════════════════════════ Purge multi-club ═══════════════════════════

def purger_tout(admin: ApiClient, base_url: str, log=print) -> None:
    for profil in config.PROFILS:
        club_id = _trouver_club(admin, profil.nom)
        if not club_id:
            log(f"  club « {profil.nom} » absent — rien à purger")
            continue
        log(f"\n=== Purge « {profil.nom} » ===")
        pres = ApiClient(base_url)
        pres.login(profil.president_email, config.PRESIDENT_DEMO_PASSWORD)
        pres.set_contexte(club_id=club_id)
        equipes = pres.get("/api/mon-club").get("equipes", [])

        # 1) Matchs + blessures : endpoints « 1 équipe active » → on itère par équipe.
        nb_m = nb_b = 0
        for e in equipes:
            pres.set_contexte(club_id=club_id, equipe_ids=[e["id"]])
            for m in pres.get("/api/matchs") or []:
                pres.delete(f"/api/matchs/{m['id']}"); nb_m += 1
            for b in pres.get("/api/blessures") or []:
                pres.delete(f"/api/blessures/{b['id']}"); nb_b += 1
        log(f"  {nb_m} matchs, {nb_b} blessures supprimés")

        # 2) Le reste au niveau club (contexte = toutes les équipes).
        pres.set_contexte(club_id=club_id, equipe_ids=[e["id"] for e in equipes])
        nb_s = _supprimer_liste(pres, "/api/seances")
        log(f"  {nb_s} séances supprimées (+ GPS en cascade)")
        nb_j = _supprimer_liste(pres, "/api/joueurs/tous", "/api/joueurs")
        log(f"  {nb_j} fiches joueurs supprimées (+ wellness/RPE/pesées/présences en cascade)")
        nb_c = _supprimer_liste(pres, "/api/conseils")
        nb_c += _supprimer_liste(pres, "/api/formations")
        nb_c += _supprimer_liste(pres, "/api/schemas")
        log(f"  {nb_c} conseils + éléments tactiques supprimés")

        # 3) Comptes de démo (workers + joueurs) du club, repérés par domaine email.
        nb_comptes = 0
        for m in pres.get("/api/mon-club/membres") or []:
            email = (m.get("email") or "").lower()
            if email.endswith("@" + config.JOUEUR_EMAIL_DOMAIN) or email.endswith("@" + config.WORKER_EMAIL_DOMAIN):
                pres.delete(f"/api/membres/{m['id']}"); nb_comptes += 1
        log(f"  {nb_comptes} comptes de démo (workers + joueurs) supprimés")


def _trouver_club(admin: ApiClient, nom: str) -> str | None:
    for c in admin.get("/api/clubs") or []:
        if c.get("nom") == nom:
            return c["id"]
    return None


def _supprimer_liste(client: ApiClient, path_get: str, path_delete: str | None = None) -> int:
    base = path_delete or path_get
    n = 0
    for x in client.get(path_get) or []:
        client.delete(f"{base}/{x['id']}")
        n += 1
    return n


# ═══════════════════════ Nettoyage avant re-push (idempotence) ═══════════════════════

def nettoyer_episodiques(ctx: BootstrapContext, log=print) -> None:
    """Supprime ce qui n'a pas d'upsert naturel (blessures, matchs, conseils) pour éviter les
    doublons à la réinjection. Tier-aware : n'agit que si le worker concerné existe (medical = Pro)."""
    coach = ctx.workers.get("entraineur")
    medic = ctx.workers.get("medical")
    prepa = ctx.workers.get("preparateur")

    if coach is not None:
        for m in coach.get("/api/matchs") or []:
            coach.delete(f"/api/matchs/{m['id']}")
    if medic is not None:
        for b in medic.get("/api/blessures") or []:
            medic.delete(f"/api/blessures/{b['id']}")
        ids = {c["id"] for c in (medic.get("/api/conseils") or [])}
        for j in (prepa.get("/api/joueurs/tous") or []) if prepa else []:
            for c in medic.get("/api/conseils", params={"joueurId": j["id"]}) or []:
                ids.add(c["id"])
        for cid in ids:
            medic.delete(f"/api/conseils/{cid}")
