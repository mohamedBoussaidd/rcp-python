# Générateur de saison démo — Rémi C Préparateur

Génère un **jeu de données réaliste et cohérent** sur une saison complète de
football (≈40 semaines) pour un **club de DÉMO**, et l'injecte via l'**API** du
backend (voie robuste, multi-tenant, champs calculés justes type `scoreBienEtre`).

Le moteur est *causal* : la charge GPS d'une séance (modulée par le poste et le
type de séance) alimente une charge aiguë/chronique (ACWR) qui pilote la fatigue,
le **wellness** et le **RPE** du lendemain, ainsi que le **risque de blessure**.
→ Pas de cas absurde (GPS élevé + ressenti « tout va bien », surpoids généralisé…).

## Couverture

GPS · wellness (Hooper 1..5) · sRPE · pesées/IMC · blessures + protocole RTP ·
séances + exercices typés · plan de jeu · matchs (prépa/débrief) · schémas &
formations · conseils staff.

## Prérequis

1. **Backend démarré** (local : `http://localhost:8080`).
2. **Compte président démo créé à la main** en base (rôle `PRESIDENT`, un club
   rattaché) :
   - email : `compte@demo.fr`
   - mot de passe : `demodaydaydemo9999`
   Le générateur crée ensuite **tout le reste automatiquement** (équipe démo,
   comptes techniques, 25 joueurs, comptes joueurs).
3. Dépendances Python : `pip install -r ../requirements.txt` (utilise `requests`,
   `numpy`).

## Utilisation

Depuis le dossier `python/` :

```bash
# Aperçu (simulation seule, aucun envoi)
python -m generate_saison.generate --apercu

# Injection en LOCAL
python -m generate_saison.generate --env local --sortie api

# Injection en PROD (confinée au club démo ; confirmation obligatoire)
python -m generate_saison.generate --env prod --sortie api --confirm

# Purge complète du tenant démo (contenu + comptes joueurs)
python -m generate_saison.generate --env local --purge

# Export SQL (LOCAL uniquement, cœur GPS)
python -m generate_saison.generate --sortie sql --sql-fichier saison_demo.sql --equipe-id <UUID_EQUIPE>
```

Options : `--seed N` (reproductibilité), `--semaines N` (durée), `--sans-tactique`
(omettre plan de jeu / matchs / schémas).

## Rafraîchir vs purger

- **Rafraîchir** = relancer la génération. C'est **idempotent** : les séances sont
  réutilisées (clé = date), GPS/RPE/wellness/pesées s'**upsertent**, blessures/
  conseils/matchs sont nettoyés puis recréés. Aucun doublon. **Pas besoin de purger.**
- **Purger** = teardown complet du contenu démo (supprime aussi les fiches et les
  comptes joueurs ; conserve l'équipe, les workers et le président). À utiliser
  pour repartir de zéro.

## Garde-fous PROD

- Écriture **confinée au club démo** : le générateur exige que le compte connecté
  soit le président démo (sinon il refuse de tourner).
- `--env prod` exige `--confirm`.
- **Sortie SQL interdite en prod** (API uniquement).

## Comptes créés

| Rôle | Email | Mot de passe | Usage |
|------|-------|--------------|-------|
| PRESIDENT | `compte@demo.fr` | `demodaydaydemo9999` | **connexion client démo** (créé à la main) |
| PREPARATEUR | `prepa@staff.demo.fr` | `DemoWorker2026!` | joueurs, GPS, pesées, séances |
| ENTRAINEUR | `coach@staff.demo.fr` | `DemoWorker2026!` | plan de jeu, matchs, schémas |
| MEDICAL | `medic@staff.demo.fr` | `DemoWorker2026!` | blessures, conseils |
| JOUEUR ×25 | `j<n>.<nom>@joueur.demo.fr` | `DemoJoueur2026!` | saisie wellness / RPE |

## Architecture

```
generate_saison/
  config.py        paramètres, comptes, garde-fous
  catalog.py       postes, anthropométrie, micro-cycle, mappings DB
  profils.py       génération des 25 joueurs (déterministe)
  calendrier.py    saison + micro-cycles + matchs
  simulation.py    MOTEUR : charge → ACWR → wellness/RPE → blessures
  api_client.py    client HTTP (auth, contexte multi-tenant)
  bootstrap.py     mise en place du tenant démo (idempotent)
  pushers.py       envoi de chaque type de donnée (bon rôle)
  purge.py         teardown + nettoyage non-idempotents
  sortie_sql.py    export SQL optionnel (local)
  generate.py      CLI
```
