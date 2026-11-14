# stresser : test de charge TCP/UDP pour mon homelab

Outil en terminal, avec menus, qui mesure comment un service de **mon propre réseau**
réagit à beaucoup de connexions TCP ou de paquets UDP : débit, latence, taux d'erreur.

> **Réservé à mes machines.** L'outil refuse toute cible hors de la plage
> `192.168.35.0/24`. Tester une machine qui ne m'appartient pas, sans accord écrit
> de son propriétaire, est illégal.

## Installation

Il faut Python 3.11 ou plus récent, et rien d'autre : l'outil n'utilise que la
bibliothèque standard. Aucun droit administrateur ni WSL n'est nécessaire.

```powershell
python stress.py          # Windows (ou : py stress.py)
```

```bash
python3 stress.py         # Linux
```

## Utilisation

### Avec les menus

```powershell
python stress.py
```

Lancé sans option, l'outil s'ouvre sur un menu :

| Entrée du menu | Rôle |
|---|---|
| Lancer un test | Assistant en 5 étapes : machine, protocole, port, intensité, récapitulatif. |
| Relancer le dernier test | Reprend les mêmes paramètres ; le récapitulatif est réaffiché avant le lancement. |
| Inventaire des machines | Les machines connues, avec un rechargement du fichier après modification. |
| Historique de la session | Revoir le résumé des tests lancés depuis l'ouverture du programme. |
| Réglages | Export CSV, délai de connexion TCP, symboles Unicode ou ASCII, couleurs. |
| Aide | Ce que mesure l'outil, en bref. |

| Touche | Effet |
|---|---|
| Flèches haut / bas | Parcourir le menu (il boucle). |
| Entrée | Valider. |
| Échap | Revenir à l'écran précédent, sans perdre ce qui est déjà saisi. |
| Chiffre 1 à 9 | Choisir directement la ligne portant ce numéro. |
| Ctrl+C | Pendant un test : l'arrêter et afficher le résumé partiel. Dans un menu : quitter. |

Dans une question à réponse libre (port, durée…), Entrée seule garde la valeur
entre crochets, et `<` revient en arrière.

L'étape « intensité » propose des profils tout prêts :

| Profil | TCP | UDP |
|---|---|---|
| Léger | 10 connexions, 10 s | 100 paquets/s, 10 s |
| Modéré | 50 connexions, 10 s | 1000 paquets/s, 10 s |
| Soutenu | 200 connexions, 30 s | 5000 paquets/s, 30 s |
| Montée progressive | de 1 à 200 connexions en 30 s | |
| Personnalisé | durée, connexions, octets, maintien, montée | durée, débit, taille |

Pendant le test, un tableau de bord se met à jour chaque seconde : barre
d'avancement, compteurs, et un mini-graphique du débit. Il laisse ensuite la
place au résumé.

Les options de test passées sans `-t` (par exemple `python stress.py -p 8123`)
deviennent les valeurs proposées par défaut dans l'assistant.

### Sans les menus (scripts)

Avec `-t`, tout vient des options et aucun menu ne s'ouvre.

```powershell
# 100 connexions TCP simultanées pendant 30 s (connecter puis fermer)
python stress.py -t 192.168.35.14 -p 8123 --proto tcp --duration 30 --concurrency 100

# Envoyer 512 octets par connexion, la garder ouverte 1 s, monter progressivement
python stress.py -t 192.168.35.14 -p 8123 --size 512 --hold 1 --ramp

# Tenir 200 connexions ouvertes en même temps pendant 60 s
python stress.py -t 192.168.35.16 -p 3001 --concurrency 200 --hold 60 --duration 60

# 500 paquets UDP de 64 octets par seconde, résumé ajouté à un CSV
python stress.py -t 192.168.35.13 -p 53 --proto udp --rate 500 --size 64 --csv tests.csv

# Sans confirmation (script)
python stress.py -t 192.168.35.16 -p 3001 --duration 10 --yes
```

### Options

