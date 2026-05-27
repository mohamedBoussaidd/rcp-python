"""
seed_gps_test.py
Génère 4 semaines de données GPS de test avec des profils variés :
  - surcharge   : hautes charges tout le mois → fatigue élevée + risque blessure
  - pic_recent  : normal semaines 1-2, spike +50% semaines 3-4 → risque blessure brutal
  - sous_charge : distances basses systématiquement → km insuffisants
  - normal      : charges régulières et cohérentes

Usage : python seed_gps_test.py
"""

import psycopg
import psycopg.rows
import random
import os
import sys
from datetime import date, timedelta
from dotenv import load_dotenv

load_dotenv()
random.seed(42)

DB_CONFIG = {
    "host":     os.getenv("DB_HOST", "localhost"),
    "port":     int(os.getenv("DB_PORT", "5433")),
    "dbname":   os.getenv("DB_NAME", "remi_preparateur"),
    "user":     os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASSWORD", "root"),
}

# ── Calendrier : lundi de la semaine la plus ancienne ──
DEBUT = date(2026, 4, 27)

PROGRAMME_SEMAINE = [
    (0, 'REPRISE'),    # Lundi
    (1, 'TECHNIQUE'),  # Mardi
    (2, 'INTENSIF'),   # Mercredi
    (3, 'PRE_MATCH'),  # Jeudi
    (5, 'MATCH'),      # Samedi
]

# ── Plages de distance (m) normales par type ──
DIST_NORMALE = {
    'MATCH':     (8800, 10800),
    'INTENSIF':  (7000,  9000),
    'TECHNIQUE': (4800,  6500),
    'REPRISE':   (3200,  5000),
    'PRE_MATCH': (3200,  4500),
}

DUREE_MIN = {
    'MATCH':     (88, 96),
    'INTENSIF':  (72, 85),
    'TECHNIQUE': (60, 75),
    'REPRISE':   (45, 60),
    'PRE_MATCH': (45, 55),
}

ETIQUETTES = {
    'surcharge':   '🔴 SURCHARGÉ   — fatigue haute + risque blessure',
    'pic_recent':  '🟠 PIC RÉCENT  — spike S3-S4 → risque blessure',
    'sous_charge': '🟡 SOUS-CHARGÉ — km insuffisants',
    'normal':      '🟢 NORMAL      — charges régulières',
}


def gen_gps(type_code: str, profil: str, semaine: int) -> dict:
    """
    Génère des métriques GPS cohérentes selon le profil et la semaine.
    semaine : 1 (plus ancienne) → 4 (la plus récente)
    """
    dmin, dmax = DIST_NORMALE[type_code]

    if profil == 'surcharge':
        # Léger S1-S2, charge très élevée S3-S4 → ACWR ~2.0 → risque ELEVE
        coeff = 2.20 if semaine >= 3 else 0.60
        dist = random.uniform(dmin * coeff, dmax * coeff)

    elif profil == 'pic_recent':
        # Très léger S1-S2, pic brutal S3-S4 → ACWR ~2.3 → risque ELEVE
        coeff = 2.50 if semaine > 2 else 0.40
        dist = random.uniform(dmin * coeff, dmax * coeff)

    elif profil == 'sous_charge':
        # Systématiquement bas : remplaçant peu utilisé / retour blessure
        dist = random.uniform(dmin * 0.38, dmax * 0.52)

    else:  # normal
        dist = random.uniform(dmin, dmax)

    dist = round(dist, 1)
    duree = random.randint(*DUREE_MIN[type_code])

    # ── Zones d'intensité (proportions réalistes) ──
    d15 = round(dist * random.uniform(0.38, 0.50), 1)
    d19 = round(dist * random.uniform(0.18, 0.28), 1)
    d24 = round(dist * random.uniform(0.06, 0.14), 1)
    d28 = round(dist * random.uniform(0.02, 0.06), 1)

    nb_sprint = max(1, int(d24 / random.uniform(52, 82)))

    # Vitesse max : entre 28.5 et 35.5 km/h (varie par joueur naturellement)
    vmax = round(random.uniform(28.5, 35.5), 1)

    # Accélérations / freinages corrélés à l'intensité
    nb_acc = max(5, int(dist / random.uniform(170, 270)))
    nb_fre = max(4, int(nb_acc * random.uniform(0.65, 1.05)))

    ratio = round(dist / duree, 2) if duree else None

    return {
        'duree':      duree,
        'dist':       dist,
        'd15':        d15,
        'd19':        d19,
        'd24':        d24,
        'd28':        d28,
        'nb_sprint':  nb_sprint,
        'vmax':       vmax,
        'nb_acc':     nb_acc,
        'nb_fre':     nb_fre,
        'ratio':      ratio,
    }


