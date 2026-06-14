"""
Données de référence "métier" du football : postes, anthropométrie réaliste par
poste, profils athlétiques, micro-cycle hebdomadaire, plages GPS par type de
séance et modulateurs par poste.

Ces tables sont la source de la COHÉRENCE : l'anthropométrie dépend du poste, la
charge GPS dépend du type de séance ET du poste, etc. Aucune valeur n'est tirée
de façon indépendante.
"""

from __future__ import annotations

# ─────────────────────────── Postes & composition de l'effectif ───────────────────────────
# Codes alignés sur seed_joueurs.sql (poste_principal).

GARDIEN = "gardien"
DEF_CENTRAL = "defenseur_central"
LAT_DROIT = "lateral_droit"
LAT_GAUCHE = "lateral_gauche"
MIL_DEFENSIF = "milieu_defensif"
MIL_CENTRAL = "milieu_central"
MIL_OFFENSIF = "milieu_offensif"
AIL_DROIT = "ailier_droit"
AIL_GAUCHE = "ailier_gauche"
AVANT_CENTRE = "avant_centre"

# Composition d'un effectif senior de 25 (sommes = 25).
COMPOSITION: dict[str, int] = {
    GARDIEN: 3,
    DEF_CENTRAL: 4,
    LAT_DROIT: 2,
    LAT_GAUCHE: 2,
    MIL_DEFENSIF: 3,
    MIL_CENTRAL: 3,
    MIL_OFFENSIF: 2,
    AIL_DROIT: 2,
    AIL_GAUCHE: 2,
    AVANT_CENTRE: 2,
}

# Grandes familles, pour les modulateurs de charge.
FAMILLE = {
    GARDIEN: "gardien",
    DEF_CENTRAL: "axe",
    LAT_DROIT: "lateral",
    LAT_GAUCHE: "lateral",
    MIL_DEFENSIF: "milieu",
    MIL_CENTRAL: "milieu",
    MIL_OFFENSIF: "milieu",
    AIL_DROIT: "ailier",
    AIL_GAUCHE: "ailier",
    AVANT_CENTRE: "attaquant",
}

POSTE_LIBELLE = {
    GARDIEN: "Gardien", DEF_CENTRAL: "Défenseur central",
    LAT_DROIT: "Latéral droit", LAT_GAUCHE: "Latéral gauche",
    MIL_DEFENSIF: "Milieu défensif", MIL_CENTRAL: "Milieu central",
    MIL_OFFENSIF: "Milieu offensif", AIL_DROIT: "Ailier droit",
    AIL_GAUCHE: "Ailier gauche", AVANT_CENTRE: "Avant-centre",
}

# Codes attendus par la base (contrainte joueur_poste_principal_check) :
# GK, DC, LB, RB, MDC, MC, MG, MD, AG, AD, ATT. On mappe nos postes internes.
POSTE_DB = {
    GARDIEN: "GK", DEF_CENTRAL: "DC", LAT_DROIT: "RB", LAT_GAUCHE: "LB",
    MIL_DEFENSIF: "MDC", MIL_CENTRAL: "MC", MIL_OFFENSIF: "MC",
    AIL_DROIT: "AD", AIL_GAUCHE: "AG", AVANT_CENTRE: "ATT",
}

# ─────────────────────────── Anthropométrie réaliste par poste ───────────────────────────
# (taille_moy_cm, taille_ecart, poids_moy_kg, poids_ecart). Le poids "de forme"
# est dérivé d'un IMC athlétique (~22-24) → AUCUN surpoids généralisé.
ANTHROPO = {
    "gardien":   (189, 4, 84, 4),
    "axe":       (187, 4, 82, 4),
    "lateral":   (178, 4, 73, 3),
    "milieu":    (180, 5, 75, 4),
    "ailier":    (175, 5, 71, 4),
    "attaquant": (183, 5, 79, 5),
}

# Profils athlétiques plausibles par famille (alignés sur seed_joueurs.sql).
PROFILS_ATHLE = {
    "gardien":   ["central_costaud", "central_rapide"],
    "axe":       ["central_costaud", "central_rapide", "sentinelle"],
    "lateral":   ["lateral_offensif", "explosif_leger"],
    "milieu":    ["box_to_box", "sentinelle", "explosif_leger"],
    "ailier":    ["explosif_leger", "lateral_offensif"],
    "attaquant": ["renard_surfaces", "attaquant_profondeur", "explosif_leger"],
}

PIEDS = ["droit", "droit", "droit", "gauche", "ambidextre"]  # majorité droitiers