| Option | Rôle | Défaut |
|---|---|---|
| `-t IP` | Cible. Sans elle : les menus. | menus |
| `-p PORT` | Port de la cible. | obligatoire avec `-t` |
| `--proto tcp\|udp` | Protocole. | `tcp` |
| `--duration S` | Durée du test, en secondes. | 10 |
| `--concurrency N` | TCP : connexions simultanées. | 50 |
| `--rate PPS` | UDP : paquets par seconde. | 1000 |
| `--size OCTETS` | TCP : octets envoyés par connexion. UDP : taille d'un paquet. | 0 (TCP), 64 (UDP) |
| `--ramp` | TCP : montée progressive de 1 à N connexions sur la durée. | non |
| `--hold S` | TCP : garde chaque connexion ouverte S secondes et compte ce qui est reçu. | 0 |
| `--timeout S` | TCP : délai maximal d'une connexion. | 3 |
| `--csv FICHIER` | Ajoute le résumé à un fichier CSV. | aucun |
| `--plain` | Texte simple : ni couleurs, ni menus à flèches, ni tableau de bord. | non |
| `--ascii` | Cadres et barres en ASCII, si la console affiche mal l'Unicode. | non |
| `--yes` | Pas de confirmation (exige `-t`). | non |
| `--allow-host IP` | Autorise ponctuellement une IP privée hors plage. | aucune |
| `--inventory FICHIER` | Autre fichier d'inventaire. | `inventory.txt` |
| `--list` | Affiche l'inventaire et quitte. | |

Codes de sortie : `0` test terminé, `1` annulé, `2` cible refusée ou paramètre
invalide, `130` test interrompu par Ctrl+C.

## Inventaire

[inventory.txt](inventory.txt) contient une machine par ligne : `IP  nom  description`.
Le marqueur `[critique]` signale une machine dont dépendent d'autres services
(hyperviseur, pare-feu, DNS, stockage) : l'outil demande alors de retaper son nom
avant de lancer le test.

## Garde-fous

- **Plage autorisée** : `192.168.35.0/24` uniquement, définie dans
  [stresser/guard.py](stresser/guard.py) (`ALLOWED_NETWORKS`). Toute autre cible
  est refusée avant le moindre envoi.
- **`--allow-host`** : accepte une adresse privée (RFC 1918) précise, jamais une
  adresse publique, ni le loopback, ni une adresse de broadcast.
- **Adresses IP uniquement** : pas de nom DNS, pour qu'une résolution de nom ne
  puisse pas envoyer le trafic ailleurs que prévu.
- **Confirmation** : récapitulatif (cible, machine, port, protocole, intensité,
  durée) puis accord explicite, dans les menus comme en ligne de commande. Seul
  `--yes` (qui exige `-t`) le saute.
- **Intensité modérée par défaut**, et avertissement au-delà de 1000 connexions
  ou de 20 000 paquets/s.
- **Ctrl+C** : arrêt propre, sockets fermées, résumé partiel affiché. Sous Linux,
  `SIGTERM` fait la même chose.

## Lire les métriques

### TCP

| Métrique | Signification |
|---|---|
| Connexions tentées / réussies / échouées | Une connexion est réussie quand la poignée de main TCP aboutit. |
| En cours à l'arrêt | Tentatives qui n'avaient pas encore abouti à la fin du test. |
| Connexions par seconde | Connexions réussies divisées par la durée réelle. |
| Latence de connexion | Temps de la poignée de main, en millisecondes. `p95` = 95 % des connexions ont été plus rapides ; c'est lui qui montre les ralentissements que la moyenne cache. |
| Pic de connexions ouvertes | Maximum de connexions ouvertes au même instant. |
| Octets envoyés / reçus | Charge utile seule, sans les en-têtes TCP/IP. |
| Fermées par le serveur | Connexions que le serveur a coupées pendant le maintien `--hold`. |

Types d'échec :

| Type | Cause habituelle |
|---|---|
| `refusée` | Rien n'écoute sur le port, ou la file d'attente du serveur est pleine. |
| `timeout` | Pas de réponse dans le délai `--timeout` : serveur saturé ou pare-feu qui jette les paquets. |
| `reset` | Le serveur a coupé la connexion. |
| `injoignable` | Pas de route vers la cible. |
| `locale (client)` | Limite atteinte sur **ma** machine (voir plus bas), pas sur le serveur. |

Sans `--hold`, l'outil ferme la connexion juste après l'envoi : les octets reçus
restent donc à 0. Pour compter une réponse, ajouter par exemple `--hold 0.5`.

### UDP

| Métrique | Signification |
|---|---|
| Paquets envoyés | Paquets remis au système. Rien ne dit qu'ils sont arrivés. |
| Débit réel | Paquets par seconde et Mbit/s réellement émis, à comparer à la cible `--rate`. |
| Paquets reçus en retour | Réponses de la cible. L'UDP ne garantit ni la livraison ni une réponse : 0 est normal si le service ne répond pas à des données quelconques. |
| Port injoignable (ICMP) | La cible signale que rien n'écoute sur ce port. Beaucoup de systèmes limitent ces messages à quelques-uns par seconde. |

## Limites à connaître

**L'outil mesure aussi le client.** Python sature avant une machine correcte,
surtout en UDP. Pour savoir qui a plafonné, regarder en même temps le CPU de la
cible (et la table d'états d'OPNsense si le trafic le traverse).

