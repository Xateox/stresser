"""Chargement de l'inventaire (fichier texte : IP, nom, description).

Le format est décrit en tête de inventory.txt. Une ligne mal formée n'empêche pas
l'outil de démarrer : elle est ignorée et signalée par un avertissement.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from pathlib import Path

# Marqueur à poser sur les machines dont dépendent d'autres services.
_CRITICAL_TAG = re.compile(r"\[critique\]", re.IGNORECASE)


@dataclass(frozen=True)
class Machine:
    ip: ipaddress.IPv4Address
    name: str                 # premier mot après l'IP, ex. "opnsense"
    description: str = ""     # le reste de la ligne, ex. "(WAN / GUI 8443)"
    critical: bool = False

    @property
    def label(self) -> str:
        """Nom complet pour l'affichage : nom + description."""
        return f"{self.name} {self.description}".strip()


def parse_inventory(text: str) -> tuple[list[Machine], list[str]]:
    """Analyse le contenu d'un inventaire. Retourne (machines, avertissements)."""
    machines: list[Machine] = []
    warnings: list[str] = []
    seen: set[ipaddress.IPv4Address] = set()

    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        critical = bool(_CRITICAL_TAG.search(line))
        line = _CRITICAL_TAG.sub("", line)

        parts = line.split(None, 2)   # IP, nom, puis toute la description
        if len(parts) < 2:
            warnings.append(f"inventaire, ligne {number} ignorée : il faut « IP nom ».")
            continue
        try:
            ip = ipaddress.IPv4Address(parts[0])
        except ipaddress.AddressValueError:
            warnings.append(f"inventaire, ligne {number} ignorée : IP invalide « {parts[0]} ».")
            continue
        if ip in seen:
            warnings.append(f"inventaire, ligne {number} ignorée : {ip} est déjà déclarée.")
            continue

        seen.add(ip)
        description = " ".join(parts[2].split()) if len(parts) == 3 else ""
        machines.append(Machine(ip, parts[1], description, critical))

    return machines, warnings


def load_inventory(path: Path) -> tuple[list[Machine], list[str]]:
    """Lit le fichier d'inventaire. S'il manque, l'outil reste utilisable en tapant une IP."""
    try:
        # utf-8-sig : tolère le BOM qu'ajoute parfois le Bloc-notes de Windows.
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        return [], [f"inventaire illisible ({path}) : {exc.strerror or exc}."]
    return parse_inventory(text)


def find_by_ip(machines: list[Machine], ip: ipaddress.IPv4Address) -> Machine | None:
    """Retrouve une machine par son adresse, ou None si elle n'est pas dans l'inventaire."""
    for machine in machines:
        if machine.ip == ip:
            return machine
    return None
