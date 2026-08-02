"""
Charge d'entraînement et ratio aigu/chronique (ACWR).

Trois sources coexistent et ne doivent JAMAIS être confondues : la charge mesurée
(GPS, en km), la charge ressentie (sRPE) et la charge unifiée qui arbitre entre les
deux. Chacune calcule sa propre fenêtre chronique, alignée sur `acwr_semaines_chronique`.

⚠ Le DIVISEUR est ADAPTATIF : `min(cap, max(1, semaines réellement présentes))`. Un
diviseur figé (le `/3` en dur d'avant la V90) gonflait mécaniquement la surcharge d'un
club à historique court. Les trois calculs de charge hebdomadaire de référence doivent
donc rester cohérents entre eux — c'est précisément l'oubli qui avait produit le bug.

⚠ `_charge_gps` et `_charge_rpe` renvoient un 3-TUPLE dont le 3ᵉ élément est le nombre
de semaines réelles. Leurs appelants indexent [0]/[1] sans jamais dépaqueter : ne pas
« simplifier » cette signature.
"""
from uuid import UUID
from datetime import date as _date

from app.core.config import _poids_seance
from app.domain.temps import _lundi


def _charge_gps(joueur_id: UUID, cfg: dict, conn, date_ref=None) -> tuple | None:
    """
    Charge externe (GPS) « découplée » — fenêtres NON chevauchantes :
      - aiguë           = SUM distances 7 derniers jours (mètres)
      - chronique hebdo = SUM distances jours 8-35 ÷ semaines RÉELLEMENT présentes (plafonné à
        `acwr_semaines_chronique`) — diviseur ADAPTATIF. Sans lui, un club à historique court
        (< 4 sem.) voit sa chronique divisée par 4 alors qu'il n'a qu'~1 semaine de données →
        ACWR faussement gonflé (~4) → risque sur-alarmé. Au régime établi (≥ cap sem.) le
        diviseur vaut cap : comportement identique à avant.
    `date_ref` permet de calculer à une date passée (tendance). Défaut = aujourd'hui.
    Renvoie (aigue_m, chronique_hebdo_m, semaines_reelles) ou None si pas de base chronique.
    Le 3e élément sert à signaler une baseline courte au front (« estimation provisoire ») ;
    les appelants qui n'en ont pas besoin indexent simplement [0] et [1].
    """
    ref = date_ref or _date.today()
    sem_chronique = int(cfg.get("acwr_semaines_chronique", 4))
    jours_chronique = 7 + sem_chronique * 7   # 35 jours pour 4 semaines
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT
                    SUM(CASE WHEN s.date >= %s::date - INTERVAL '7 days'
                             THEN dg.distance_totale_m ELSE 0 END) AS charge_aigue,
                    SUM(CASE WHEN s.date >= %s::date - INTERVAL '{jours_chronique} days'
                             AND s.date  < %s::date - INTERVAL '7 days'
                             THEN dg.distance_totale_m ELSE 0 END) AS chronique_somme,
                    COUNT(DISTINCT date_trunc('week', s.date))
                        FILTER (WHERE s.date >= %s::date - INTERVAL '{jours_chronique} days'
                                  AND s.date  < %s::date - INTERVAL '7 days') AS chronique_semaines
                FROM donnee_gps dg
                JOIN seance s ON dg.seance_id = s.id
                WHERE dg.joueur_id = %s
                  AND s.date >= %s::date - INTERVAL '{jours_chronique} days'
                  AND s.date <= %s::date
            """, (ref, ref, ref, ref, ref, str(joueur_id), ref, ref))
            row = cur.fetchone()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return None

    if not row or row[1] is None or float(row[1]) == 0 or not row[2]:
        return None
    diviseur = min(sem_chronique, max(1, int(row[2])))
    return (float(row[0] or 0), float(row[1]) / diviseur, diviseur)

def _charge_rpe(joueur_id: UUID, conn, date_ref=None, cfg: dict | None = None) -> tuple | None:
    """
    Charge interne (sRPE = RPE × durée, saisie joueur) « découplée » :
      - aiguë           = SUM charges 7 derniers jours
      - chronique hebdo = SUM charges de la fenêtre chronique ÷ semaines RÉELLEMENT présentes
        — diviseur ADAPTATIF, même raison que `_charge_gps` (pas de sous-estimation).
    Sert de source de repli quand le GPS manque (séances techniques, sans gilets).

    La fenêtre est ALIGNÉE sur celle du GPS (`acwr_semaines_chronique`, défaut 4) : elle était
    figée à 3 semaines, si bien qu'`acwr_gps` et `acwr_rpe` ne reposaient pas sur la même
    longueur de référence — une partie de leur écart n'était qu'un artefact de fenêtre, ce qui
    rendait leur comparaison côte à côte trompeuse.

    Renvoie (aigue, chronique_hebdo, semaines_reelles) ou None si pas de base chronique /
    table absente.
    """
    ref = date_ref or _date.today()
    sem_chronique = int((cfg or {}).get("acwr_semaines_chronique", 4))
    jours_chronique = 7 + sem_chronique * 7
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT
                    SUM(CASE WHEN date >= %s::date - INTERVAL '7 days'
                             THEN charge ELSE 0 END) AS aigue,
                    SUM(CASE WHEN date >= %s::date - INTERVAL '{jours_chronique} days'
                             AND date  < %s::date - INTERVAL '7 days'
                             THEN charge ELSE 0 END) AS chronique_somme,
                    COUNT(DISTINCT date_trunc('week', date))
                        FILTER (WHERE date >= %s::date - INTERVAL '{jours_chronique} days'
                                  AND date  < %s::date - INTERVAL '7 days') AS chronique_semaines
                FROM rpe_seance
                WHERE joueur_id = %s
                  AND date >= %s::date - INTERVAL '{jours_chronique} days'
                  AND date <= %s::date
                  AND charge IS NOT NULL
            """, (ref, ref, ref, ref, ref, str(joueur_id), ref, ref))
            row = cur.fetchone()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return None

    if not row or row[1] is None or float(row[1]) == 0 or not row[2]:
        return None
    diviseur = min(sem_chronique, max(1, int(row[2])))
    return (float(row[0] or 0), float(row[1]) / diviseur, diviseur)

