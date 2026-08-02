"""
Helpers de date partagés par tous les calculs.

`_parse_date_simulee` lit l'en-tête X-Date-Simulee (« voyage dans la saison », réservé
super-admin côté Java) ; `_lundi` sert à compter les semaines réellement présentes dans
une fenêtre de référence — la brique du diviseur adaptatif de l'ACWR.
"""
from datetime import date as _date, timedelta as _timedelta


def _parse_date_simulee(valeur: str | None):
    """Parse l'en-tête X-Date-Simulee (yyyy-MM-dd) en date, ou None si absent/invalide.
    Outil de TEST : permet de se placer à une date arbitraire (préparation, trêve…)."""
    if not valeur:
        return None
    try:
        return _date.fromisoformat(valeur.strip()[:10])
    except Exception:
        return None


def _lundi(d):
    """
    Lundi de la semaine ISO de `d` — équivalent Python de `date_trunc('week', …)` en SQL.
    Sert à compter les semaines RÉELLEMENT présentes dans une fenêtre de référence côté Python,
    quand la requête ramène déjà les lignes (inutile de refaire un COUNT en base).
    Tolère un `datetime` comme une `date`.
    """
    if hasattr(d, "date"):
        d = d.date()
    return d - _timedelta(days=d.weekday())
