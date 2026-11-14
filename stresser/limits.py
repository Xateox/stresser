"""Limites du système qui plafonnent le générateur de charge (côté client).

Deux limites comptent pour le mode TCP :

1. Le nombre de sockets ouvertes en même temps.
   - Linux : limité par `ulimit -n` (souvent 1024). On relève la limite souple
     jusqu'à la limite dure au démarrage, ce qui ne demande aucun droit.
   - Windows : pas de limite basse de ce type avec la boucle asyncio par défaut.

2. Le nombre de ports source « éphémères ». Chaque connexion sortante en consomme
   un, et le système le garde réservé un moment après la fermeture (état
   TIME_WAIT). En mode connecter/fermer à cadence élevée, on peut tous les
   consommer : les erreurs viennent alors du client, pas du serveur testé.
   - Linux : plage lue dans /proc (environ 28 000 ports par défaut).
   - Windows : 16 384 ports par défaut (49152 à 65535).
"""

from __future__ import annotations

import sys

_FD_MARGIN = 64                 # descripteurs gardés pour Python lui-même (fichiers, console...)
_WINDOWS_EPHEMERAL = 16_384     # valeur par défaut de Windows
_LINUX_PORT_RANGE = "/proc/sys/net/ipv4/ip_local_port_range"


def raise_fd_limit() -> int | None:
    """Relève la limite de fichiers ouverts au maximum permis (Unix).

    Retourne la limite en vigueur après l'appel, ou None sous Windows, où le
    module `resource` n'existe pas.
    """
    try:
        import resource   # module Unix uniquement, d'où l'import local
    except ImportError:
        return None

    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    wanted = 65_536 if hard == resource.RLIM_INFINITY else hard
    if soft == resource.RLIM_INFINITY or soft >= wanted:
        return None if soft == resource.RLIM_INFINITY else soft
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (wanted, hard))
    except (ValueError, OSError):
        return soft   # refusé par le système : on garde l'ancienne limite
    return wanted


def ephemeral_port_count() -> int | None:
    """Nombre de ports source disponibles pour les connexions sortantes, si connu."""
    if sys.platform == "win32":
        return _WINDOWS_EPHEMERAL
    try:
        with open(_LINUX_PORT_RANGE, encoding="ascii") as handle:
            low, high = (int(value) for value in handle.read().split())
    except (OSError, ValueError):
        return None
    return high - low + 1


def system_warnings(proto: str, concurrency: int) -> list[str]:
    """Avertissements liés aux limites du client, à afficher avant la confirmation."""
    if proto != "tcp":
        return []

    warnings = []
    fd_limit = raise_fd_limit()
    if fd_limit is not None and concurrency > fd_limit - _FD_MARGIN:
        warnings.append(
            f"Limite de fichiers ouverts du système : {fd_limit}. Avec {concurrency} "
            "connexions simultanées, une partie échouera côté client (erreur « locale »). "
            "Augmente-la avec `ulimit -n` avant de lancer l'outil."
        )
    ports = ephemeral_port_count()
    if ports is not None and concurrency > ports * 0.8:
        warnings.append(
            f"Seulement {ports} ports source disponibles sur cette machine : "
            f"{concurrency} connexions simultanées vers la même cible n'y tiendront pas."
        )
    return warnings
