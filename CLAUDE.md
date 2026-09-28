# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## je t'interdi de toucher à cette fichier: service_mvola.py

## Commandes courantes

Toutes les commandes s'exécutent depuis la racine du projet, avec le venv activé (`.\.venv\Scripts\python.exe` sous PowerShell).

```powershell
# Installation (requirements-service.txt inclut requirements.txt + pandas/pyodbc/SQLAlchemy pour service_mvola.py)
.\.venv\Scripts\python.exe -m pip install -r requirements-service.txt

# Migrations et lancement du serveur de dev (SQLite par défaut, PostgreSQL si DATABASE_URL est défini dans .env)
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py runserver

# Créer le premier compte Admin (à compléter dans /admin/ en lui attribuant le groupe "Admin")
.\.venv\Scripts\python.exe manage.py createsuperuser

# Tests (SQLite en mémoire, CBS mocké, n'accède jamais aux tables interop_mvola_* ni à CBS)
.\.venv\Scripts\python.exe manage.py test --settings=config.test_settings

# Un seul module ou une seule classe/méthode de test
.\.venv\Scripts\python.exe manage.py test mvola.tests --settings=config.test_settings
.\.venv\Scripts\python.exe manage.py test mvola.tests.SomeTestCase.test_something --settings=config.test_settings
```

Sans configuration SMTP (`EMAIL_HOST` vide), les emails de confirmation s'affichent dans la console du serveur au lieu d'être envoyés.

## Objectif

Rapprocher les transactions WTB (Wallet to Bank) entre la PAMF (microfinance) et MVOLA (MNO), puis produire les données de rapprochement nécessaires pour identifier les transactions correspondantes et les écarts.

Le rapprochement batch actuel est dans `service_mvola.py`, fonction `reconciliation(file, userid)`. Les rapports locaux sont attendus dans `input/mvola/`.

L'objectif web est de permettre l'ajout des rapports MVOLA et leur stockage dans `input/mvola/`. Les fichiers de `templates/` (racine du projet) sont uniquement une référence de thème et de style: ne pas les réutiliser comme implémentation ni supposer que leurs routes, formulaires, permissions ou fonctionnalités sont raccordés au service. Les templates réellement raccordés vivent dans `accounts/templates/accounts/` et `mvola/templates/mvola/`.

## Architecture

Deux mondes de données coexistent et ne doivent pas être confondus:

- **ORM Django (SQLite/PostgreSQL selon l'environnement, migré par Django)**: `accounts` (utilisateurs, rôles via `django.contrib.auth.Group`/`Permission`) et `mvola.ReconciliationRun` / `mvola.OrphanMvolaProcessingHistory` (suivi applicatif des lancements et de la régularisation des écarts).
- **Tables PostgreSQL externes `interop_mvola_*`** (schéma déclaré dans `mvola/models.py` avec `Meta.managed = False`, jamais dans les migrations Django): `interop_mvola_process`, `interop_mvola_cbs`, `interop_mvola_mvola`, `interop_mvola_reconciliation`. Elles sont écrites uniquement par `service_mvola.reconciliation()` (via SQLAlchemy/pandas `to_sql`, pas l'ORM) et lues en lecture seule par `mvola/results_repository.py` (SQL brut via SQLAlchemy, `get_pg_engine()` partagé avec `service_mvola.py`). Ne jamais utiliser le Django ORM pour écrire dans ces tables.

Flux d'un rapprochement, de bout en bout:

1. `mvola/forms.py::MvolaReportUploadForm` valide le nom (`FILENAME_PATTERN`), l'encodage, les 14 colonnes attendues et au moins une ligne `Completed` avant tout enregistrement.
2. `mvola/storage.py::store_report()` écrit le fichier dans `MVOLA_INPUT_DIR` avec `O_CREAT | O_EXCL` (jamais d'écrasement, permissions 0600).
3. `mvola/views.py::launch_reconciliation` revalide le nom de fichier côté serveur (défense en profondeur contre les chemins), crée un `ReconciliationRun` Django (`queued` → `running`), puis appelle **directement et de façon synchrone** `service_mvola.reconciliation(filename, user_id)` — pas de file d'attente, pas de tâche async.
4. `service_mvola.reconciliation()` lit le CSV (`read_mvola_file`), interroge SQL Server/CBS (`get_mvola_data_cbs`, `get_trx_id_cbs` via pyodbc), fait un merge outer pandas pour dériver `reconciliation_status` (`matched`/`orphan_mvola`/`orphan_pamf`), puis insère process + données brutes + résultat dans une seule transaction PostgreSQL (`get_pg_engine().begin()`).
5. Le `ReconciliationRun` Django est marqué `succeeded`/`failed` seulement après le retour du service, avec le `process_id` externe s'il existe. `mvola/orphan_history.py::copy_orphan_processing_history()` copie ensuite les lignes orphelines vers `OrphanMvolaProcessingHistory` pour le suivi de régularisation (best-effort: une erreur ici n'invalide pas le rapprochement lui-même).
6. `mvola/results_repository.py` interroge les tables externes en SQL brut pour l'historique (`reconciliation_history`) et le détail par processus/source/statut (`reconciliation_detail`), avec pagination.

Rôles et permissions (Django `Group`/`Permission`, seedés par `accounts/roles.py::seed_roles`, appelé depuis un signal `post_migrate` — voir `accounts/signals.py`/`accounts/apps.py`): `Viewer` (lecture seule: `view_mvola_transactions`, `view_reconciliation_results`), `Backoffice` (+ `import_mvola_report`, `run_mvola_reconciliation`), `Admin` (toutes les capacités + gestion des utilisateurs/groupes Django). `accounts/signals.py` synchronise aussi `is_staff` avec l'appartenance au groupe `Admin`/`is_superuser` via `m2m_changed`. Les comptes créés par inscription libre restent `is_active=False` jusqu'à la confirmation du lien d'activation signé (`django.core.signing`, expiration via `EMAIL_CONFIRMATION_MAX_AGE`); `DEFAULT_SIGNUP_ROLE` attribue un groupe automatiquement à l'activation (utile en dev, laissé vide en prod pour forcer une attribution manuelle par un Admin).

## Flux MVOLA (service `service_mvola.py`)

- Le nom du fichier doit suivre `YYYY-MM-DD_reporting_PAMF.csv`; la date du nom détermine la date de recherche dans CBS et la date du processus.
- Le CSV utilise `;` comme séparateur. `read_mvola_file()` conserve seulement les lignes avec `STATE == Completed`.
- `get_mvola_data_cbs()` interroge SQL Server pour les transactions du marchand 13 à la date demandée: remboursements de prêts et dépôts de compte. Les montants sont agrégés par `apiLogId` et `PostingDate`.
- `get_trx_id_cbs()` cherche les identifiants de transaction CBS associés aux `apiLogId` (services 302 et 700). La jointure externe rapproche `TRANSID_MVOLA` avec `trx_id`.
- Les états produits sont `matched`, `orphan_mvola` et `orphan_pamf` (`reconciliation_status`).
- Si les données CBS sont vides, le service renvoie une erreur avant insertion. En cas de succès, il crée un processus avec `Status="completed"` puis insère les données brutes MVOLA, CBS et le résultat dans PostgreSQL dans une transaction.
- La fonction renvoie un dictionnaire avec `status` (`success` ou `error`) et `message`. Elle ne téléverse pas de fichier et ne le déplace pas: le fichier doit déjà être lisible sous `input/mvola/`.
- Chaque exécution réussie crée un nouveau processus; aucune protection contre le retraitement d'une même date n'est visible dans ce service. Ne pas promettre l'idempotence côté interface sans l'ajouter explicitement.

Pour l'upload web à implémenter, valider côté serveur le nom, le contenu et les colonnes attendues du CSV; empêcher les chemins et l'écrasement accidentel; enregistrer le fichier dans `input/mvola/`; puis appeler le traitement et n'annoncer le succès qu'après sa réponse positive. Garder la logique métier dans du code Python, pas dans un template.

## Stack technique cible

| Couche | Technologie |
|---|---|
| Backend | Django (framework web Python) |
| Frontend — interactions | HTMX (appels backend partiels) |
| Frontend — mise en page | Bootstrap + CSS personnalisé |
| Frontend — animations | JavaScript Vanilla |
| Base de données | SQLite pour dev et PostgreSQL (recommandé) pour prod |
| Authentification | Django Auth + validation par email |
| Exécution des rapprochements | Synchrone dans la requête Django |
| Stockage fichiers | Django FileField (pièces jointes de régularisation) |
| Design | Inspiré Debian 13 (palette claire, typographie monospace) |


## Sécurité et configuration

Le service actuel contient des identifiants de connexion codés en dur. Ne jamais recopier de secrets dans cette documentation, les logs ou les réponses. Pour toute évolution, les lire depuis une configuration sécurisée (variables d'environnement ou gestionnaire de secrets) et traiter les identifiants déjà exposés selon la procédure de renouvellement de l'équipe.

Configuration via `.env` (voir `.env.example`, jamais commité — `DATABASE_URL`, `DJANGO_SECRET_KEY`, `EMAIL_*`, `MVOLA_INPUT_DIR`, `MVOLA_MAX_UPLOAD_SIZE`). `config/settings.py` lève `ImproperlyConfigured` si `DEBUG=False` sans `DJANGO_SECRET_KEY`, `DATABASE_URL` (PostgreSQL) ou `EMAIL_HOST` explicites — pas de valeur par défaut silencieuse en production.

Les tables ci-après sont utilisées directement par le service de rapprochement et ne sont pas gérées par Django.
#ici la table que j'ai générer pour les process
CREATE TABLE public.interop_mvola_process (
	id int8 NOT NULL GENERATED BY DEFAULT AS IDENTITY( INCREMENT BY 1 MINVALUE 1 MAXVALUE 9223372036854775807 START 1 CACHE 1 NO CYCLE),
	"userID" int8 NOT NULL,
	"Status" text NOT NULL,
	"insertDate" timestamptz NOT NULL DEFAULT now(),
	transaction_date date NULL,
	CONSTRAINT interop_mvola_process_pkey PRIMARY KEY (id)
);

# la liste des transactions brut mvola dans notre cbs
CREATE TABLE public.interop_mvola_cbs (
	id int8 NOT NULL GENERATED BY DEFAULT AS IDENTITY( INCREMENT BY 1 MINVALUE 1 MAXVALUE 9223372036854775807 START 1 CACHE 1 NO CYCLE),
	process_id int8 NOT NULL,
	"apiLogId" text NULL,
	"PostingDate" text NULL,
	"AmountCRY" float8 NULL,
	trx_id text NULL,
	CONSTRAINT interop_mvola_cbs_pkey PRIMARY KEY (id),
	CONSTRAINT interop_mvola_cbs_process_fk FOREIGN KEY (process_id) REFERENCES public.interop_mvola_process(id) ON DELETE CASCADE
);
CREATE INDEX interop_mvola_cbs_process_idx ON public.interop_mvola_cbs USING btree (process_id);

# la liste des trransaction brut mvola depuis mvola
CREATE TABLE public.interop_mvola_mvola (
	id int8 NOT NULL GENERATED BY DEFAULT AS IDENTITY( INCREMENT BY 1 MINVALUE 1 MAXVALUE 9223372036854775807 START 1 CACHE 1 NO CYCLE),
	process_id int8 NOT NULL,
	"DATE_TRANS" text NULL,
	"TRANSID_MVOLA" text NULL,
	"STATE" text NULL,
	"MSISDN" text NULL,
	"PIVOT" int8 NULL,
	"SENS" text NULL,
	"NOM" text NULL,
	"TRANSID_PARENT" int8 NULL,
	"TRANS_TYPE" text NULL,
	"AMOUNT" int8 NULL,
	"SOLDE_PIVOT_AVANT" int8 NULL,
	"SOLDE_PIVOT_APRES" int8 NULL,
	"ORIGFTID" text NULL,
	"TYPE_OPERATION" text NULL,
	CONSTRAINT interop_mvola_mvola_pkey PRIMARY KEY (id),
	CONSTRAINT interop_mvola_mvola_process_fk FOREIGN KEY (process_id) REFERENCES public.interop_mvola_process(id) ON DELETE CASCADE
);
CREATE INDEX interop_mvola_mvola_process_idx ON public.interop_mvola_mvola USING btree (process_id);


# resulta de la réconcilmiaiton reconciliation_status-> orphan_mvola ,orphan_pamf, matched
CREATE TABLE public.interop_mvola_reconciliation (
	id int8 NOT NULL GENERATED BY DEFAULT AS IDENTITY( INCREMENT BY 1 MINVALUE 1 MAXVALUE 9223372036854775807 START 1 CACHE 1 NO CYCLE),
	process_id int8 NOT NULL,
	"DATE_TRANS" text NULL,
	"TRANSID_MVOLA" text NULL,
	"STATE" text NULL,
	"MSISDN" text NULL,
	"PIVOT" int8 NULL,
	"SENS" text NULL,
	"NOM" text NULL,
	"TRANSID_PARENT" int8 NULL,
	"TRANS_TYPE" text NULL,
	"AMOUNT" int8 NULL,
	"SOLDE_PIVOT_AVANT" int8 NULL,
	"SOLDE_PIVOT_APRES" int8 NULL,
	"ORIGFTID" text NULL,
	"TYPE_OPERATION" text NULL,
	"apiLogId" text NULL,
	"PostingDate" text NULL,
	"AmountCRY" float8 NULL,
	trx_id text NULL,
	reconciliation_status text NULL,
	CONSTRAINT interop_mvola_reconciliation_pkey PRIMARY KEY (id),
	CONSTRAINT interop_mvola_reconciliation_process_fk FOREIGN KEY (process_id) REFERENCES public.interop_mvola_process(id) ON DELETE CASCADE
);
CREATE INDEX interop_mvola_reconciliation_process_idx ON public.interop_mvola_reconciliation USING btree (process_id);

## Plan de réalisation par sprints

Ne pas commencer l'implémentation avant validation de ce découpage et clarification des décisions ouvertes du Sprint 1.

### Sprint 1 — Cadrage et fiabilisation du service (terminé)

- Décisions validées: la vue fournit le `userid`; chaque lancement est autorisé même si la date a déjà été traitée, et l'exécution est synchrone dans la requête web.
- Décisions de retraitement et d'exécution validées. La sortie des identifiants codés en dur reste un prérequis technique du Sprint 4 avant tout appel au service.
- La connexion CBS actuellement utilisée par le service cible déjà des bases de test; conserver cette configuration.
- **Critère de sortie:** les décisions de retraitement et d'exécution sont validées; le service peut être testé avec des données simulées ou des bases de test isolées, jamais contre des bases de production.

### Sprint 2 — Socle Django et gestion des utilisateurs (terminé)

- Créer le projet Django dans le dossier `DigitalChannel` en conservant `service_mvola.py`, les templates de référence et les rapports existants.
- Configurer SQLite pour le développement et PostgreSQL pour la production, avec des paramètres et secrets distincts selon l'environnement.
- Mettre en place l'inscription libre avec Django Auth. Un compte reste inactif jusqu'à la confirmation de l'adresse email par un lien à durée limitée.
- Envoyer le mail de confirmation via SMTP en s'inspirant de la fonction `send_email` fournie: serveur `mail.pamf.mg`, port `25`, expéditeur `noreply@pamf.mg`. Lire ces paramètres depuis la configuration d'environnement; ne pas reprendre la liste de destinataires du rapport PAR.
- L'envoi doit remonter ses erreurs à l'application: si le mail ne part pas, ne pas confirmer le compte et afficher une erreur récupérable. Vérifier avec l'équipe infrastructure si le serveur exige TLS ou authentification SMTP.
- Utiliser les groupes et permissions Django pour que les rôles et leurs privilèges puissent évoluer; créer initialement les rôles Viewer, Backoffice et Admin.
- Viewer: consulter les transactions et les résultats, sans importer de rapport, lancer de traitement ni gérer les utilisateurs.
- Backoffice: importer les rapports, lancer les rapprochements et consulter leurs résultats; aucun accès à la gestion ou à la liste des utilisateurs.
- Admin: gérer les utilisateurs et les rôles, et accéder à toutes les fonctions MVola. Les utilisateurs non confirmés ne peuvent accéder à aucune fonction protégée.
- Garder les tables `interop_mvola_*` hors gestion des migrations Django; configurer leur accès côté service et utiliser une base PostgreSQL de test isolée pour les tests d'intégration.
- **Critère de sortie:** le projet démarre en local avec SQLite; inscription, envoi et confirmation email, erreurs d'envoi et droits des trois rôles sont testés; la configuration PostgreSQL de production est définie et testée uniquement sur une base isolée.
- S'inspirer du thème et du style visuel de `templates/` et réutiliser ses feuilles CSS si elles conviennent; ne pas reprendre les templates comme implémentation.

### Sprint 3 — Import web des rapports (terminé)

- Créer le formulaire et la vue d'import dans l'application Django réelle.
- Vérifier côté serveur le nom, le contenu et les colonnes du CSV; refuser les chemins non autorisés et éviter l'écrasement accidentel.
- Enregistrer les fichiers valides dans `input/mvola/` et afficher le résultat de l'import.
- **Critère de sortie:** un utilisateur autorisé peut importer un rapport valide; un fichier invalide est refusé sans modifier les rapports existants.

### Sprint 4 — Exécution du rapprochement (en cours)

- Les identifiants PostgreSQL et CBS codés en dur ont été retirés de `service_mvola.py`; les connexions utilisent la configuration d'environnement. Renseigner les identifiants CBS de test par le canal sécurisé avant tout rapprochement réel.
- Depuis la vue Django, appeler directement et de façon synchrone `reconciliation(file, userid)` avec le rapport validé et l'utilisateur connecté.
- Afficher un modal bloquant `En cours` pendant l'appel; n'autoriser sa fermeture et n'afficher le résultat final qu'après le retour du service et la fin de sa transaction PostgreSQL.
- Enregistrer le résultat final et le `process_id` dans le suivi Django hors des tables `interop_mvola_*`; afficher les erreurs renvoyées par le service.
- Chaque lancement crée un nouveau processus, y compris si cette date a déjà été traitée; ne pas bloquer les retraitements.
- N'annoncer le succès qu'après validation de la transaction PostgreSQL du service. Configurer les délais d'attente du serveur web et du proxy pour couvrir la durée maximale du traitement.
- **Critère de sortie:** le statut affiché correspond au résultat effectivement persisté et les échecs restent explicites.

### Sprint 5 — Consultation des résultats (en cours)

- Ajouter l'historique des processus et la consultation des transactions MVOLA, CBS et des écarts à partir des tables externes.
- Permettre de filtrer les statuts `matched`, `orphan_mvola` et `orphan_pamf`, ainsi que la date ou le processus.
- Réserver l'historique et le détail aux utilisateurs autorisés par `view_reconciliation_results`.
- **Critère de sortie:** un utilisateur autorisé peut retrouver un rapprochement et examiner ses résultats sans relancer le traitement.

### Sprint 6 — Validation et mise en production

- Tester le parcours complet avec des rapports représentatifs, les erreurs CBS/PostgreSQL, les droits d'accès et les tentatives de retraitement.
- Vérifier la configuration des secrets, les journaux, la sauvegarde des rapports et la procédure de reprise.
- **Critère de sortie:** parcours validé dans l'environnement cible et consignes d'exploitation documentées.
</content>
