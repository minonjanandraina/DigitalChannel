# DigitalChannel

Application Django de rapprochement WTB MVOLA/PAMF. Les templates existants servent de référence visuelle; le flux d'authentification du socle est implémenté dans l'application `accounts`.

## Développement local

Dans PowerShell, à la racine du projet:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-service.txt
Copy-Item .env.example .env
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py runserver
```

Ouvrir `http://127.0.0.1:8000/`. Sans configuration SMTP, les emails de confirmation s'affichent dans la console du serveur. Créer le premier compte Admin avec `python manage.py createsuperuser`, puis lui attribuer le groupe `Admin` dans `/admin/`.

Ce poste utilise PostgreSQL pour le développement via `DATABASE_URL` dans le fichier local `.env`, qui est ignoré par Git. Sans `DATABASE_URL`, SQLite sert de solution de repli. Ne jamais placer les identifiants dans `.env.example` ou dans un fichier suivi. Les tables `interop_mvola_*` restent externes aux migrations Django; utiliser une base PostgreSQL de test isolée pour leur intégration.

Les comptes confirmés reçoivent le rôle `Backoffice` par défaut en développement. En production, laisser `DEFAULT_SIGNUP_ROLE` vide: l'adresse email sera confirmée, mais un Admin devra attribuer un rôle avant tout accès aux fonctions métier.

Les utilisateurs Backoffice et Admin peuvent importer un rapport depuis `/mvola/import/`. Le nom doit suivre `YYYY-MM-DD_reporting_PAMF.csv`; le CSV `;` doit contenir les 14 colonnes attendues. Les fichiers sont stockés dans `input/mvola/` par défaut, sans écrasement d'un fichier existant. `MVOLA_INPUT_DIR` permet de choisir un autre dossier et `MVOLA_MAX_UPLOAD_SIZE` de modifier la limite (25 Mio par défaut).

Le rapprochement s'exécute de façon synchrone dans la requête web. Le modal reste ouvert pendant le traitement et n'affiche le résultat qu'après le retour du service. En production, régler les délais d'attente du serveur web et du proxy selon la durée maximale du rapprochement.

Les utilisateurs disposant de `view_reconciliation_results` peuvent ouvrir `/mvola/reconciliations/`, filtrer par date, processus et statut, puis consulter les transactions MVOLA, CBS ou le rapprochement d'un processus. Ces écrans lisent les tables PostgreSQL `interop_mvola_*` sans les créer ni les modifier.

Pour lancer les tests:

```powershell
.\.venv\Scripts\python.exe manage.py test --settings=config.test_settings
```

Les tests utilisent SQLite en mémoire et mockent le service CBS; ils ne contactent ni CBS ni les tables `interop_mvola_*`. Pour exécuter un rapprochement réel dans un environnement de test, renseigner les variables `CBS_SQLSERVER_*` après obtention des identifiants via le canal sécurisé de l'équipe. Le service refuse de démarrer l'accès CBS si le serveur, l'utilisateur ou le mot de passe manquent.