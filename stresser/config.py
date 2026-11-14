"""Paramètres d'un test, regroupés dans un seul objet.

La CLI construit un RunConfig (après validation par guard.py), puis le passe aux
moteurs TCP/UDP et aux fonctions d'affichage. Avoir un seul objet évite de
promener une dizaine d'arguments de fonction en fonction.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass


@dataclass
class RunConfig:
    ip: ipaddress.IPv4Address   # cible, déjà validée par guard.check_target
    port: int
    proto: str                  # "tcp" ou "udp"
    duration: float             # durée du test, en secondes
    name: str = ""              # nom court de la machine (inventaire), "" si inconnue
    label: str = ""             # nom + description, pour l'affichage
    critical: bool = False      # machine marquée [critique] dans l'inventaire
    concurrency: int = 50       # TCP : nombre de connexions simultanées
    rate: int = 1000            # UDP : paquets par seconde visés
    size: int = 0               # octets envoyés par connexion (TCP) ou par paquet (UDP)
    ramp: bool = False          # TCP : montée progressive de 1 à `concurrency`
    hold: float = 0.0           # TCP : secondes pendant lesquelles on garde la connexion ouverte
    timeout: float = 3.0        # TCP : délai maximal d'établissement d'une connexion

    @property
    def display_name(self) -> str:
        """Nom à afficher dans les récapitulatifs."""
        return self.label or self.name or "hors inventaire"

    def intensity_text(self) -> str:
        """Résume l'intensité du test en une phrase."""
        if self.proto == "udp":
            return f"{self.rate} paquets/s de {self.size} octets"
        payload = f"{self.size} octets par connexion" if self.size else "connecter puis fermer"
        text = f"{self.concurrency} connexions simultanées, {payload}"
        if self.hold:
            text += f", maintien {self.hold:g} s"
        if self.ramp:
            text += ", montée progressive"
        return text