def _charge_acwr_unifiee(joueur_id: UUID, cfg: dict, conn, date_ref=None) -> dict:
    """
    Source de charge UNIFIÉE avec repli (fallback) :
      - GPS présent seul          → ACWR sur les km (charge externe)
      - RPE présent seul           → ACWR sur la charge ressentie (repli)
      - les deux présents          → ACWR combiné pondéré (MIXTE)
      - aucune donnée              → source None

    GPS (externe) et RPE (interne) ne mesurent pas la même chose : on les combine
    via leurs ratios ACWR (sans dimension), pondérés (clés cfg `poids_charge_gps`
    / `poids_charge_rpe`). Les charges aiguë/chronique renvoyées sont en km si le
    GPS est disponible (source GPS ou MIXTE), sinon en unités sRPE.

    Renvoie : {source, acwr, aigue, chronique, unite, acwr_gps, acwr_rpe,
    semaines_gps, semaines_rpe} — les deux ratios isolés et le nombre de semaines de
    référence réellement utilisées de chaque côté sont RAPPORTÉS (et non plus jetés) :
    c'est l'écart entre GPS et ressenti qui informe, pas la seule moyenne pondérée qui
    l'annule. Cf. `_ecart_sources()`.
    """
    gps = _charge_gps(joueur_id, cfg, conn, date_ref)
    rpe = _charge_rpe(joueur_id, conn, date_ref, cfg)

    acwr_gps = (gps[0] / gps[1]) if gps and gps[1] > 0 else None
    acwr_rpe = (rpe[0] / rpe[1]) if rpe and rpe[1] > 0 else None
    sem_gps  = gps[2] if gps and len(gps) > 2 else None
    sem_rpe  = rpe[2] if rpe and len(rpe) > 2 else None

    vide = {"source": None, "acwr": None, "aigue": None, "chronique": None,
            "unite": None, "acwr_gps": None, "acwr_rpe": None,
            "semaines_gps": None, "semaines_rpe": None}

    if acwr_gps is not None and acwr_rpe is not None:
        w_g = float(cfg.get("poids_charge_gps", 0.6))
        w_r = float(cfg.get("poids_charge_rpe", 0.4))
        acwr = (w_g * acwr_gps + w_r * acwr_rpe) / (w_g + w_r)
        return {"source": "MIXTE", "acwr": round(acwr, 2),
                "aigue": round(gps[0] / 1000, 1), "chronique": round(gps[1] / 1000, 1),
                "unite": "km", "acwr_gps": round(acwr_gps, 2), "acwr_rpe": round(acwr_rpe, 2),
                "semaines_gps": sem_gps, "semaines_rpe": sem_rpe}
    if acwr_gps is not None:
        return {"source": "GPS", "acwr": round(acwr_gps, 2),
                "aigue": round(gps[0] / 1000, 1), "chronique": round(gps[1] / 1000, 1),
                "unite": "km", "acwr_gps": round(acwr_gps, 2), "acwr_rpe": None,
                "semaines_gps": sem_gps, "semaines_rpe": None}
    if acwr_rpe is not None:
        return {"source": "RPE", "acwr": round(acwr_rpe, 2),
                "aigue": round(rpe[0], 0), "chronique": round(rpe[1], 0),
                "unite": "sRPE", "acwr_gps": None, "acwr_rpe": round(acwr_rpe, 2),
                "semaines_gps": None, "semaines_rpe": sem_rpe}
    return vide

