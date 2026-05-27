"""
============================================================
Rémi C Préparateur - Script d'import des fichiers Excel GPS
============================================================

Usage :
    python import_gps.py --fichier "seance.xlsx" --type INTENSIF --date 2024-10-15

Arguments :
    --fichier   : chemin vers le fichier Excel GPS
    --type      : type de séance (REPRISE, INTENSIF, TECHNIQUE, PRE_MATCH, MATCH, MATCH_AMICAL, FORCE)
    --date      : date de la séance au format YYYY-MM-DD
    --heure     : heure de début (optionnel, ex: 10:00)
    --meteo     : conditions météo (optionnel : beau, nuageux, pluie, vent_fort)
    --terrain   : type de terrain (optionnel : gazon_naturel, gazon_synthetique, salle)
    --resultat  : résultat si match (optionnel : V, N, D)
    --score     : score si match (optionnel, ex: 2-1)
    --lieu      : D (domicile) ou E (extérieur) si match

Exemples :
    # Importer une séance d'entraînement
    python import_gps.py --fichier "lundi_reprise.xlsx" --type REPRISE --date 2024-10-14

    # Importer un match
    python import_gps.py --fichier "J26.xlsx" --type MATCH --date 2024-10-19 --resultat V --score 2-1 --lieu D
"""

import argparse
import sys
import re
import pandas as pd
import psycopg
from datetime import datetime
from dotenv import load_dotenv
import os

load_dotenv()

# ============================================================
# CONFIGURATION BASE DE DONNÉES (via .env)
# ============================================================
DB_CONFIG = {
    "host":     os.getenv("DB_HOST", "localhost"),
    "port":     int(os.getenv("DB_PORT", "5433")),
    "dbname":   os.getenv("DB_NAME", "remi_preparateur"),
    "user":     os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASSWORD", "root"),
}

# ============================================================
# MAPPING DES COLONNES EXCEL → BASE DE DONNÉES
# Correspond exactement à ton format de fichier
# ============================================================
COLONNES_GPS = {
    "Minute":                           "duree_minutes",
    "Distance totale  (m)":             "distance_totale_m",
    "Distance (m)  > 15km/h":          "distance_15kmh_m",
    "Distance (m)  > 19km/h":          "distance_19kmh_m",
    "Distance Sprint (m)  > 24km/h":   "distance_sprint_24kmh_m",
    "Distance Sprint (m)  > 28km/h":   "distance_sprint_28kmh_m",
    "Nombre de sprint >  24  km/h":    "nb_sprints_24kmh",
    "Vitesse max (Km/h)":              "vitesse_max_kmh",
    "Nombre d' accélérations":         "nb_accelerations",
    "Nombre de freinages":             "nb_freinages",
    "Ratio Distance (m/min)":          "ratio_distance_min",
}

# Lignes à ignorer (objectifs par poste)
POSTES_A_IGNORER = [
    "Attaquant", "Ailiers", "Milieu", "Latéral",
    "Defcentral", "Def central", "MOYENNE", "Objectif"
]

# ============================================================
# FONCTIONS UTILITAIRES
# ============================================================

def connecter_db():
    """Connexion à PostgreSQL"""
    try:
        conn = psycopg.connect(**DB_CONFIG)
        print("✓ Connecté à PostgreSQL")
        return conn
    except Exception as e:
        print(f"✗ Erreur connexion PostgreSQL : {e}")
        print("  Vérifiez le fichier .env")
        sys.exit(1)


def nettoyer_valeur(valeur):
    """Convertit une valeur Excel en float propre"""
    if pd.isna(valeur) or valeur == "" or valeur is None:
        return None
    try:
        return float(str(valeur).replace(",", ".").strip())
    except (ValueError, TypeError):
        return None


def nettoyer_nom(nom):
    """Nettoie un nom de joueur"""
    if pd.isna(nom) or nom is None:
        return None
    return str(nom).strip()


def est_ligne_joueur(row, colonne_nom):
    """Vérifie si une ligne correspond à un vrai joueur"""
    nom = nettoyer_nom(row.get(colonne_nom, ""))
    if not nom:
        return False
    # Rejeter si le nom contient des sauts de ligne (lignes d'objectifs multi-lignes)
    if "\n" in nom:
        return False
    # Rejeter les noms trop longs (lignes d'objectifs)
    if len(nom) > 60:
        return False
    # Rejeter si contient des chiffres (objectifs chiffrés)
    if any(c.isdigit() for c in nom):
        return False
    # Ignorer les lignes d'objectifs et de moyenne
    for poste in POSTES_A_IGNORER:
        if poste.lower() in nom.lower():
            return False
    return True


