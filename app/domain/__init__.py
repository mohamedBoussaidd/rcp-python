"""
Logique métier des indicateurs de préparation physique.

Sens des dépendances, à ne jamais inverser :

    api/routes  →  domain  →  core

et à l'intérieur de `domain` :

    temps, contexte  →  charge  →  risque, fatigue, objectif, athletique
                                →  rapport_seance, derives, equipe

Un import circulaire n'est pas un obstacle à contourner : c'est le signal qu'une
fonction est rangée au mauvais endroit.
"""
