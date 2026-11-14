# stresser : test de charge TCP/UDP pour mon homelab

Outil en ligne de commande qui mesure comment un service de **mon propre réseau**
réagit à beaucoup de connexions TCP ou de paquets UDP : débit, latence, taux d'erreur.

> **Réservé à mes machines.** L'outil refuse toute cible hors de la plage
> `192.168.35.0/24`. Tester une machine qui ne m'appartient pas, sans accord écrit
> de son propriétaire, est illégal.

## Installation

Il faut Python 3.11 ou plus récent. L'outil n'a aucune dépendance obligatoire ;
`rich` (optionnel) ajoute un tableau mis à jour en direct.

**Windows (PowerShell)**

```powershell
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt    # optionnel
python stress.py --help
```

Sous `cmd`, la commande d'activation est la même. Si PowerShell refuse d'exécuter
le script d'activation, lancer une fois
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`. Aucun droit administrateur
ni WSL n'est nécessaire.

**Linux**

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt    # optionnel
python3 stress.py --help
```

## Utilisation

### Mode interactif

```powershell
python stress.py
```

L'outil liste les machines de l'inventaire. Je choisis un numéro (ou je tape une
IP), puis il demande le port, le protocole, la durée et l'intensité. Entrée seule
garde la valeur par défaut indiquée entre crochets. Les options déjà passées sur
la ligne de commande ne sont pas redemandées.

### Mode non interactif

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
| `-t IP` | Cible. Sans elle : menu interactif. | menu |
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
| `--plain` | Affichage en texte simple, même si `rich` est installé. | non |
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
  durée) puis accord explicite, sauf avec `--yes`.
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
| Latence de connexion | Temps de la poignée de main. `p95` = 95 % des connexions ont été plus rapides ; c'est lui qui montre les ralentissements que la moyenne cache. |
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
| [stresser/cli.py](stresser/cli.py) | Options, menu interactif, récapitulatif, confirmation. |
| [stresser/guard.py](stresser/guard.py) | Plages autorisées, validation de la cible, bornes d'intensité. |
| [stresser/inventory.py](stresser/inventory.py) | Lecture de l'inventaire. |
| [stresser/config.py](stresser/config.py) | Paramètres d'un test (`RunConfig`). |
| [stresser/tcp.py](stresser/tcp.py) | Mode TCP, avec `asyncio`. |
| [stresser/udp.py](stresser/udp.py) | Mode UDP, avec deux threads et un seau à jetons. |
| [stresser/stats.py](stresser/stats.py) | Compteurs, percentiles, résumé, export CSV. |
| [stresser/display.py](stresser/display.py) | Affichage en direct (`rich`, ou texte simple). |
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
petits serveurs locaux sur `127.0.0.1`, et la ligne de commande est testée avec
un faux moteur.

## Affichage

Avec `rich` installé et un vrai terminal, les statistiques s'affichent dans un
tableau mis à jour sur place chaque seconde. Sans `rich`, avec `--plain`, ou si la
sortie est redirigée vers un fichier, l'outil écrit une ligne de texte par
seconde. Si le tableau s'affiche mal dans une console (cmd ancienne par exemple),
utiliser `--plain`.