def _ecart_sources(charge: dict, cfg: dict) -> dict | None:
    """
    Divergence entre charge EXTERNE (GPS) et charge RESSENTIE (sRPE), quand les deux
    existent. L'ACWR mixte est une moyenne pondérée : elle ANNULE justement le cas le plus
    utile (« il en fait autant mais le vit mal »). On rapporte donc l'écart signé et une
    lecture prête à afficher. Seuil configurable `seuil_ecart_sources` (défaut 0.30).

    Renvoie None si une seule source, sinon {ecart, sens, libelle}.
    """
    a_gps, a_rpe = charge.get("acwr_gps"), charge.get("acwr_rpe")
    if a_gps is None or a_rpe is None:
        return None

    seuil = float(cfg.get("seuil_ecart_sources", 0.30))
    ecart = round(a_rpe - a_gps, 2)
    if abs(ecart) < seuil:
        return {"ecart": ecart, "sens": "COHERENT",
                "libelle": "charge mesurée et ressenti concordants"}
    if ecart > 0:
        return {"ecart": ecart, "sens": "RESSENTI_SUP",
                "libelle": "charge mesurée stable mais ressenti en hausse — "
                           "fatigue extra-sportive possible (sommeil, maladie, vie perso)"}
    return {"ecart": ecart, "sens": "MESURE_SUP",
            "libelle": "charge mesurée en hausse mais peu ressentie — "
                       "bonne tolérance, ou sous-déclaration du RPE"}

