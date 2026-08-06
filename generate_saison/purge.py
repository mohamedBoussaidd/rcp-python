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
from .api_client import ApiClient, ApiError
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
        # Chaque ressource est OPTIONNELLE : les packs ne donnent pas les mêmes modules à tous les
        # tiers (« AS Amateurs » n'a ni médical ni tactique), et un module absent répond 403. Sans
        # cette tolérance, la purge s'arrêtait au premier club du catalogue et ne nettoyait rien.
        nb_m = nb_b = 0
        for e in equipes:
            pres.set_contexte(club_id=club_id, equipe_ids=[e["id"]])
            for m in _lister(pres, "/api/matchs"):
                if _supprimer(pres, f"/api/matchs/{m['id']}"):
                    nb_m += 1
            for b in _lister(pres, "/api/blessures"):
                if _supprimer(pres, f"/api/blessures/{b['id']}"):
                    nb_b += 1
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
        for m in _lister(pres, "/api/mon-club/membres"):
            email = (m.get("email") or "").lower()
            if email.endswith("@" + config.JOUEUR_EMAIL_DOMAIN) or email.endswith("@" + config.WORKER_EMAIL_DOMAIN):
                pres.delete(f"/api/membres/{m['id']}"); nb_comptes += 1
        log(f"  {nb_comptes} comptes de démo (workers + joueurs) supprimés")


def _trouver_club(admin: ApiClient, nom: str) -> str | None:
    for c in admin.get("/api/clubs") or []:
        if c.get("nom") == nom:
            return c["id"]
    return None


def _lister(client: ApiClient, path: str) -> list:
    """
    GET tolérant : une ressource dont le module n'est pas dans le pack du club répond 403, et une
    purge ne doit jamais s'arrêter là-dessus — il n'y a alors simplement rien à supprimer.
    """
    try:
        return client.get(path) or []
    except ApiError as e:
        if e.statut in (403, 404, 409):
            return []
        raise


def _supprimer(client: ApiClient, path: str) -> bool:
    """DELETE tolérant : une ressource déjà partie en cascade n'est pas un échec de purge."""
    try:
        client.delete(path)
        return True
    except ApiError as e:
        if e.statut in (403, 404, 409):
            return False
        raise


def _supprimer_liste(client: ApiClient, path_get: str, path_delete: str | None = None) -> int:
    base = path_delete or path_get
    n = 0
    for x in _lister(client, path_get):
        if _supprimer(client, f"{base}/{x['id']}"):
            n += 1
    return n


# ═══════════════════════ Nettoyage avant re-push (idempotence) ═══════════════════════

def _fenetre(saison) -> tuple[str, str] | None:
    """Bornes ISO (première/dernière séance) du calendrier en cours d'injection."""
    dates = [s.date for s in getattr(saison, "seances", []) or []]
    return (min(dates).isoformat(), max(dates).isoformat()) if dates else None


def _dans_fenetre(valeur: str | None, bornes: tuple[str, str]) -> bool:
    """Une entrée sans date est considérée DANS la fenêtre : on ne veut pas accumuler à l'infini
    des enregistrements non datés que la réinjection recréerait."""
    if not valeur:
        return True
    return bornes[0] <= valeur[:10] <= bornes[1]


def nettoyer_episodiques(ctx: BootstrapContext, saison=None, log=print) -> None:
    """Supprime ce qui n'a pas d'upsert naturel (blessures, matchs, conseils) pour éviter les
    doublons à la réinjection. Tier-aware : n'agit que si le worker concerné existe (medical = Pro).

    ⚠ Ce nettoyage est borné à la FENÊTRE INJECTÉE. Sans bornage il supprimait tout l'historique de
    l'équipe : injecter la saison 2026-2027 a effacé les 36 matchs et toutes les blessures de
    2025-2026, que rien ne recréait ensuite. Le piège est d'autant plus vicieux que `GET /api/matchs`
    est désormais borné par saison côté back — mais le générateur n'envoie pas `X-Contexte-Saison`,
    donc il voit et détruit toutes les saisons à la fois.
    """
    coach = ctx.workers.get("entraineur")
    medic = ctx.workers.get("medical")
    prepa = ctx.workers.get("preparateur")
    bornes = _fenetre(saison)
    if bornes is None:
        log("  ⚠ nettoyage non borné (aucun calendrier fourni) — les autres saisons sont exposées")

    if coach is not None:
        n = 0
        for m in coach.get("/api/matchs") or []:
            if bornes and not _dans_fenetre(m.get("dateMatch"), bornes):
                continue
            coach.delete(f"/api/matchs/{m['id']}")
            n += 1
        if n:
            log(f"  {n} matchs de la fenêtre retirés avant re-push")
    if medic is not None:
        for b in medic.get("/api/blessures") or []:
            if bornes and not _dans_fenetre(b.get("dateBlessure"), bornes):
                continue
            medic.delete(f"/api/blessures/{b['id']}")
        # Les conseils n'ont pas de date métier (`conseil_staff` ne porte qu'un `created_at`) :
        # impossible de les rattacher à une saison, ils restent purgés en bloc.
        ids = {c["id"] for c in (medic.get("/api/conseils") or [])}
        for j in (prepa.get("/api/joueurs/tous") or []) if prepa else []:
            for c in medic.get("/api/conseils", params={"joueurId": j["id"]}) or []:
                ids.add(c["id"])
        for cid in ids:
            medic.delete(f"/api/conseils/{cid}")
