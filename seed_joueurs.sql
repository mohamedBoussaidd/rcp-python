-- Active: 1732031777277@@localhost@5433@remi_preparateur
-- ============================================================
-- Données de test — 24 joueurs (effectif complet)
-- Composition : 2 GK · 4 DC · 2 LD · 2 LG · 2 MD · 3 MC
--               2 MO · 2 AD · 2 AG · 2 AC · 1 ATT
-- ============================================================

INSERT INTO joueur (
    nom, prenom,
    date_naissance, sexe,
    poids_actuel, poids_forme_cible, taille,
    pied_fort,
    poste_principal, poste_secondaire,
    profil_athletique,
    statut,
    date_arrivee_club
) VALUES

-- ── Gardiens ──────────────────────────────────────────────
('Dupont',    'Lucas',     '1995-03-15', 'M', 85.00, 83.00, 190.00, 'droit',     'gardien',           NULL,                'central_costaud',       'actif',    '2021-07-01'),
('Martin',    'Théo',      '1999-07-22', 'M', 82.00, 80.00, 188.00, 'gauche',    'gardien',           NULL,                'central_rapide',        'actif',    '2023-01-15'),

-- ── Défenseurs centraux ───────────────────────────────────
('Bernard',   'Antoine',   '1994-11-08', 'M', 83.00, 81.00, 187.00, 'droit',     'defenseur_central', 'milieu_defensif',   'central_costaud',       'actif',    '2020-07-01'),
('Leroy',     'Maxime',    '1996-04-19', 'M', 78.00, 76.00, 183.00, 'gauche',    'defenseur_central', 'lateral_gauche',    'central_rapide',        'actif',    '2022-07-01'),
('Moreau',    'Thomas',    '1993-09-02', 'M', 86.00, 84.00, 190.00, 'droit',     'defenseur_central', 'milieu_defensif',   'central_costaud',       'actif',    '2019-07-01'),
('Petit',     'Julien',    '2000-01-30', 'M', 76.00, 74.00, 181.00, 'droit',     'defenseur_central', 'milieu_defensif',   'sentinelle',            'actif',    '2023-07-01'),

-- ── Latéraux droits ───────────────────────────────────────
('Girard',    'Nicolas',   '1997-06-14', 'M', 74.00, 72.00, 178.00, 'droit',     'lateral_droit',     'ailier_droit',      'lateral_offensif',      'actif',    '2021-07-01'),
('Roux',      'Clément',   '2001-12-05', 'M', 72.00, 70.00, 176.00, 'droit',     'lateral_droit',     'milieu_central',    'explosif_leger',        'actif',    '2023-07-01'),

-- ── Latéraux gauches ──────────────────────────────────────
('Faure',     'Kévin',     '1995-08-21', 'M', 74.00, 72.00, 177.00, 'gauche',    'lateral_gauche',    'ailier_gauche',     'lateral_offensif',      'actif',    '2020-07-01'),
('Blanc',     'Mathieu',   '1998-03-17', 'M', 75.00, 73.00, 179.00, 'gauche',    'lateral_gauche',    'milieu_central',    'lateral_offensif',      'blesse',   '2022-07-01'),

-- ── Milieux défensifs ─────────────────────────────────────
('Simon',     'Rémi',      '1993-05-28', 'M', 79.00, 77.00, 182.00, 'droit',     'milieu_defensif',   'defenseur_central', 'sentinelle',            'actif',    '2018-07-01'),
('Laurent',   'Paul',      '1996-10-11', 'M', 77.00, 75.00, 180.00, 'droit',     'milieu_defensif',   'milieu_central',    'box_to_box',            'actif',    '2021-07-01'),

-- ── Milieux centraux ──────────────────────────────────────
('Michel',    'Alexandre', '1997-02-03', 'M', 74.00, 72.00, 180.00, 'droit',     'milieu_central',    'milieu_offensif',   'box_to_box',            'actif',    '2021-07-01'),
('Rousseau',  'Baptiste',  '1999-07-19', 'M', 72.00, 70.00, 177.00, 'gauche',    'milieu_central',    'ailier_gauche',     'box_to_box',            'actif',    '2022-07-01'),
('Fontaine',  'Arthur',    '1995-11-25', 'M', 75.00, 73.00, 179.00, 'droit',     'milieu_central',    'milieu_defensif',   'sentinelle',            'suspendu', '2020-07-01'),

-- ── Milieux offensifs ─────────────────────────────────────
('Chevalier', 'Hugo',      '1998-04-08', 'M', 71.00, 69.00, 175.00, 'gauche',    'milieu_offensif',   'ailier_gauche',     'explosif_leger',        'actif',    '2022-07-01'),
('Garnier',   'Enzo',      '2001-08-14', 'M', 70.00, 68.00, 174.00, 'droit',     'milieu_offensif',   'ailier_droit',      'explosif_leger',        'actif',    '2023-07-01'),

-- ── Ailiers droits ────────────────────────────────────────
('Mercier',   'Dylan',     '1996-01-22', 'M', 70.00, 68.00, 173.00, 'droit',     'ailier_droit',      'avant_centre',      'explosif_leger',        'actif',    '2021-07-01'),
('Renard',    'Yannis',    '2000-05-30', 'M', 73.00, 71.00, 177.00, 'droit',     'ailier_droit',      'milieu_offensif',   'lateral_offensif',      'actif',    '2023-01-01'),

-- ── Ailiers gauches ───────────────────────────────────────
('Lefebvre',  'Samir',     '1997-09-17', 'M', 71.00, 69.00, 174.00, 'gauche',    'ailier_gauche',     'milieu_offensif',   'explosif_leger',        'actif',    '2021-07-01'),
('Bonnet',    'Karim',     '2000-02-11', 'M', 69.00, 67.00, 172.00, 'gauche',    'ailier_gauche',     'attaquant',         'lateral_offensif',      'actif',    '2022-07-01'),

-- ── Avant-centres ─────────────────────────────────────────
('Morin',     'Sofiane',   '1994-07-04', 'M', 80.00, 78.00, 184.00, 'droit',     'avant_centre',      'attaquant',         'renard_surfaces',       'actif',    '2019-07-01'),
('Leclerc',   'Yanis',     '1997-12-28', 'M', 77.00, 75.00, 181.00, 'gauche',    'avant_centre',      'milieu_offensif',   'attaquant_profondeur',  'actif',    '2022-07-01'),

-- ── Attaquant polyvalent ──────────────────────────────────
('Aubry','Mehdi','1998-06-09','M',75.00, 73.00, 178.00,'ambidextre','attaquant','avant_centre','renard_surfaces','prete',    '2021-07-01');