def trouver_ou_creer_joueur(conn, nom):
    """Cherche un joueur par nom, le crée s'il n'existe pas"""
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute("""
            SELECT id, nom, prenom
            FROM joueur
            WHERE LOWER(TRIM(nom)) = LOWER(TRIM(%s))
               OR LOWER(TRIM(CONCAT(prenom, ' ', nom))) = LOWER(TRIM(%s))
               OR LOWER(TRIM(CONCAT(nom, ' ', prenom))) = LOWER(TRIM(%s))
        """, (nom, nom, nom))

        joueur = cur.fetchone()

        if joueur:
            return joueur["id"]

        print(f"  → Nouveau joueur détecté : '{nom}' — créé automatiquement")
        parties = nom.strip().split(" ", 1)
        prenom = parties[0] if len(parties) > 1 else ""
        nom_famille = parties[1] if len(parties) > 1 else parties[0]

        cur.execute("""
            INSERT INTO joueur (nom, prenom, statut)
            VALUES (%s, %s, 'actif')
            RETURNING id
        """, (nom_famille, prenom))

        return cur.fetchone()["id"]


def trouver_type_seance(conn, code_type):
    """Récupère l'UUID du type de séance"""
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute("SELECT id FROM type_seance WHERE code = %s", (code_type,))
        result = cur.fetchone()
        if not result:
            print(f"✗ Type de séance '{code_type}' non trouvé en base")
            sys.exit(1)
        return result["id"]


