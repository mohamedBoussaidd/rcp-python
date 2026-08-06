"""
CLI du générateur de démo MULTI-CLUB (chantier A).

Peuple 3 clubs de niveaux différents (AS Amateurs / FC Semi-Pro / Olympique Pro),
chacun avec son pack, ses équipes, sa saison et des données cohérentes alignées sur
le pack. Piloté par un SUPER_ADMIN existant (réutilisé).

Exemples :
  # Aperçu (simulation seule, aucun envoi, un échantillon par niveau) :
  python -m generate_saison.generate --apercu

  # Injection en LOCAL (identifiants super-admin en CLI ou via env RCP_ADMIN_*) :
  python -m generate_saison.generate --env local \
      --admin-email admin@rcp.fr --admin-password '••••'

  # Injection en PROD (confirmation obligatoire) :
  python -m generate_saison.generate --env prod --confirm

  # Purge des 3 clubs de démo :
  python -m generate_saison.generate --env local --purge
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
from datetime import date as _date, timedelta as _timedelta

from . import config
from .config import DEFAUT, ENVIRONNEMENTS


def _args():
    p = argparse.ArgumentParser(description="Générateur de démo multi-club — Rémi C Préparateur")
    p.add_argument("--env", choices=list(ENVIRONNEMENTS), default="local")
    p.add_argument("--seed", type=int, default=DEFAUT.seed)
    p.add_argument("--semaines", type=int, default=DEFAUT.nb_semaines)
    p.add_argument("--debut-saison", metavar="AAAA-MM-JJ",
                   help=f"1er lundi de pré-saison (défaut {DEFAUT.debut_saison.isoformat()}). "
                        "Doit tomber DANS la saison EN_COURS du club, sinon les données "
                        "atterrissent hors de la saison qui est censée les contenir.")
    p.add_argument("--admin-email", help=f"super-admin pilote (sinon ${config.ADMIN_EMAIL_ENV})")
    p.add_argument("--admin-password", help=f"mot de passe super-admin (sinon ${config.ADMIN_PASSWORD_ENV})")
    p.add_argument("--confirm", action="store_true", help="obligatoire pour --env prod")
    p.add_argument("--purge", action="store_true", help="purge les clubs de démo puis quitte")
    p.add_argument("--apercu", action="store_true", help="simule et affiche le résumé sans rien envoyer")
    return p.parse_args()


def _garde_fous(a):
    if a.env == "prod" and not a.apercu and not a.confirm:
        sys.exit("✗ --env prod exige --confirm (écriture sur la PROD, confinée aux clubs de démo).")


def _debut_saison(valeur):
    """Parse `--debut-saison` et exige un LUNDI : tout le calendrier est bâti en microcycles."""
    if not valeur:
        return DEFAUT.debut_saison
    try:
        d = _date.fromisoformat(valeur)
    except ValueError:
        sys.exit(f"✗ --debut-saison « {valeur} » n'est pas une date AAAA-MM-JJ.")
    if d.weekday() != 0:
        lundi = d - _timedelta(days=d.weekday())
        sys.exit(f"✗ --debut-saison doit être un LUNDI (les semaines sont des microcycles). "
                 f"Le lundi de cette semaine-là est {lundi.isoformat()}.")
    return d


def main():
    # Console Windows : éviter les plantages d'encodage sur les symboles (✓, →…).
    for flux in (sys.stdout, sys.stderr):
        try:
            flux.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    a = _args()
    _garde_fous(a)
    params = dataclasses.replace(DEFAUT, seed=a.seed, nb_semaines=a.semaines,
                                 debut_saison=_debut_saison(a.debut_saison))

    # ── Aperçu : simulation seule, un échantillon par niveau (aucune connexion) ──
    if a.apercu:
        from .simulation import simuler
        for profil in config.PROFILS:
            p_eq = dataclasses.replace(params, nb_joueurs=profil.nb_joueurs, intensite=profil.intensite)
            print(f"\n[{profil.nom}] pack={profil.pack}, {profil.nb_equipes} équipe(s), "
                  f"{profil.nb_joueurs} joueurs/équipe")
            print(simuler(p_eq).resume())
        return

    base_url = ENVIRONNEMENTS[a.env]
    admin_email, admin_password = config.admin_credentials(a.admin_email, a.admin_password)
    print(f"Cible API : {base_url}")

    from .bootstrap import login_admin, preparer_clubs
    admin = login_admin(base_url, admin_email, admin_password)

    # ── Purge ──
    if a.purge:
        from .purge import purger_tout
        print("Purge des clubs de démo…")
        purger_tout(admin, base_url)
        print("\n✓ Purge terminée.")
        return

    # ── Injection multi-club ──
    print("Mise en place des clubs (super-admin)…")
    contexts = preparer_clubs(admin, base_url, params)

    from .pushers import pousser_tier
    for ctx in contexts:
        pousser_tier(ctx, ctx.saison_sim)

    print("\n✓ Injection multi-club terminée.")
    print("\nLogins présidents (démo) :")
    for profil in config.PROFILS:
        print(f"  {profil.nom:24s} {profil.president_email}  /  {config.PRESIDENT_DEMO_PASSWORD}")
    print(f"\nMots de passe : workers = {config.WORKER_PASSWORD} · joueurs = {config.JOUEUR_PASSWORD}")
    print("Astuce démo : connectez-vous en super-admin et voyagez dans la saison (date simulée).")


if __name__ == "__main__":
    main()
