"""Garde-fous : quelles cibles et quelles intensités sont acceptées.

C'est le module le plus important du projet. Aucune connexion ni aucun paquet ne
part tant que la cible n'est pas passée par check_target().

Règles :
- la cible doit être une adresse IPv4 écrite en chiffres (pas de nom DNS : on ne
  veut pas qu'une résolution de nom nous envoie ailleurs que prévu) ;
- elle doit appartenir à ALLOWED_NETWORKS, ou avoir été autorisée ponctuellement
  par --allow-host, ce qui n'est possible que pour une adresse privée RFC 1918 ;
- les adresses de réseau et de broadcast sont refusées : en UDP, un paquet envoyé
  au broadcast arrose toutes les machines du réseau.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable

# --- Plages autorisées -------------------------------------------------------
# Volontairement en dur dans le code, et pas dans inventory.txt : on n'élargit
# pas le périmètre de l'outil en modifiant un fichier de données par mégarde.
ALLOWED_NETWORKS = (
    ipaddress.ip_network("192.168.35.0/24"),   # réseau maison
)

# Adresses privées au sens strict (RFC 1918). On n'utilise PAS ip.is_private de
# Python, qui accepte aussi le loopback, le link-local, etc.
RFC1918_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)

# --- Intensités --------------------------------------------------------------
DEFAULT_CONCURRENCY = 50      # connexions TCP simultanées par défaut (modéré)
DEFAULT_RATE = 1000           # paquets UDP par seconde par défaut (modéré)
DEFAULT_UDP_SIZE = 64         # taille de paquet UDP par défaut, en octets
WARN_CONCURRENCY = 1000       # au-delà : avertissement
WARN_RATE = 20_000            # au-delà : avertissement

MAX_TCP_SIZE = 1024 * 1024    # charge utile TCP maximale par connexion (1 Mio)
MAX_UDP_SIZE = 65_507         # plus gros datagramme UDP possible en IPv4
UDP_NO_FRAGMENT_SIZE = 1472   # au-delà, le paquet est fragmenté (MTU Ethernet 1500)


class TargetError(ValueError):
    """Cible refusée. Le message est destiné à être affiché tel quel."""


def parse_ipv4(text: str) -> ipaddress.IPv4Address:
    """Convertit un texte en adresse IPv4, ou lève TargetError."""
    try:
        return ipaddress.IPv4Address(text.strip())
    except (ipaddress.AddressValueError, AttributeError):
        raise TargetError(
            f"« {text} » n'est pas une adresse IPv4 valide (les noms DNS ne sont pas acceptés)."
        ) from None


def is_rfc1918(ip: ipaddress.IPv4Address) -> bool:
    """Vrai si l'adresse est privée au sens de la RFC 1918."""
    return any(ip in net for net in RFC1918_NETWORKS)


def allowed_networks_text() -> str:
    """Liste lisible des plages autorisées, pour les messages."""
    return ", ".join(str(net) for net in ALLOWED_NETWORKS)


def parse_allow_hosts(values: Iterable[str] | None) -> frozenset[ipaddress.IPv4Address]:
    """Valide les adresses passées à --allow-host.

    Une adresse publique (ou loopback, link-local, multicast...) est refusée :
    --allow-host sert seulement à viser une autre machine PRIVÉE, ponctuellement.
    """
    hosts = set()
    for value in values or ():
        ip = parse_ipv4(value)
        if not is_rfc1918(ip):
            raise TargetError(
                f"--allow-host {ip} refusé : seules les adresses privées RFC 1918 "
                "(10.x.x.x, 172.16-31.x.x, 192.168.x.x) peuvent être autorisées."
            )
        # On ne connaît pas le masque du réseau visé : on suppose un /24, le cas
        # habituel en homelab, pour écarter les adresses de réseau et de broadcast.
        if ip.packed[-1] in (0, 255):
            raise TargetError(
                f"--allow-host {ip} refusé : adresse de réseau ou de broadcast."
            )
        hosts.add(ip)
    return frozenset(hosts)


def check_target(
    text: str,
    allow_hosts: frozenset[ipaddress.IPv4Address] = frozenset(),
) -> ipaddress.IPv4Address:
    """Valide une cible. Retourne l'adresse si elle est autorisée, sinon lève TargetError."""
    ip = parse_ipv4(text)

    for net in ALLOWED_NETWORKS:
        if ip in net:
            if ip in (net.network_address, net.broadcast_address):
                raise TargetError(
                    f"Cible refusée : {ip} est l'adresse de réseau ou de broadcast de {net}."
                )
            return ip

    if ip in allow_hosts:   # déjà vérifiée par parse_allow_hosts
        return ip

    if is_rfc1918(ip):
        raise TargetError(
            f"Cible refusée : {ip} est hors des plages autorisées ({allowed_networks_text()}). "
            f"Pour l'autoriser ponctuellement : --allow-host {ip}"
        )
    raise TargetError(
        f"Cible refusée : {ip} n'est pas une adresse privée de ton réseau. "
        f"Plages autorisées : {allowed_networks_text()}."
    )


def is_targetable(ip: ipaddress.IPv4Address) -> bool:
    """Vrai si l'adresse serait acceptée sans --allow-host (sert à filtrer le menu)."""
    try:
        check_target(str(ip))
    except TargetError:
        return False
    return True


def check_size(proto: str, size: int) -> None:
    """Refuse une taille de charge utile impossible ou déraisonnable."""
    limit = MAX_TCP_SIZE if proto == "tcp" else MAX_UDP_SIZE
    if size < 0 or size > limit:
        raise ValueError(
            f"--size doit être compris entre 0 et {limit} octets en {proto.upper()}."
        )


def intensity_warnings(proto: str, concurrency: int, rate: int, size: int) -> list[str]:
    """Avertissements à afficher avant la confirmation quand l'intensité est forte."""
    warnings = []
    if proto == "tcp" and concurrency > WARN_CONCURRENCY:
        warnings.append(
            f"Concurrence élevée ({concurrency} > {WARN_CONCURRENCY}) : le service visé "
            "risque de ne plus répondre pendant le test."
        )
    if proto == "udp" and rate > WARN_RATE:
        warnings.append(
            f"Débit élevé ({rate} > {WARN_RATE} paquets/s) : le service visé, ou le "
            "réseau entre toi et lui, risque de saturer."
        )
    if proto == "udp" and size > UDP_NO_FRAGMENT_SIZE:
        warnings.append(
            f"Paquets de {size} octets : au-delà de {UDP_NO_FRAGMENT_SIZE}, chaque paquet "
            "est fragmenté en plusieurs trames (MTU 1500)."
        )
    return warnings
