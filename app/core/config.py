"""
Lecture des paramètres de calcul et tables de correspondance de référence.

`_load_config` est appelée une fois par requête par chaque endpoint : elle ramène les
~76 clés de la table `configuration`, sur lesquelles TOUS les calculs s'appuient via
`cfg.get(cle, defaut)`. Le défaut en dur reste la source de vérité quand la clé est
absente — c'est ce qui rend une migration de seed sans effet sur le comportement.
"""
from uuid import UUID  # noqa: F401  (conservé pour les annotations futures)

# Alias abréviations → clé canonique (insensible à la casse)
POSTE_ALIASES: dict[str, str] = {
    "g": "gardien", "gk": "gardien", "gd": "gardien", "goal": "gardien",
    "dc": "defenseur_central", "cb": "defenseur_central", "def": "defenseur_central",
    "lb": "lateral_gauche", "lg": "lateral_gauche",
    "rb": "lateral_droit", "ld": "lateral_droit",
    "md": "milieu_defensif", "mdc": "milieu_defensif",
    "cdm": "milieu_defensif", "dmc": "milieu_defensif", "mdeft": "milieu_defensif",
    "mc": "milieu_central", "cm": "milieu_central", "mf": "milieu_central",
    "mo": "milieu_offensif", "moff": "milieu_offensif", "cam": "milieu_offensif",
    "ag": "ailier_gauche", "aig": "ailier_gauche", "lw": "ailier_gauche",
    "ad": "ailier_droit", "aid": "ailier_droit", "rw": "ailier_droit",
    "att": "attaquant", "st": "attaquant", "fw": "attaquant",
    "ac": "avant_centre", "cf": "avant_centre", "9": "avant_centre",
}

# Correspondance code type → clé config pondération
POIDS_TYPE_KEY: dict[str, str] = {
    "MATCH":        "poids_match",
    "MATCH_AMICAL": "poids_match_amical",
    "INTENSIF":     "poids_intensif",
    "FORCE":        "poids_force",
    "TECHNIQUE":    "poids_technique",
    "PRE_MATCH":    "poids_pre_match",
    "REPRISE":      "poids_reprise",
}

# Correspondance poste → clé config objectif GPS
OBJECTIF_POSTE_KEY: dict[str, str] = {
    "gardien":            "objectif_gardien",
    "defenseur_central":  "objectif_defenseur_central",
    "lateral_droit":      "objectif_lateral_droit",
    "lateral_gauche":     "objectif_lateral_gauche",
    "milieu_defensif":    "objectif_milieu_defensif",
    "milieu_central":     "objectif_milieu_central",
    "milieu_offensif":    "objectif_milieu_offensif",
    "ailier_droit":       "objectif_ailier_droit",
    "ailier_gauche":      "objectif_ailier_gauche",
    "attaquant":          "objectif_attaquant",
    "avant_centre":       "objectif_avant_centre",
}

# Types de match (objectif GPS applicable)
TYPES_MATCH    = ("MATCH", "MATCH_AMICAL")
TYPES_INTENSIF = ("INTENSIF",)


def _normaliser_poste(poste: str) -> str:
    if not poste:
        return ""
    cle = poste.strip().lower()
    return POSTE_ALIASES.get(cle, cle)


def _load_config(conn) -> dict:
    """
    Charge les valeurs de configuration depuis la base.
    Si la table n'existe pas encore (migration non exécutée),
    retourne un dict vide — tous les cfg.get(key, défaut) utilisent
    alors leurs valeurs hardcodées, identiques à l'ancien comportement.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT cle, valeur FROM configuration")
            rows = cur.fetchall()
        return {row[0]: float(row[1]) for row in rows}
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return {}


def _poids_seance(type_code: str, cfg: dict) -> float:
    key = POIDS_TYPE_KEY.get(type_code, "")
    return cfg.get(key, 0.60) if key else 0.60


def _objectif_poste(poste: str, cfg: dict) -> float | None:
    key = OBJECTIF_POSTE_KEY.get(poste, "")
    return cfg.get(key) if key else None
