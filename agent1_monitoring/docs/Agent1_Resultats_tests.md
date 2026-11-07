# Agent 1 – Résultats des tests

Date du test réel : 11/10/2026, 19:05 à 19:44, sur le banc local (service de paiement de test, Prometheus, cAdvisor).
Script : `python experiments/checks/full_scenario.py 19:05`. Les journaux complets sont dans `experiments/runs/scenario/` (non versionnés).

## 1. Tests automatiques

| Contrôle | Résultat |
|---|---|
| `pytest tests` (sans Docker) | 227 réussis, 1 ignoré par défaut |
| Test avec le vrai modèle Chronos (`PIDS_RUN_CHRONOS=1`) | réussi : courbe stable jugée normale, fuite jugée anormale |
| ruff, mypy (strict), `export_contracts --check` | aucune erreur |

Chaque événement produit par l'agent est validé par le contrat Python **et** par le schéma JSON `contracts/jsonschema/v1/EventEnvelope.schema.json`.

## 2. Délai de détection par panne (test réel)

Pour chaque panne, on lance un nouvel agent au moment de l'injection, avec un cycle toutes les 30 s.

| Panne injectée | Détecteur | Première détection | Délai |
|---|---|---|---|
| Fuite mémoire 30 Mo/min | Statistique (pente) | 19:07:50 | 2 min 50 s |
| | Chronos | 19:08:20 | 3 min 20 s |
| Latence +800 ms | SLO latence (slow-burn) | 19:19:17 | 2 min 23 s |
| | Statistique (saut de niveau) | 19:19:47 | 2 min 53 s |
| | SLO latence (fast-burn, HIGH) | 19:20:17 | 3 min 23 s |
| | Chronos | 19:20:17 | 3 min 23 s |
| 50 % d'erreurs HTTP 500 | SLO disponibilité (slow-burn) | 19:30:41 | 5 min 21 s |
| | SLO disponibilité (fast-burn, HIGH) | 19:31:11 | 5 min 51 s |
| CPU saturé | Statistique (saut de niveau) | 19:38:35 | 2 min 52 s |
| | Chronos | 19:39:05 | 3 min 22 s |

**Lecture :**
- Les 4 pannes sont détectées en **2 min 23 s à 5 min 51 s**.
- La statistique et Chronos détectent toutes les pannes de mémoire, de latence et de CPU. Chronos arrive en moyenne **30 s après** la statistique.
- Les erreurs 500 sont vues par le SLO uniquement : c'est voulu, car un taux d'erreurs se juge par rapport à l'objectif de qualité.
- Le délai du SLO sur les erreurs est plus long (environ 5 min 30 s). C'est le prix des fenêtres longues (1 h et 6 h), qui évitent d'alerter sur un pic bref.

## 3. Fausses alertes

- **Aucune alerte sur un signal sain** pendant une panne d'un autre type : par exemple, le CPU et la latence restent normaux pendant la fuite mémoire.
- **Signaux résiduels :** au début des phases C et D, l'agent signale encore la panne de la phase précédente (latence, puis erreurs) pendant 20 à 30 s. Ce ne sont pas des fausses alertes : les fenêtres SLO de 5 minutes contiennent encore les requêtes dégradées. La fenêtre courte de 5 minutes garantit que ces signaux s'arrêtent après la guérison.

## 4. Envoi aux autres agents (contrat v1)

9 événements ont été écrits dans `events.jsonl`, tous valides :

| Sujet | Nombre | Exemple |
|---|---|---|
| `monitoring.anomaly.detected` | 7 | fuite mémoire : P2 / MEDIUM, confiance 0,84, détecteur `statistical` |
| `monitoring.recovery.verified` | 2 | voir section 5 |

La priorité suit la gravité : les incidents HIGH (SLO fast-burn) partent en P1. Le même incident garde la même clé d'idempotence.

## 5. Vérification indépendante de la guérison

Le message `deployment.action.completed` est fourni par le fichier `experiments/checks/action_completed.json`. Fenêtre d'observation : 2 min, avec une mesure toutes les 30 s.

| Situation | Résultat de l'agent |
|---|---|
| L'action se dit réussie mais la fuite continue | **NOT_RECOVERED** (`memory_slope_mb_min` = FAIL) |
| Après la vraie réparation | **RECOVERED** (erreurs, latence et mémoire = PASS) |

L'agent ne croit pas l'agent de déploiement sur parole : il mesure lui-même.

## 6. Tâche de l'Orchestrateur

`inbox detect payment-service`, envoyée pendant la fuite, a renvoyé un `AgentResult` SUCCEEDED : « Anomalie sur payment-service : memory_pressure », avec une confiance de 0,6 et une preuve `ANM-…`.

