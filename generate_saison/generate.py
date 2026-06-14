"""
CLI du générateur de saison démo.

Exemples :
  # Aperçu (simulation seule, aucun envoi) :
  python -m generate_saison.generate --apercu

  # Injection en LOCAL via l'API :
  python -m generate_saison.generate --env local --sortie api

  # Injection en PROD (confinée au club démo, confirmation obligatoire) :
  python -m generate_saison.generate --env prod --sortie api --confirm

  # Purge du tenant démo (API) :
  python -m generate_saison.generate --env local --purge

  # Export SQL (local uniquement, cœur GPS) :
  python -m generate_saison.generate --sortie sql --sql-fichier saison_demo.sql --equipe-id <UUID>
"""

from __future__ import annotations

import argparse
import dataclasses
import sys

from . import config
from .config import DEFAUT, ENVIRONNEMENTS
from .simulation import simuler


def _args():
    p = argparse.ArgumentParser(description="Générateur de saison démo — Rémi C Préparateur")
    p.add_argument("--env", choices=list(ENVIRONNEMENTS), default="local")
    p.add_argument("--sortie", choices=["api", "sql"], default="api")
    p.add_argument("--seed", type=int, default=DEFAUT.seed)
    p.add_argument("--semaines", type=int, default=DEFAUT.nb_semaines)
    p.add_argument("--confirm", action="store_true", help="obligatoire pour --env prod")
    p.add_argument("--purge", action="store_true", help="purge le tenant démo (API) puis quitte")
    p.add_argument("--apercu", action="store_true", help="simule et affiche le résumé sans rien envoyer")
    p.add_argument("--sans-tactique", action="store_true", help="n'injecte pas plan de jeu / matchs / schémas")
    p.add_argument("--equipe-id", help="(sortie SQL) rattache joueurs/séances à cette équipe")
    p.add_argument("--sql-fichier", default="saison_demo.sql")
    return p.parse_args()


def _garde_fous(a):
    if a.sortie == "sql" and a.env == "prod":
        sys.exit("✗ Sortie SQL interdite en --env prod. Utilisez --sortie api.")
    if a.purge and a.sortie == "sql":
        sys.exit("✗ La purge passe par l'API ; incompatible avec --sortie sql.")
    if a.env == "prod" and not a.apercu and not a.confirm:
        sys.exit("✗ --env prod exige --confirm (écriture sur la PROD, confinée au club démo).")


def main():
    # Console Windows : éviter les plantages d'encodage sur les symboles (✓, →…).
    for flux in (sys.stdout, sys.stderr):
        try:
            flux.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    a = _args()
    _garde_fous(a)

    params = dataclasses.replace(DEFAUT, seed=a.seed, nb_semaines=a.semaines)

    # Purge : pas besoin de simuler (teardown du tenant démo via l'API).
    if a.purge:
        base_url = ENVIRONNEMENTS[a.env]
        print(f"Cible API : {base_url}  (club démo : {config.PRESIDENT_EMAIL})")
        from .bootstrap import bootstrap
        from .purge import purger
        print("Bootstrap léger + purge…")
        ctx = bootstrap(base_url, params, [], creer_effectif=False)
        purger(ctx)
        print("\n✓ Purge terminée.")
        return

    print("Simulation de la saison…")
    saison = simuler(params)
    print(saison.resume())

    if a.apercu:
        return

    # ── Sortie SQL (local) ──
    if a.sortie == "sql":
        from .sortie_sql import ecrire_sql
        chemin = ecrire_sql(saison, a.sql_fichier, equipe_id=a.equipe_id)
        print(f"\n✓ SQL écrit : {chemin}")
        if not a.equipe_id:
            print("  (sans --equipe-id : données non rattachées à une équipe → invisibles en vue multi-tenant)")
        return

    # ── Sortie API ──
    base_url = ENVIRONNEMENTS[a.env]
    print(f"\nCible API : {base_url}  (club démo : {config.PRESIDENT_EMAIL})")
    from .bootstrap import bootstrap
    from .pushers import pousser_tout

    print("Bootstrap du tenant démo (équipe, comptes, joueurs)…")
    ctx = bootstrap(base_url, params, saison.effectif, creer_effectif=True)
    pousser_tout(ctx, saison, inclure_tactique=not a.sans_tactique)

    print("\n✓ Injection terminée.")
    if ctx.comptes_crees:
        print(f"\nComptes créés ({len(ctx.comptes_crees)}) :")
        for role, email, mdp in ctx.comptes_crees[:6]:
            print(f"  {role:11s} {email}  /  {mdp}")
        if len(ctx.comptes_crees) > 6:
            print(f"  … et {len(ctx.comptes_crees) - 6} autres (workers + joueurs).")
    print(f"\nConnexion client démo : {config.PRESIDENT_EMAIL} / {config.PRESIDENT_PASSWORD}")


if __name__ == "__main__":
    main()
