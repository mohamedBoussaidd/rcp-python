# Générateur de démo MULTI-CLUB — Rémi C Préparateur

Peuple **3 clubs de démo de niveaux différents**, chacun avec son **pack**, ses
**équipes**, sa **saison** et des données **réalistes et cohérentes** (moteur causal :
charge → ACWR → wellness/RPE → blessures). Les données sont **alignées sur le pack**
(un club sans GPS ne reçoit pas de GPS → aucune donnée fantôme masquée).

Piloté par un **SUPER_ADMIN existant** (réutilisé) qui crée les clubs et affecte les packs.

## Topologie générée

| Club | Équipes | Effectif/éq. | Pack | Données |
|------|---------|--------------|------|---------|
| **AS Amateurs (Démo)** | 1 | 18 | Prépa | séances, présence, wellness/RPE, pesées, matchs — *pas de GPS/tactique/médical* |
| **FC Semi-Pro (Démo)** | 2 | 25 | Performance | + GPS, tactique (exos/plan de jeu/schémas), diaporama |
| **Olympique Pro (Démo)** | 3 | 25 | Complet | + médical (blessures/RTP/conseils), notifications, rôle custom « Entraîneur adjoint » |

## Prérequis

1. **Backend démarré** (local : `http://localhost:8080`) avec la migration **V42** appliquée.
2. **Un compte SUPER_ADMIN existant** (le générateur crée tout le reste : clubs, présidents,
   équipes, workers, joueurs, saison…). Identifiants fournis via CLI ou variables d'env.
3. Dépendances Python : `pip install -r ../requirements.txt` (`requests`, `numpy`).

## Utilisation

Depuis le dossier `python/` :

```bash
# Aperçu (simulation seule, aucun envoi — un échantillon par niveau) :
python -m generate_saison.generate --apercu

# Injection en LOCAL (identifiants super-admin en CLI…) :
python -m generate_saison.generate --env local \
    --admin-email admin@exemple.fr --admin-password '••••'

# …ou via variables d'environnement (recommandé, non commité) :
export RCP_ADMIN_EMAIL=admin@exemple.fr
export RCP_ADMIN_PASSWORD='••••'
python -m generate_saison.generate --env local

# Injection en PROD (confirmation obligatoire) :
python -m generate_saison.generate --env prod --confirm

# Purge des 3 clubs de démo :
python -m generate_saison.generate --env local --purge
```

Options : `--seed N` (reproductibilité), `--semaines N` (durée de saison).

## Comptes créés

| Rôle | Email | Mot de passe |
|------|-------|--------------|
| PRÉSIDENT (1/club) | `president.amateurs@demo.fr`, `president.semi@demo.fr`, `president.pro@demo.fr` | `DemoPresident2026!` |
| Workers (par équipe) | `prepa.<club><équipe>@staff.demo.fr`, `coach.…`, `medic.…` (medic = Pro) | `DemoWorker2026!` |
| JOUEUR (par joueur) | `<club><équipe>.j<n>.<nom>@joueur.demo.fr` | `DemoJoueur2026!` |

> Le SUPER_ADMIN pilote n'est PAS créé par le générateur : c'est un compte existant réutilisé.

## Démo « vivante » (voyage dans la saison)

La saison générée est datée sur 2025-2026. Pour la voir « en direct », connectez-vous
en **SUPER_ADMIN**, entrez dans le contexte d'un club, et utilisez la **date simulée**
(chantier B : lecture seule, réservée super-admin) pour vous placer à n'importe quel
instant de la saison.

## Rafraîchir vs purger

- **Rafraîchir** = relancer la commande. Idempotent : clubs/équipes/comptes réutilisés,
  séances réutilisées par date, GPS/RPE/wellness/pesées upsertés, épisodiques (matchs/
  blessures/conseils) nettoyés puis recréés. Pas besoin de purger.
- **Purger** = supprime le CONTENU des 3 clubs (+ comptes workers/joueurs de démo).
  Conserve les clubs, leurs présidents et les équipes.

## Garde-fous

- Écritures **confinées aux clubs de démo** (`config.PROFILS`).
- `--env prod` exige `--confirm`.
- Le générateur refuse de tourner si le compte fourni n'est pas SUPER_ADMIN.

## Architecture

```
generate_saison/
  config.py        paramètres, PROFILS (3 niveaux), comptes, garde-fous
  catalog.py       postes, anthropométrie, micro-cycle, mappings DB
  profils.py       génération des joueurs (déterministe, par effectif)
  calendrier.py    saison + micro-cycles + matchs
  simulation.py    MOTEUR causal : charge → ACWR → wellness/RPE → blessures (intensité/niveau)
  api_client.py    client HTTP (auth, contexte multi-tenant)
  bootstrap.py     mise en place MULTI-CLUB (super-admin → clubs/packs, président/équipes/workers/joueurs, saison)
  pushers.py       envoi filtré par pack (pousser_tier) + présence/diaporama/notifications
  purge.py         teardown multi-club + nettoyage non-idempotents
  generate.py      CLI
```