# ─────────────────────────── Micro-cycle hebdomadaire ───────────────────────────
# (delta_jour depuis le lundi, code_type_seance). Match le samedi.
# MD-4 (mar) charge montante → MD-1 (ven) activation légère → Match (sam).
PROGRAMME_SEMAINE = [
    (0, "REPRISE"),    # Lundi  : récupération post-match / réveil musculaire
    (1, "TECHNIQUE"),  # Mardi  : technique-tactique, charge modérée
    (2, "INTENSIF"),   # Mercredi: pic de charge physique
    (3, "TECHNIQUE"),  # Jeudi  : tactique, charge modérée
    (4, "PRE_MATCH"),  # Vendredi: activation, léger
    (5, "MATCH"),      # Samedi : match (pic absolu)
    # Dimanche : repos
]

# Indice de charge relative par type (1.0 = référence match).
CHARGE_RELATIVE = {
    "MATCH": 1.00,
    "INTENSIF": 0.82,
    "TECHNIQUE": 0.55,
    "PRE_MATCH": 0.35,
    "REPRISE": 0.30,
}

# Plages de distance totale (m) "de référence" par type (avant modulation poste).
DIST_REFERENCE = {
    "MATCH":     (9000, 11000),
    "INTENSIF":  (7000, 9000),
    "TECHNIQUE": (4800, 6500),
    "PRE_MATCH": (3200, 4500),
    "REPRISE":   (3200, 5000),
}

DUREE_MIN = {
    "MATCH":     (88, 96),
    "INTENSIF":  (72, 85),
    "TECHNIQUE": (60, 75),
    "PRE_MATCH": (45, 55),
    "REPRISE":   (45, 60),
}

# Modulateurs par famille de poste (multiplie distance totale et haute intensité).
# Ailiers/latéraux couvrent + de haute intensité ; gardiens très peu ; axes moins.
MOD_DISTANCE = {
    "gardien": 0.45, "axe": 0.92, "lateral": 1.08,
    "milieu": 1.10, "ailier": 1.05, "attaquant": 0.98,
}
MOD_HAUTE_INTENSITE = {
    "gardien": 0.20, "axe": 0.80, "lateral": 1.20,
    "milieu": 1.00, "ailier": 1.35, "attaquant": 1.15,
}

# Type RPE attendu par le backend (RpeRequest.seanceType = PHYSIQUE | TECHNIQUE).
RPE_TYPE = {
    "MATCH": "PHYSIQUE", "INTENSIF": "PHYSIQUE", "PRE_MATCH": "PHYSIQUE",
    "REPRISE": "PHYSIQUE", "TECHNIQUE": "TECHNIQUE",
}

# ─────────────────────────── Blessures (réalisme) ───────────────────────────
# Vocabulaire imposé par la base (contraintes V1/V11) :
#   type_blessure  ∈ musculaire | articulaire | osseux | tendineux | ligamentaire | autre
#   zone_corporelle∈ ischio_jambiers | quadriceps | mollet | cheville | genou | hanche
#                    | dos | epaule | adducteurs | autre
#   gravite        ∈ leger | modere | grave
# (type_db, zone_db, libelle_humain, jours_min, jours_max, gravite_db)
BLESSURES_TYPES = [
    ("musculaire", "ischio_jambiers", "Lésion ischio-jambiers", 14, 35, "modere"),
    ("musculaire", "quadriceps", "Élongation quadriceps", 10, 24, "leger"),
    ("musculaire", "mollet", "Lésion mollet", 12, 28, "modere"),
    ("ligamentaire", "cheville", "Entorse de cheville", 7, 28, "modere"),
    ("musculaire", "adducteurs", "Pubalgie / adducteurs", 21, 56, "modere"),
    ("autre", "autre", "Contusion / choc", 3, 10, "leger"),
    ("tendineux", "genou", "Tendinopathie genou", 14, 35, "modere"),
    ("ligamentaire", "genou", "Entorse du genou", 28, 90, "grave"),
]

# Étapes de retour au jeu (RTP) génériques — proportions de la durée d'indispo.
RTP_ETAPES = ["Repos / soins", "Réathlétisation", "Reprise course", "Reprise collective", "Disponible"]

# ─────────────────────────── Banque de noms (FR) ───────────────────────────
NOMS = [
    "Dupont", "Martin", "Bernard", "Leroy", "Moreau", "Petit", "Girard", "Roux",
    "Faure", "Blanc", "Simon", "Laurent", "Michel", "Rousseau", "Fontaine",
    "Chevalier", "Garnier", "Mercier", "Renard", "Lefebvre", "Bonnet", "Morin",
    "Leclerc", "Aubry", "Lemoine", "Colin", "Gauthier", "Perrin", "Robin", "Clement",
]
PRENOMS = [
    "Lucas", "Théo", "Antoine", "Maxime", "Thomas", "Julien", "Nicolas", "Clément",
    "Kévin", "Mathieu", "Rémi", "Paul", "Alexandre", "Baptiste", "Arthur", "Hugo",
    "Enzo", "Dylan", "Yannis", "Samir", "Karim", "Sofiane", "Yanis", "Mehdi",
    "Adrien", "Quentin", "Florian", "Romain", "Nathan", "Téo",
]
