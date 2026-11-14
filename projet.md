# Projet : outil de test de charge (stress test) pour mon homelab

## Contexte
Je suis étudiant (orientation cybersécurité) et j'ai un homelab Proxmox chez moi.
Je veux un outil en ligne de commande pour tester la RÉSISTANCE À LA CHARGE de MES
PROPRES machines, sur MON réseau local. Objectif : voir comment mes services réagissent
à beaucoup de connexions / paquets (TCP et UDP), mesurer débit, latence et taux d'erreur.
Ce n'est pas un outil d'attaque : les cibles sont uniquement mes machines, sur des
plages IP privées.

Ma plage réseau (la seule cible autorisée) :
- 192.168.35.0/24  (réseau maison)

## Ce que je veux
Un outil Python 3.11+, qui marche aussi bien sur Windows que sur une VM Linux,
dépendances minimales (stdlib en priorité ; une lib d'affichage comme `rich` est
acceptable mais optionnelle). Point d'entrée clair et `--help` complet.

### 1. Choix de la cible (interactif)
Lancé sans argument, l'outil affiche un MENU : il liste mes machines par NOM à partir
d'un inventaire (IP → nom), je choisis une machine (ou je tape une IP à la main), puis
il me demande le PORT, le protocole (TCP/UDP), la durée et l'intensité.
Tous ces paramètres doivent AUSSI être passables en option de ligne de commande
(mode non interactif), par ex. :
  stress -t 10.10.10.21 -p 3000 --proto tcp --duration 30 --concurrency 100

Inventaire de départ (à mettre dans un fichier séparé facile à éditer, ex. inventory.*),
que l'outil charge au démarrage :
  192.168.35.11  proxmox
  192.168.35.12  opnsense (WAN / GUI 8443)
  192.168.35.13  adguard
  192.168.35.14  home-assistant
  192.168.35.15  truenas
  192.168.35.16  monitor (uptime-kuma)
  10.10.10.10    npm (reverse proxy)
  10.10.10.11    pbs
  10.10.10.20    vaultwarden
  10.10.10.21    gitea
  10.10.10.22    gitea-runner
  10.10.10.23    postgres / pgadmin
  10.10.10.30    vm-gpu (immich / litellm / open-webui)
  10.10.10.40    vm-torrent
Quand je sélectionne une IP, affiche le nom de la machine en confirmation.

### 2. Mode TCP
- Ouvre N connexions simultanées (paramètre --concurrency) vers IP:port pendant --duration.
- Option : envoyer une charge utile (--size octets) sur chaque connexion, ou juste
  connecter/fermer (test de capacité de connexions).
- Métriques : connexions tentées / réussies / échouées, connexions par seconde,
  latence de connexion (min / moyenne / p50 / p95 / max), octets envoyés/reçus, débit.
- Option --ramp : montée progressive de la concurrence (ex. de 1 à N sur la durée).

### 3. Mode UDP
- Envoie des paquets vers IP:port à un débit cible (--rate paquets/s) pendant --duration,
  taille de paquet --size.
- Métriques : paquets envoyés, débit réel (pps et Mbit/s), et si le service renvoie quelque
  chose, paquets reçus en retour (sinon, préciser que l'UDP ne garantit aucune réponse).

### 4. Affichage
- Stats en direct pendant le test (rafraîchies chaque seconde).
- Résumé clair à la fin.
- Option --csv <fichier> pour écrire le résumé (me permet de comparer plusieurs runs).

## Garde-fous (important)
- REFUSER toute cible qui n'est pas dans mes 3 plages privées ci-dessus : message
  d'erreur clair et sortie, pas de requête envoyée. (Une option --allow-host <ip> peut
  autoriser ponctuellement une IP privée hors inventaire, mais jamais une IP publique.)
- Demander une CONFIRMATION avant de lancer (sauf flag --yes pour le mode scripté),
  en récapitulant cible + nom de machine + port + proto + intensité + durée.
- Intensité par défaut MODÉRÉE (ex. concurrency 50, rate 1000 pps) pour ne pas faire
  tomber un service par accident au premier essai.
- Ctrl+C = arrêt PROPRE : stopper les workers, fermer les sockets, afficher quand même
  le résumé partiel.
- Borne haute raisonnable sur concurrency / rate avec un avertissement si je la dépasse.

## Structure et livrables
- Code organisé (séparer : inventaire, mode TCP, mode UDP, affichage/stats, CLI).
- README.md : à quoi ça sert, comment l'installer et le lancer, exemples de commandes,
  explication des métriques, rappel que c'est réservé à mon propre réseau.
- requirements.txt si une dépendance est utilisée.
- Code commenté en français, lisible (c'est aussi un projet d'apprentissage).

## Déroulé attendu
Commence par me proposer la structure des fichiers et le choix technique (asyncio vs
threads pour TCP et UDP), attends mon accord, puis implémente. Teste au moins le parsing
CLI et le mode interactif avant de me dire que c'est prêt.

## Contraintes Windows (l'outil tournera sur Windows, Python)
- Doit fonctionner depuis PowerShell et cmd, sans WSL ni droits admin.
- asyncio sous Windows : utiliser une boucle adaptée à la forte concurrence réseau
  (SelectorEventLoop plutôt que Proactor si besoin pour ouvrir beaucoup de sockets),
  et documenter la limite pratique du nombre de connexions simultanées sous Windows.
- Gérer proprement Ctrl+C sous Windows (le signal se comporte différemment de Linux) :
  l'arrêt doit rester propre et afficher le résumé.
- Si `rich` est utilisé pour l'affichage live, vérifier qu'il rend bien dans Windows
  Terminal ET dans la console cmd classique (sinon, prévoir un mode d'affichage simple
  en repli).
- README : commandes de lancement en PowerShell (ex. `python stress.py` ou `py stress.py`),
  et comment créer/activer un venv sous Windows (`py -m venv .venv` puis
  `.venv\Scripts\activate`).
- Pas de dépendance à des outils POSIX (pas de `os.fork`, pas de modules Unix-only).