def main():
    print("\n" + "=" * 60)
    print("  Seed GPS de test — Rémi C Préparateur")
    print("=" * 60)

    conn = psycopg.connect(**DB_CONFIG)

    try:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:

            # ── Récupération des joueurs ──
            cur.execute("SELECT id, nom, prenom FROM joueur ORDER BY nom, prenom")
            joueurs = cur.fetchall()
            if not joueurs:
                print("✗ Aucun joueur en base — lancez d'abord seed_joueurs.sql")
                sys.exit(1)
            print(f"✓ {len(joueurs)} joueurs trouvés")

            # ── Récupération des types de séance ──
            cur.execute("SELECT id, code FROM type_seance")
            types = {r['code']: r['id'] for r in cur.fetchall()}
            print(f"✓ {len(types)} types de séance : {list(types.keys())}")

            for code in ['REPRISE', 'TECHNIQUE', 'INTENSIF', 'PRE_MATCH', 'MATCH']:
                if code not in types:
                    print(f"✗ Type manquant : '{code}' — vérifiez la table type_seance")
                    sys.exit(1)

            # ── Attribution des profils ──
            ids = [j['id'] for j in joueurs]
            random.shuffle(ids)
            n = len(ids)

            n_surcharge = max(1, round(n * 0.20))
            n_pic       = max(1, round(n * 0.15))
            n_sous      = max(1, round(n * 0.17))

            profil_map = {}
            for i, jid in enumerate(ids):
                if   i < n_surcharge:
                    profil_map[jid] = 'surcharge'
                elif i < n_surcharge + n_pic:
                    profil_map[jid] = 'pic_recent'
                elif i < n_surcharge + n_pic + n_sous:
                    profil_map[jid] = 'sous_charge'
                else:
                    profil_map[jid] = 'normal'

            # ── Affichage de la répartition ──
            print()
            for profil, etiquette in ETIQUETTES.items():
                membres = [j for j in joueurs if profil_map.get(j['id']) == profil]
                noms = ', '.join(f"{j['prenom']} {j['nom']}" for j in membres)
                print(f"  {etiquette}")
                print(f"     → {noms}")
            print()

            # ── Génération des séances et des données GPS ──
            nb_seances = 0
            nb_gps     = 0

            for semaine in range(1, 5):
                lundi = DEBUT + timedelta(weeks=semaine - 1)

                for delta_jour, type_code in PROGRAMME_SEMAINE:
                    date_seance = lundi + timedelta(days=delta_jour)
                    type_id = types[type_code]

                    # Créer la séance si elle n'existe pas
                    cur.execute(
                        "SELECT id FROM seance WHERE date = %s AND type_seance_id = %s",
                        (date_seance, type_id)
                    )
                    row = cur.fetchone()

                    if row:
                        seance_id = row['id']
                    else:
                        cur.execute(
                            "INSERT INTO seance (type_seance_id, date) VALUES (%s, %s) RETURNING id",
                            (type_id, date_seance)
                        )
                        seance_id = cur.fetchone()['id']
                        nb_seances += 1

                    # Insérer ou mettre à jour les données GPS pour chaque joueur
                    for joueur in joueurs:
                        jid    = joueur['id']
                        profil = profil_map[jid]

                        g = gen_gps(type_code, profil, semaine)

                        cur.execute("""
                            INSERT INTO donnee_gps (
                                joueur_id, seance_id,
                                duree_minutes, distance_totale_m,
                                distance_15kmh_m, distance_19kmh_m,
                                distance_sprint_24kmh_m, distance_sprint_28kmh_m,
                                nb_sprints_24kmh, vitesse_max_kmh,
                                nb_accelerations, nb_freinages, ratio_distance_min
                            ) VALUES (
                                %s, %s,
                                %s, %s,
                                %s, %s, %s, %s,
                                %s, %s,
                                %s, %s, %s
                            )
                            ON CONFLICT (joueur_id, seance_id) DO UPDATE SET
                                duree_minutes            = EXCLUDED.duree_minutes,
                                distance_totale_m        = EXCLUDED.distance_totale_m,
                                distance_15kmh_m         = EXCLUDED.distance_15kmh_m,
                                distance_19kmh_m         = EXCLUDED.distance_19kmh_m,
                                distance_sprint_24kmh_m  = EXCLUDED.distance_sprint_24kmh_m,
                                distance_sprint_28kmh_m  = EXCLUDED.distance_sprint_28kmh_m,
                                nb_sprints_24kmh         = EXCLUDED.nb_sprints_24kmh,
                                vitesse_max_kmh          = EXCLUDED.vitesse_max_kmh,
                                nb_accelerations         = EXCLUDED.nb_accelerations,
                                nb_freinages             = EXCLUDED.nb_freinages,
                                ratio_distance_min       = EXCLUDED.ratio_distance_min
                        """, (
                            jid, seance_id,
                            g['duree'], g['dist'],
                            g['d15'],   g['d19'],
                            g['d24'],   g['d28'],
                            g['nb_sprint'], g['vmax'],
                            g['nb_acc'], g['nb_fre'], g['ratio'],
                        ))
                        nb_gps += 1

            conn.commit()

            print(f"{'=' * 60}")
            print(f"  ✓ {nb_seances} séances créées")
            print(f"  ✓ {nb_gps} entrées GPS insérées")
            print(f"{'=' * 60}\n")

    except Exception as e:
        conn.rollback()
        import traceback
        print(f"\n✗ Erreur — rollback effectué : {e}")
        traceback.print_exc()
        sys.exit(1)

    finally:
        conn.close()


if __name__ == "__main__":
    main()
