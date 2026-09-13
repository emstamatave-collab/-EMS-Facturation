# EMS Devis & Facturation — V4 Internet

Version prête à être hébergée sur Internet avec HTTPS via un hébergeur compatible Docker.

## Ajouts V4
- Connexion par identifiant et mot de passe.
- Session sécurisée compatible HTTPS.
- Base SQLite déplacée dans un dossier de données persistant (`EMS_DATA_DIR`).
- Bouton **Sauvegarde** pour télécharger une copie de `ems.db`.
- Déploiement Docker avec Gunicorn.
- Fichier `render.yaml` pour un déploiement simplifié sur Render avec disque persistant.
- Application PWA : peut être ajoutée à l'écran d'accueil de l'iPhone.

## Important avant mise en ligne
Définir obligatoirement :
- `EMS_ADMIN_USER`
- `EMS_ADMIN_PASSWORD`
- `EMS_SECRET_KEY`
- `EMS_HTTPS=1`

Ne pas conserver le mot de passe par défaut `change-me`.

## Lancer en local avec Docker
```bash
docker build -t ems-facturation .
docker run --rm -p 5000:5000 \
  -e EMS_ADMIN_USER=admin \
  -e EMS_ADMIN_PASSWORD='mot-de-passe-fort' \
  -e EMS_SECRET_KEY='cle-secrete-longue' \
  -e EMS_HTTPS=0 \
  -v ems-data:/data \
  ems-facturation
```
Puis ouvrir `http://localhost:5000`.

## Mise en ligne
Le dossier est prêt pour un hébergement Docker. Le serveur doit disposer d'un stockage persistant monté sur `/data`, sinon les factures, clients et règlements seront perdus lors d'un redéploiement.

Le fichier `render.yaml` est fourni comme exemple de configuration de déploiement.

## Sauvegarde
Une fois connecté, cliquer sur **Sauvegarde** pour télécharger le fichier `ems_backup.db`. Conserver régulièrement une copie hors du serveur.

## iPhone
Après mise en ligne avec HTTPS :
1. Ouvrir l'adresse du logiciel dans Safari.
2. Partager → **Sur l'écran d'accueil**.
3. L'icône EMS s'ouvre ensuite comme une application.

## Sécurité et comptabilité
Cette V4 est une base métier fonctionnelle, mais avant usage comptable officiel il faut faire valider par le comptable/conseil local les mentions obligatoires, règles fiscales, conservation, séquence de numérotation et exigences applicables à EMS à Madagascar.