## 7. Limites connues

- **Chronos s'habitue à une panne longue.** Après environ 6 minutes de fuite, la fuite entre dans son historique et il la prédit. La statistique (pente) prend le relais. Amélioration possible : figer l'historique de référence pendant un incident.
- **Kafka et le Context Service de l'équipe ne sont pas encore implémentés.** Les événements passent pour l'instant par un fichier JSON Lines, à travers l'interface `EventPublisher` : le branchement sur Kafka ne changera pas l'agent.
- **Kubernetes et les traces ne sont pas couverts.** Le banc local surveille uniquement des conteneurs Docker.

## 8. Test réel sur l'API de détection de fraude (16/10/2026, 14:34 → 14:49)

L'équipe a choisi comme application cible le projet `Fraud_Detection_MLOps` : une API FastAPI qui sert un modèle MLflow. Il est lancé **depuis son propre dépôt** (profil Docker `fraud`) ; son code n'est pas copié ici. L'agent le surveille en mode app, à partir de sa page `/metrics` (prometheus-fastapi-instrumentator, étiquette `handler`).

Script : `python experiments/checks/fraud_scenario.py`, avec un historique Prometheus vierge au départ. Trafic : `experiments/load/fraud_traffic.py`.

| Phase | Action | Ce que l'agent a fait | Délai |
|---|---|---|---|
| 0. Normal | transactions saines | service `UP`, **aucune alerte** | — |
| 1. Panne | `docker stop fraud-api` à 14:36:31 | `DOWN`, incident `readiness` HIGH, alerte **P1** | 61 s |
| 1b. Réparation | `docker start` à 14:39:33, puis `deployment.action.completed` | `UP`, puis vérification **RECOVERED** (erreurs, latence, mémoire et `service_up` = PASS) | 29 s |
| 2. Vrais bugs | 50 % des transactions déclenchent un bug de l'API à partir de 14:42:05 | SLO disponibilité slow-burn, incident `error_rate` MEDIUM, alerte **P2** | 27 s |
| | | SLO fast-burn, incident passé en HIGH | 57 s |

Événements envoyés : `monitoring.anomaly.detected` (P1, readiness), `monitoring.recovery.verified` (RECOVERED) et `monitoring.anomaly.detected` (P2, error_rate).