def creer_seance(conn, args, type_seance_id):
    """Crée la séance en base et retourne son UUID"""
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:

        # Vérifier si une séance existe déjà pour cette date et ce type
        cur.execute("""
            SELECT id FROM seance
            WHERE date = %s AND type_seance_id = %s
        """, (args.date, type_seance_id))

        existante = cur.fetchone()
        if existante:
            print(f"  ⚠ Séance du {args.date} ({args.type}) déjà existante — réutilisation")
            return existante[0]

        cur.execute("""
            INSERT INTO seance (
                type_seance_id, date, heure_debut, heure_fin,
                conditions_meteo, terrain,
                resultat_match, score_match, domicile_exterieur
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (
            type_seance_id,
            args.date,
            args.heure or None,
            None,
            args.meteo or None,
            args.terrain or None,
            args.resultat or None,
            args.score or None,
            args.lieu or None,
        ))

        seance_id = cur.fetchone()["id"]
        print(f"✓ Séance créée : {args.date} ({args.type})")
        return seance_id


def importer_donnees_gps(conn, df, seance_id, colonne_nom):
    """Importe les données GPS de chaque joueur"""
    nb_importes = 0
    nb_erreurs = 0
    nb_ignores = 0

    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        for _, row in df.iterrows():
            nom = nettoyer_nom(row.get(colonne_nom, ""))

            # Ignorer les lignes non-joueurs
            if not est_ligne_joueur(row, colonne_nom):
                nb_ignores += 1
                continue

            try:
                joueur_id = trouver_ou_creer_joueur(conn, nom)

                # Construire le dict des données GPS
                donnees = {}
                for col_excel, col_db in COLONNES_GPS.items():
                    # Cherche la colonne même si le nom est légèrement différent
                    valeur = None
                    for col in df.columns:
                        if col_excel.lower().strip() in col.lower().strip() or \
                           col.lower().strip() in col_excel.lower().strip():
                            valeur = nettoyer_valeur(row.get(col))
                            break
                    donnees[col_db] = valeur

                # Vérifier si les données existent déjà
                cur.execute("""
                    SELECT id FROM donnee_gps
                    WHERE joueur_id = %s AND seance_id = %s
                """, (joueur_id, seance_id))

                existant = cur.fetchone()

                if existant is not None:
                    # Mise à jour
                    cur.execute("""
                        UPDATE donnee_gps SET
                            duree_minutes = %s,
                            distance_totale_m = %s,
                            distance_15kmh_m = %s,
                            distance_19kmh_m = %s,
                            distance_sprint_24kmh_m = %s,
                            distance_sprint_28kmh_m = %s,
                            nb_sprints_24kmh = %s,
                            vitesse_max_kmh = %s,
                            nb_accelerations = %s,
                            nb_freinages = %s,
                            ratio_distance_min = %s
                        WHERE joueur_id = %s AND seance_id = %s
                    """, (
                        donnees["duree_minutes"],
                        donnees["distance_totale_m"],
                        donnees["distance_15kmh_m"],
                        donnees["distance_19kmh_m"],
                        donnees["distance_sprint_24kmh_m"],
                        donnees["distance_sprint_28kmh_m"],
                        donnees["nb_sprints_24kmh"],
                        donnees["vitesse_max_kmh"],
                        donnees["nb_accelerations"],
                        donnees["nb_freinages"],
                        donnees["ratio_distance_min"],
                        joueur_id, seance_id
                    ))
                    print(f"  ↻ Mis à jour : {nom}")
                else:
                    # Insertion
                    cur.execute("""
                        INSERT INTO donnee_gps (
                            joueur_id, seance_id,
                            duree_minutes, distance_totale_m,
                            distance_15kmh_m, distance_19kmh_m,
                            distance_sprint_24kmh_m, distance_sprint_28kmh_m,
                            nb_sprints_24kmh, vitesse_max_kmh,
                            nb_accelerations, nb_freinages, ratio_distance_min
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """, (
                        joueur_id, seance_id,
                        donnees["duree_minutes"],
                        donnees["distance_totale_m"],
                        donnees["distance_15kmh_m"],
                        donnees["distance_19kmh_m"],
                        donnees["distance_sprint_24kmh_m"],
                        donnees["distance_sprint_28kmh_m"],
                        donnees["nb_sprints_24kmh"],
                        donnees["vitesse_max_kmh"],
                        donnees["nb_accelerations"],
                        donnees["nb_freinages"],
                        donnees["ratio_distance_min"],
                    ))
                    print(f"  ✓ Importé  : {nom}")

                nb_importes += 1

            except Exception as e:
                print(f"  ✗ Erreur pour '{nom}' : {e}")
                nb_erreurs += 1

    return nb_importes, nb_erreurs, nb_ignores


def lire_excel(fichier):
    """Lit le fichier Excel et retourne les DataFrames par onglet"""
    try:
        xl = pd.ExcelFile(fichier)
        print(f"✓ Fichier lu : {fichier}")
        print(f"  Onglets trouvés : {xl.sheet_names}")
        return xl
    except Exception as e:
        print(f"✗ Erreur lecture Excel : {e}")
        sys.exit(1)


def trouver_colonne_nom(df):
    """Trouve automatiquement la colonne qui contient les noms des joueurs"""
    # La première colonne non-numérique contient les noms
    for col in df.columns:
        valeurs = df[col].dropna().astype(str)
        # Si la colonne contient des noms alphabétiques
        noms_alpha = valeurs[valeurs.str.match(r'^[A-Za-zÀ-ÿ\s\-]+$')]
        if len(noms_alpha) > 3:
            return col
    return df.columns[0]


# ============================================================
# PROGRAMME PRINCIPAL
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="Import GPS Excel → PostgreSQL")
    parser.add_argument("--fichier",  required=True,  help="Chemin vers le fichier Excel")
    parser.add_argument("--type",     required=True,  help="Type de séance (REPRISE, INTENSIF, etc.)")
    parser.add_argument("--date",     required=True,  help="Date de la séance (YYYY-MM-DD)")
    parser.add_argument("--heure",    required=False, help="Heure de début (HH:MM)")
    parser.add_argument("--meteo",    required=False, help="Conditions météo")
    parser.add_argument("--terrain",  required=False, help="Type de terrain")
    parser.add_argument("--resultat", required=False, help="Résultat match (V/N/D)")
    parser.add_argument("--score",    required=False, help="Score du match (ex: 2-1)")
    parser.add_argument("--lieu",     required=False, help="Domicile/Extérieur (D/E)")
    args = parser.parse_args()

    print("\n" + "="*55)
    print("  Import GPS — Rémi C Préparateur")
    print("="*55)
    print(f"  Fichier  : {args.fichier}")
    print(f"  Type     : {args.type}")
    print(f"  Date     : {args.date}")
    print("="*55 + "\n")

    # Connexion
    conn = connecter_db()

    try:
        # Récupérer le type de séance
        type_seance_id = trouver_type_seance(conn, args.type)

        # Créer la séance
        seance_id = creer_seance(conn, args, type_seance_id)

        # Lire le fichier Excel
        xl = lire_excel(args.fichier)

        total_importes = 0
        total_erreurs  = 0

        # Parcourir chaque onglet
        for onglet in xl.sheet_names:
            print(f"\n--- Onglet : {onglet} ---")

            # Lire sans entête pour voir la structure brute
            df_raw = xl.parse(onglet, header=None)

            # Chercher la ligne qui contient les vrais noms de colonnes GPS
            # (celle qui contient "Minute" ou "Distance")
            header_row = None
            for i, row in df_raw.iterrows():
                row_str = " ".join(str(v) for v in row.values if pd.notna(v)).lower()
                if "minute" in row_str or "distance" in row_str:
                    header_row = i
                    break

            if header_row is None:
                print(f"  ⚠ Impossible de trouver la ligne d'entête GPS — onglet ignoré")
                continue

            # Extraire les noms de colonnes depuis cette ligne, normaliser les sauts de ligne
            col_names = [
                str(v).replace("\n", " ").strip() if pd.notna(v) else f"col_{j}"
                for j, v in enumerate(df_raw.iloc[header_row])
            ]

            # Lire les données à partir de la ligne suivante
            df = df_raw.iloc[header_row + 1:].copy()
            df.columns = col_names
            df = df.reset_index(drop=True)

            # Trouver la colonne des noms
            colonne_nom = trouver_colonne_nom(df)
            print(f"  Colonne noms : '{colonne_nom}'")
            print(f"  Lignes trouvées : {len(df)}")

            # Importer
            nb_imp, nb_err, nb_ign = importer_donnees_gps(
                conn, df, seance_id, colonne_nom
            )
            total_importes += nb_imp
            total_erreurs  += nb_err

        # Valider la transaction
        conn.commit()

        print(f"\n{'='*55}")
        print(f"  ✓ Import terminé avec succès!")
        print(f"  Joueurs importés : {total_importes}")
        print(f"  Erreurs          : {total_erreurs}")
        print(f"{'='*55}\n")

    except Exception as e:
        conn.rollback()
        print(f"\n✗ Erreur critique — rollback effectué : {e}")
        sys.exit(1)

    finally:
        conn.close()


if __name__ == "__main__":
    main()
