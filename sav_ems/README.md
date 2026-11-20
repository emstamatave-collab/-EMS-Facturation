# EMS SAV Tamatave — application indépendante

Version pilote créée pour Europe Manutention Service Tamatave. Basée sur les flux observés dans MMS SAV08, sans copier ses données clients ni ses machines.

## Fonctionnalités
- Tableau de bord : machines disponibles, interventions ouvertes, à facturer, tâches, VGP arrivées à échéance, locations à terminer.
- Machines : stock Vente/Location/Client, recherche, caractéristiques, prix en ariary, dates VGP, détail et modification.
- Interventions : planification, attribution à un technicien, statuts jusqu'à « À facturer ».
- Clients, tâches, locations, offres : ajout, consultation et modification.
- Comptes utilisateurs distincts, administrateur et technicien, session HTTPS et mots de passe hachés.
- Icône et affichage adaptés à l'écran d'accueil iPhone/Android.

## Déploiement Render
Blueprint : `render-ems-sav.yaml` à la racine de la branche `ems-sav-independant`.
La base PostgreSQL `ems-sav-db` est séparée de MMS et de l'application de facturation EMS existante. Les données ne sont jamais migrées depuis MMS.

Au premier lancement, ouvrir `/setup` puis saisir le code d'activation `SETUP_KEY` (enregistré dans l'environnement Render) pour créer l'administrateur. Le formulaire d'activation se ferme après le premier utilisateur. Ensuite, l'administrateur peut créer des comptes depuis « Collaborateurs ».

## Important avant usage réel
- La base Render **free** utilisée pour ce pilote expire **le 20 décembre 2026** : il faut la passer sur une base durable et sauvegardée avant de stocker des informations professionnelles réelles.
- Version pilote : pas de synchronisation avec la facturation EMS existante, pas de photos téléversées, ni de génération de factures, ni de signature sur interventions.
- Configurer sauvegardes, rôles affinés, traces d'audit, restauration, politiques de données et validation métier avant usage en production.
- Ne publier aucun secret dans ce dépôt GitHub public. `SECRET_KEY`, `SETUP_KEY` et `DATABASE_URL` sont fournis par les variables d'environnement Render.
