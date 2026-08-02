"""
Dérives lentes de l'effectif sur ~4 semaines, en trois axes séparés.

Volume (distance totale), intensité (PART du volume au-dessus de 19 km/h) et ressenti
(fatigue subjective composite) : 14 derniers jours contre les 14 précédents.

L'axe intensité est exprimé en PART du volume et non en valeur absolue, sinon toute
hausse de volume produisait mécaniquement une « dérive d'intensité ». Les garde-fous sur
les petits volumes de référence sont ce qui a mis fin aux dérives à +1942 %.
"""
from fastapi import HTTPException
from uuid import UUID
from datetime import date as _date, timedelta as _timedelta

from app.core.database import get_connection
from app.core.config import _load_config
from app.core.scope import _equipes_scope
from app.domain.temps import _parse_date_simulee
from app.domain.effectif import _joueurs_resume


def derives(x_contexte_equipes: str | None = None,
            x_contexte_club: str | None = None,
            x_date_simulee: str | None = None):
    """
    Dérives lentes de l'effectif sur ~4 semaines, en TROIS axes séparés (pour une lecture globale
    de chacun) : volume (distance totale), intensité (PART du volume courue au-dessus de 19 km/h)
    et ressenti (fatigue subjective composite). Par axe et par joueur : comparaison des 14 derniers
    jours vs les 14 précédents → dérive en hausse / en baisse au-delà d'un seuil. Indicateurs déjà
    agrégés (jamais de données brutes au LLM), consommés par la carte web/PWA et le debrief textuel.

    ⚠ Ce n'est PAS un ratio de charge (cf. ACWR) et les deux ne se remplacent pas : l'ACWR compare
    un joueur à SON passé proche pour dire « trop / pas assez », avec des seuils physiologiques ;
    la dérive dit dans quel SENS l'effectif se déplace, sans seuil de danger. L'ACWR est aveugle à
    une montée lente (la charge chronique suit l'aiguë, le ratio ne bouge pas), la dérive est
    aveugle à un pic isolé (dilué dans une fenêtre de 14 jours).

    L'axe intensité raisonne en PART du volume : en mètres bruts il ne faisait que répéter l'axe
    volume (quand le volume monte, les mètres à haute intensité montent mécaniquement avec lui).
    En part, il répond à une question distincte — « le contenu devient-il plus intense ? ».

    GARDE-FOUS de quantité de données : un joueur n'est comparé que s'il a assez de séances DANS
    CHAQUE fenêtre et une référence au-dessus d'un plancher absolu. Sans eux, un retour de blessure
    (1 séance de reprise en référence, ~120 m à haute intensité) produisait des dérives à +1900 %
    qui ne disaient rien d'autre qu'un dénominateur minuscule. Les joueurs écartés sont COMPTÉS
    (`nb_ecartes`), jamais masqués en silence — le reste du moteur signale déjà ses estimations
    provisoires, ce bloc était le seul sans aucun garde-fou.
    """
    try:
        with get_connection() as conn:
            cfg = _load_config(conn)
            seuil        = float(cfg.get("derive_seuil_pct", 20.0))
            min_seances  = int(cfg.get("derive_min_seances", 3))
            min_jours    = int(cfg.get("derive_min_jours_ressenti", 3))
            plancher_vol = float(cfg.get("derive_plancher_volume_m", 3000.0))
            plancher_hi  = float(cfg.get("derive_plancher_hi_m", 300.0))

            # Fenêtres bornées par la date de RÉFÉRENCE (date simulée honorée, comme le risque) :
            # ]debut, milieu[ = référence, [milieu, date_ref] = récent.
            date_ref = _parse_date_simulee(x_date_simulee) or _date.today()
            debut    = date_ref - _timedelta(days=28)
            milieu   = date_ref - _timedelta(days=14)

            scope = _equipes_scope(x_contexte_equipes, x_contexte_club, conn)
            roster = _joueurs_resume(conn, scope)
            noms = {str(jid): f'{(prenom or "").strip()} {(nom or "").strip()}'.strip()
                    for (jid, nom, prenom, poste) in roster}
            ids = list(noms.keys())

            gps = {}   # jid -> (vol_recent, vol_ref, hi_recent, hi_ref, n_recent, n_ref)
            well = {}  # jid -> (w_recent, w_ref, n_recent, n_ref)  (composite, haut = + de fatigue)
            if ids:
                gwhere = ["s.statut = 'REALISEE'", "s.date >= %s", "s.date <= %s",
                          "dg.joueur_id = ANY(%s)"]
                gparams: list = [debut, date_ref, ids]
                if scope:
                    gwhere.append("s.equipe_id = ANY(%s)"); gparams.append(scope)
                with conn.cursor() as cur:
                    # Les deux COUNT(DISTINCT …) sont le garde-fou : sans le nombre de séances de
                    # chaque fenêtre, impossible de distinguer une vraie dérive d'un effectif de
                    # comparaison ridicule.
                    cur.execute(f"""
                        SELECT dg.joueur_id,
                          SUM(CASE WHEN s.date >= %s THEN dg.distance_totale_m ELSE 0 END),
                          SUM(CASE WHEN s.date <  %s THEN dg.distance_totale_m ELSE 0 END),
                          SUM(CASE WHEN s.date >= %s THEN COALESCE(dg.distance_19kmh_m,0) ELSE 0 END),
                          SUM(CASE WHEN s.date <  %s THEN COALESCE(dg.distance_19kmh_m,0) ELSE 0 END),
                          COUNT(DISTINCT CASE WHEN s.date >= %s THEN s.id END),
                          COUNT(DISTINCT CASE WHEN s.date <  %s THEN s.id END)
                        FROM donnee_gps dg JOIN seance s ON s.id = dg.seance_id
                        WHERE {' AND '.join(gwhere)}
                        GROUP BY dg.joueur_id
                    """, [milieu] * 6 + gparams)
                    for jid, vr, vf, hr, hf, nr, nf in cur.fetchall():
                        gps[str(jid)] = (float(vr or 0), float(vf or 0),
                                         float(hr or 0), float(hf or 0),
                                         int(nr or 0), int(nf or 0))
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT joueur_id,
                          AVG(CASE WHEN date >= %s THEN comp END),
                          AVG(CASE WHEN date <  %s THEN comp END),
                          COUNT(CASE WHEN date >= %s THEN 1 END),
                          COUNT(CASE WHEN date <  %s THEN 1 END)
                        FROM (
                          SELECT joueur_id, date,
                                 ((11-sommeil)+(11-humeur)+(11-fatigue)+(11-douleur)+(11-stress))/5.0*10 AS comp
                          FROM wellness_quotidien
                          WHERE date >= %s AND date <= %s AND joueur_id = ANY(%s)
                        ) w
                        GROUP BY joueur_id
                    """, (milieu, milieu, milieu, milieu, debut, date_ref, ids))
                    for jid, wr, wf, nr, nf in cur.fetchall():
                        well[str(jid)] = (float(wr) if wr is not None else None,
                                          float(wf) if wf is not None else None,
                                          int(nr or 0), int(nf or 0))

        def _drift(recent, ref, n_recent, n_ref, min_n, plancher):
            """
            (direction, pct, recent, ref) si la comparaison est FIABLE, sinon None.
            None = « données insuffisantes » : pas assez de séances (ou de jours) dans une des deux
            fenêtres, ou référence sous le plancher absolu. C'est volontairement distinct de
            « stable » — l'appelant compte les deux séparément.
            """
            if recent is None or ref is None or ref <= 0:
                return None
            if n_recent < min_n or n_ref < min_n:
                return None
            if plancher is not None and ref < plancher:
                return None
            pct = round((recent - ref) / ref * 100, 1)
            if pct >= seuil:  return ("hausse", pct, recent, ref)
            if pct <= -seuil: return ("baisse", pct, recent, ref)
            return ("stable", pct, recent, ref)

        def _axe(code, libelle, sens_hausse, unite, valeur):
            hausse, baisse, ecartes = [], [], 0
            for jid in ids:
                d = _drift(*valeur(jid))
                if d is None:
                    ecartes += 1          # écarté faute de données — surtout pas « stable »
                    continue
                if d[0] == "stable":
                    continue
                # valeur_recente / valeur_reference : de quoi montrer la composition dans la carte
                # (« 6,1 % → 7,9 % »), un pourcentage seul n'est pas interprétable.
                ligne = {"joueur_id": jid, "nom": noms.get(jid, "joueur"), "drift_pct": d[1],
                         "valeur_recente": round(d[2], 1), "valeur_reference": round(d[3], 1)}
                (hausse if d[0] == "hausse" else baisse).append(ligne)
            hausse.sort(key=lambda x: x["drift_pct"], reverse=True)
            baisse.sort(key=lambda x: x["drift_pct"])
            return {
                "code": code, "libelle": libelle, "sens_hausse": sens_hausse, "unite": unite,
                "nb_hausse": len(hausse), "nb_baisse": len(baisse), "nb_ecartes": ecartes,
                "hausse": hausse[:5], "baisse": baisse[:5],
            }

        def _g(jid):
            return gps.get(jid, (0.0, 0.0, 0.0, 0.0, 0, 0))

        def _volume(jid):
            v_r, v_f, _h_r, _h_f, n_r, n_f = _g(jid)
            return (v_r / 1000, v_f / 1000, n_r, n_f, min_seances, plancher_vol / 1000)

        def _part_hi(jid):
            """
            Part du volume courue au-dessus de 19 km/h (%). Insensible au nombre de séances : un
            calendrier plus dense ne la fait pas bouger, seul le CONTENU la fait bouger. Sans
            volume ni mètres de haute intensité de référence suffisants, la part n'a pas de sens.
            """
            v_r, v_f, h_r, h_f, n_r, n_f = _g(jid)
            if v_r <= 0 or v_f < plancher_vol or h_f < plancher_hi:
                return (None, None, n_r, n_f, min_seances, None)
            return (h_r / v_r * 100, h_f / v_f * 100, n_r, n_f, min_seances, None)

        def _ressenti(jid):
            w_r, w_f, n_r, n_f = well.get(jid, (None, None, 0, 0))
            # Échelle bornée 0-100 : pas de plancher à poser, mais une moyenne sur 1 jour reste
            # du bruit — d'où un minimum de JOURS de saisie dans chaque fenêtre.
            return (w_r, w_f, n_r, n_f, min_jours, None)

        axes = [
            _axe("volume", "Volume (distance totale)", "charge en hausse", "km", _volume),
            _axe("intensite", "Intensité (part du volume ≥ 19 km/h)",
                 "contenu plus intense à volume égal", "%", _part_hi),
            _axe("wellness", "Ressenti (fatigue subjective)", "fatigue en hausse", "/100", _ressenti),
        ]
        return {
            "fenetre_jours": 28,
            "seuil_pct": seuil,
            "min_seances": min_seances,
            "date_reference": str(date_ref),
            "effectif": {"nb_joueurs": len(ids)},
            "axes": axes,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