**C'est un test de couche 4.** Il mesure la capacité d'un service à accepter des
connexions ou des paquets, pas sa performance applicative. Pour tester un site
web en HTTP, des outils comme `hey` ou `k6` sont plus adaptés ; `iperf3` mesure
mieux le débit brut d'un lien.

**Sur une VM du même Proxmox que la cible**, deux biais : le générateur et la
cible se partagent le CPU de l'hôte, et le trafic ne sort pas du bridge virtuel
(ni switch physique, ni câble). Les chiffres sont alors à comparer entre eux,
pas à prendre comme des valeurs absolues.

**Nombre de connexions simultanées**

| | Linux | Windows |
|---|---|---|
| Fichiers ouverts | Limité par `ulimit -n`. L'outil relève lui-même la limite souple jusqu'à la limite dure ; au-delà, lancer `ulimit -n 65535` avant. | Pas de limite basse de ce type. |
| Ports source | Environ 28 000 (`/proc/sys/net/ipv4/ip_local_port_range`). | 16 384 par défaut (49152 à 65535). |
| Limite pratique | La plus basse des deux lignes ci-dessus. | Les ports source : au plus 16 384 connexions vers une même cible. |

Chaque connexion sortante consomme un port source, que le système garde réservé
un moment après la fermeture (état `TIME_WAIT`). En mode connecter/fermer à
cadence élevée, on peut tous les consommer : les connexions échouent alors avec
le type `locale (client)`. Ce n'est pas le serveur qui lâche. Remèdes : baisser
`--concurrency`, ou ajouter un `--hold` pour ralentir la cadence.

**Boucle asyncio.** L'outil garde la boucle par défaut de chaque système : `epoll`
sous Linux, `ProactorEventLoop` (IOCP) sous Windows. Il ne faut pas forcer
`SelectorEventLoop` sous Windows : elle repose sur `select()`, limité à 512 sockets.

## Organisation du code

| Fichier | Rôle |
|---|---|
| [stress.py](stress.py) | Point d'entrée. |
| [inventory.txt](inventory.txt) | Inventaire des machines. |
| [stresser/cli.py](stresser/cli.py) | Options de la ligne de commande, mode sans menus. |
| [stresser/menu.py](stresser/menu.py) | Les écrans : menu principal, assistant de test, inventaire, historique, réglages. |
| [stresser/ui.py](stresser/ui.py) | Briques d'interface : menu à flèches, question, cadre, message. |
| [stresser/term.py](stresser/term.py) | Terminal : couleurs, lecture des touches (Windows et Linux), dessin. |
| [stresser/guard.py](stresser/guard.py) | Plages autorisées, validation de la cible, bornes d'intensité. |
| [stresser/inventory.py](stresser/inventory.py) | Lecture de l'inventaire. |
| [stresser/config.py](stresser/config.py) | Paramètres d'un test (`RunConfig`). |
| [stresser/tcp.py](stresser/tcp.py) | Mode TCP, avec `asyncio`. |
| [stresser/udp.py](stresser/udp.py) | Mode UDP, avec deux threads et un seau à jetons. |
| [stresser/stats.py](stresser/stats.py) | Compteurs, percentiles, résumé, export CSV. |
| [stresser/display.py](stresser/display.py) | Affichage en direct : tableau de bord, ou texte simple. |
| [stresser/limits.py](stresser/limits.py) | Limites du système (fichiers ouverts, ports source). |
| [tests/](tests/) | Tests automatiques. |

**Pourquoi asyncio pour TCP et des threads pour UDP ?** En TCP, il faut tenir des
centaines de connexions qui passent leur temps à attendre : une tâche asyncio
coûte bien moins qu'un thread, et tout tourne dans un seul thread, donc sans
verrou. En UDP, il n'y a qu'une socket et ce qui compte est la régularité de la
cadence : une boucle dédiée la tient mieux.

## Tests

```powershell
python -m unittest discover -s tests -t .
```

Les tests n'envoient rien sur le réseau : les moteurs sont essayés contre de
petits serveurs locaux sur `127.0.0.1`, et les menus comme la ligne de commande
sont testés avec un faux moteur.

## Affichage

L'interface n'utilise que des séquences ANSI et la bibliothèque standard, sur
Windows comme sur Linux, y compris à travers SSH.

| Situation | Ce que fait l'outil |
|---|---|
| Terminal normal | Menus à flèches, couleurs, tableau de bord redessiné sur place. |
| Cadres ou barres mal affichés | Lancer avec `--ascii`, ou Réglages > Symboles. |
| Sortie redirigée vers un fichier, ou `--plain` | Menus numérotés, une ligne de statistiques par seconde. |
| Variable `NO_COLOR` définie | Mêmes menus, sans couleur. |