def _moyenne_hebdo_gps(joueur_id: UUID, conn, lundi, cap: int) -> tuple | None:
    """
    Baseline STABLE de la charge cible : volume GPS des `cap` dernières semaines COMPLÈTES
    (fenêtre [lundi − cap·7 j ; lundi[ — inclut la semaine passée, exclut la semaine en cours)
    ET le nombre de semaines réellement présentes (diviseur adaptatif). Ancrée au lundi : figée
    toute la semaine, recalculée chaque lundi (pas de dérive en cours de semaine). Volontairement
    distincte de la fenêtre « chronique découplée » de l'ACWR risque, qui exclut les 7 derniers
    jours et raterait un historique court.
    Renvoie (somme_mètres, semaines_présentes) ou None si aucune donnée dans la fenêtre.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT SUM(dg.distance_totale_m),
                       COUNT(DISTINCT date_trunc('week', s.date))
                FROM donnee_gps dg
                JOIN seance s ON dg.seance_id = s.id
                WHERE dg.joueur_id = %s
                  AND s.date >= %s::date - INTERVAL '{int(cap) * 7} days'
                  AND s.date <  %s::date
            """, (str(joueur_id), lundi, lundi))
            row = cur.fetchone()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return None
    if not row or row[0] is None or float(row[0]) == 0 or not row[1]:
        return None
    return (float(row[0]), int(row[1]))

def _moyenne_hebdo_rpe(joueur_id: UUID, conn, lundi, cap: int) -> tuple | None:
    """Repli sRPE de `_moyenne_hebdo_gps` (même fenêtre calendaire) quand le GPS manque."""
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT SUM(charge),
                       COUNT(DISTINCT date_trunc('week', date))
                FROM rpe_seance
                WHERE joueur_id = %s
                  AND charge IS NOT NULL
                  AND date >= %s::date - INTERVAL '{int(cap) * 7} days'
                  AND date <  %s::date
            """, (str(joueur_id), lundi, lundi))
            row = cur.fetchone()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return None
    if not row or row[0] is None or float(row[0]) == 0 or not row[1]:
        return None
    return (float(row[0]), int(row[1]))

def _baseline_ratio(joueur_id: UUID, type_seance_id, conn, recence_j: int, date_ref=None) -> tuple:
    """
    Baseline personnelle en m/min : moyenne des 10 dernières séances RÉALISÉES du joueur, dans la
    fenêtre de récence. `type_seance_id=None` → toutes séances confondues (repli affichable quand
    le type est trop mince). Renvoie (ratio, n) — même définition que la « distance attendue » du
    rapport de séance (sujet 4), extraite ici pour être réutilisable hors d'une séance existante.

    `date_ref` ancre la fenêtre de récence : sans elle, le rapport d'une séance consultée à une
    date simulée comparait la séance à une norme construite sur des séances POSTÉRIEURES.
    """
    ref         = date_ref or _date.today()
    filtre_type = "AND s.type_seance_id = %s" if type_seance_id else ""
    params: list = [str(joueur_id)]
    if type_seance_id:
        params.append(str(type_seance_id))
    params += [ref, ref]
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT AVG(sub.ratio), COUNT(*) FROM (
                SELECT dg.distance_totale_m / NULLIF(dg.duree_minutes, 0) AS ratio
                FROM donnee_gps dg
                JOIN seance s ON dg.seance_id = s.id
                WHERE dg.joueur_id = %s
                  {filtre_type}
                  AND s.statut = 'REALISEE'
                  AND s.date >= %s::date - INTERVAL '{recence_j} days'
                  AND s.date <= %s::date
                  AND dg.duree_minutes > 0
                  AND dg.distance_totale_m > 0
                  -- Séances où le joueur a été volontairement ménagé : exclues de sa norme
                  -- (même règle que la distance attendue du rapport de séance).
                  AND NOT EXISTS (
                      SELECT 1 FROM presence pr
                      WHERE pr.seance_id = s.id
                        AND pr.joueur_id = dg.joueur_id
                        AND pr.statut = 'ADAPTE'
                  )
                ORDER BY s.date DESC
                LIMIT 10
            ) sub
        """, params)
        r = cur.fetchone()
    return (float(r[0]) if r and r[0] is not None else None, int(r[1]) if r and r[1] else 0)