**Deux vrais bugs de l'API trouvés grâce à l'agent** (HTTP 500, à signaler à l'équipe) :
1. `paymentMethodAgeDays` est converti en entier par l'API, alors que la signature du modèle MLflow attend un réel. Toute carte de plus d'un jour provoque une erreur 500.
2. Une `Category` absente (autorisée par le schéma de l'API) fait échouer la concaténation `Category + "_" + paymentMethod` dans `Src/model.py`.

Un premier essai, qui envoyait sans le savoir des catégories vides, a été signalé par l'agent dès la phase « normale » (`error_rate` HIGH). La vérification après redémarrage a répondu `NOT_RECOVERED` (l'API répondait, mais elle plantait toujours). Les deux réponses étaient justes.

**Corrections apportées à l'agent à la suite de ces tests :**
- **Détection « service mort »** (`detectors/availability.py`). Avant, un service arrêté ne donnait que « données insuffisantes ». La vérification de guérison inclut maintenant `service_up`.
- **Nouvelle alerte quand la gravité augmente** (MEDIUM vers HIGH). Pendant la phase 2, le passage en HIGH n'avait pas été renvoyé, et l'Orchestrateur restait sur une alerte P2. La clé d'idempotence inclut désormais la gravité.

**Vérification en vrai des corrections** (même scénario, 16/10/2026, 15:27 → 15:42, historique Prometheus vierge) :

| Heure | Événement envoyé | Clé d'idempotence (fin) |
|---|---|---|
| 15:30:00 | `anomaly.detected` P1 HIGH : readiness (arrêt à 15:28:58, détecté en 62 s) | `readiness:HIGH` |
| 15:34:07 | `recovery.verified` : RECOVERED (redémarrage à 15:32:03, `UP` en 27 s) | `recovery:ACT-fraud-restart-001` |
| 15:35:30 | `anomaly.detected` P2 MEDIUM : error_rate (bugs lancés à 15:34:37, détectés en 53 s) | `error_rate:MEDIUM` |
| 15:36:00 | `anomaly.detected` **P1 HIGH** : error_rate, raison « gravite en hausse (error_rate MEDIUM -> HIGH) » | `error_rate:HIGH` |

- **Gravité en hausse.** Un premier essai (15:12 → 15:27) a montré que la comparaison par service ne suffisait pas : l'alerte HIGH de la panne précédente (readiness) masquait la hausse d'`error_rate`. La gravité est maintenant suivie **par catégorie de symptôme**.
- **Catégories de l'événement.** L'alerte de 15:35:30 ne cite plus `readiness` (le service était de nouveau `UP` depuis 15:32:30) : seuls les incidents observés pendant le cycle sont listés.

## 9. Corrections issues du test réel à deux services (payment-service → payments-db)

- **L'alerte visait la victime.** Base lente : le raisonnement classait `payments-db` en premier (0,61), mais la règle métier « HIGH toujours alerté » alertait `payment-service`. Désormais, quand le raisonnement explique l'incident grave par une dépendance, l'alerte vise l'origine probable (« origine probable payments-db »). Vérifié en vrai le 19/10 à 16:28 et 19:44 : alerte sur `payments-db`.
- **Une nouvelle panne était fusionnée avec l'ancienne.** Un problème du même type, sur le même service, revenu 12 min après le précédent, restait dans l'ancien incident (« deja alerte »). Désormais, après **5 min de silence**, son retour ouvre un **nouvel incident** et une **nouvelle alerte** (« nouvel incident »). Une panne qui clignote, avec des trous plus courts, reste un seul incident.
- **Chronos désactivé par défaut** (`forecast.enabled: false`). Il confirmait les pannes environ 30 s après la statistique, s'habituait aux pannes longues et coûtait environ 350 Mo de RAM. Il reste disponible pour les comparaisons.

Test du raisonnement, état actuel : essais 1 (base lente → `payments-db`) et 2 (paiement lent → `payment-service`) réussis. Essais 3 à 6 à refaire avec ces corrections.

## 10. Test réel « tué faute de mémoire » (OOM, 20/10/2026)

Fuite de mémoire injectée sur payment-service (400 Mo/min, limite du conteneur 512 Mo).

**1er essai (12:14) : échec.** Le conteneur a bien été tué et redémarré, mais sous Docker Desktop /
WSL2 le compteur de cAdvisor `container_oom_events_total` reste à 0 (Docker affiche même
`OOMKilled=false`). L'agent attendait une preuve qui n'arrive jamais sur ce poste.

**Correction :** un OOM est maintenant reconnu par deux faits observés sur 5 minutes :
le processus a redémarré (`changes(process_start_time_seconds)` > 0) **et** sa mémoire avait atteint
au moins 90 % de la limite du conteneur (pic du working set / limite). Le compteur cAdvisor reste
utilisé quand il fonctionne (hôte Linux). Un redémarrage sans mémoire à la limite devient un
symptôme `readiness` HIGH (redémarrage imprévu).

**2e essai (12:28) : réussi.**

| Heure | Fait |
|---|---|
| 12:28:37 | fuite injectée |
| 12:31:06 | conteneur tué et redémarré (Docker : `OOMKilled=false`) |
| 12:31:23 | `redem=1.00 pic/limite=1.00 -> KILLED`, incident memory_oom CRITICAL, événement **P0** (confiance 0.9) |

Délai entre la mort du conteneur et l'alerte : **17 s**. Un seul événement émis sur les 14 cycles
(les 5 cycles suivants : « déjà alerté, situation inchangée »), aucune fausse alerte.

Limite : en mode conteneur (service non instrumenté), le redémarrage n'est pas encore mesurable ;
seul le compteur cAdvisor est utilisé, qui fonctionne sur un hôte Linux mais pas sous WSL2.

## 11. Test réel final sur l'API de fraude (20/10/2026, 13:13 → 13:27)

Même scénario qu'en section 8, avec la version finale de l'agent (cycle de 30 s, fenêtre de
disponibilité de 45 s, règle SLO très rapide, détection des redémarrages).

| Étape | Heure | Ce que l'agent a envoyé à l'orchestrateur | Délai |
|---|---|---|---|
| 0. Trafic normal | 13:13 | rien (aucune fausse alerte) | — |
| 1. API arrêtée (`docker stop`) | 13:15:03 | `anomaly.detected` readiness, **P1** HIGH, confiance 0.6 | **31 s** (62 s le 16/10) |
| 1b. API redémarrée | 13:18:04 | `recovery.verified` **RECOVERED** | après la fenêtre de vérification |
| 2. Vrais bugs (50 % de HTTP 500) | 13:20:37 | `anomaly.detected` error_rate, **P2** MEDIUM, confiance 0.8 | **27 s** |
| 2. Les erreurs continuent | ~13:22 | `anomaly.detected` « gravité en hausse » MEDIUM → HIGH, **P1** | — |

Quatre événements au total, aucun doublon : pendant que la situation ne change pas, l'agent
note « déjà alerté, situation inchangée » et n'envoie rien.

Défaut observé : après le redémarrage volontaire (réparation), la détection des redémarrages
rattache ce redémarrage à l'incident readiness, qui reste donc ouvert jusqu'à 5 min de plus. Aucune
alerte supplémentaire n'est envoyée, mais un redémarrage voulu ne devrait pas compter comme
symptôme.

## 12. Intégration dans le dépôt de l'équipe (04/11/2026, `main` c3ff102)

Les sections précédentes ont été mesurées avant l'intégration : les chemins qu'elles citent
(`experiments/...`) sont aujourd'hui sous `agent1_monitoring/experiments/`. Les chiffres ci-dessous
sont mesurés dans ce dépôt, sur un PC de 8 Go de RAM (0,7 Go libre pendant les tests).

**Changements observés dans l'API depuis les tests précédents**

- Les 2 bugs trouvés par l'agent (sections 8 et 11) sont **corrigés** dans `main` : les mêmes valeurs
  répondent HTTP 200. `--buggy-share` sert donc de test de non-régression, et la détection d'erreurs
  se teste maintenant sur le service de test (`/chaos/errors`).
- Une prédiction est plus lente : médiane 144 ms contre ~95 ms, l'API calculant aussi une probabilité
  et écrivant chaque prédiction en SQLite (`synchronous=FULL`).
- L'API met environ 50 s à démarrer.

**Scénario réel sur l'API de fraude (11:15 → 11:32)**

| Étape | Résultat |
|---|---|
| Réchauffage 5 min, puis 2 min de trafic normal | Lenteur vue (MEDIUM), vérifiée, aucune alerte envoyée |
| Panne (`docker stop`, 11:22:43) | Alerte **P1** à 11:23:46, soit 63 s |
| Réparation et vérification | **RECOVERED**, 4 contrôles PASS |
| Valeurs des 2 anciens bugs, 3 min | 0 erreur 500, aucune alerte |

**Service de test (11:33 → 11:41)**

| Panne | Résultat |
|---|---|
| 50 % d'erreurs (11:34:57) | Alerte **P1** `error_rate` à 11:36:30, soit 93 s |
| Fuite de 400 Mo/min | `memory_pressure` vue à 11:39:59, juste avant la mort du service |
| Service tué (11:39:58) | Alerte **P0** `memory_oom`, confiance 0,95. Ce run s'est arrêté 1 s après la mort : le P0 vient d'un cycle relancé à 11:41:09 ; le délai n'est donc pas mesuré ici (17 s en section 10) |

**Lien avec l'Agent 2** (`agent2_diagnosis.agent1_runner`, sur les vraies alertes)

| Alerte de l'Agent 1 | Diagnostic de l'Agent 2 |
|---|---|
| `memory_oom` | `MEMORY_LIMIT_REACHED` (0,8) |
| `memory_pressure` | `MEMORY_GROWTH` (0,7) |
| `error_rate` seul | `INSUFFICIENT_EVIDENCE` (0,25) |
| `latency` seul (base lente) | `INSUFFICIENT_EVIDENCE` (0,25) |

**Limites constatées**

- Avec l'API sur disque, le trafic normal montre des rafales de lenteur (jusqu'à 67 % de requêtes de
  plus de 500 ms) qui ont déclenché des alertes P2 et P1, et un NOT_RECOVERED. Une seule mesure après
  réchauffage donne 98,4 % sous 500 ms. Avec le journal SQLite en mémoire (`compose.agent1.yaml`,
  banc local uniquement) : 82 % sous 100 ms contre 62 %, mais il reste une rafale à 21 % en 8 min et
  3,17 % de requêtes lentes au total. La cause probable est le manque de RAM et le disque de Docker
  sous WSL2 ; elle n'est pas prouvée. Les seuils de l'agent n'ont pas été changés : ces lenteurs sont
  réelles. **RECOVERED et l'absence d'alerte P1 en trafic normal ne sont obtenus qu'une fois, avec ce
  réglage : à revalider sur une machine moins chargée.**
- `agent1_runner.py` de l'Agent 2 refuse tout un fichier qui contient un
  `monitoring.recovery.verified` : il faut lui donner seulement les anomalies (voir le README).
- La campagne de raisonnement (2 et 3 services) n'a pas été relancée dans ce dépôt. Le test en direct
  du 21/10 sur 2 services (origine `payments-db` 0,61 contre `payment-service` 0,20) a été fait avant
  l'intégration.
