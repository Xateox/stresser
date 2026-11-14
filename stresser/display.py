"""Affichage en direct pendant le test.

Deux modes, avec la même interface (start / update / stop) :

- RichDisplay  : un tableau qui se met à jour sur place, grâce à la bibliothèque
                 `rich`. Utilisé seulement si elle est installée et si la sortie
                 est un vrai terminal.
- PlainDisplay : une ligne de texte par seconde. Aucune dépendance, fonctionne
                 partout (cmd classique, sortie redirigée vers un fichier, SSH).

Le résumé final, lui, est toujours écrit en texte simple avec print().
"""

from __future__ import annotations

import sys

# Une « mesure » affichée : (libellé, valeur déjà mise en forme).
Fields = list[tuple[str, str]]


class PlainDisplay:
    """Repli universel : une ligne par rafraîchissement."""

    def start(self, title: str) -> None:
        print(title, flush=True)

    def update(self, fields: Fields) -> None:
        print(" | ".join(f"{label} {value}" for label, value in fields), flush=True)

    def stop(self) -> None:
        pass


class RichDisplay:
    """Tableau mis à jour sur place avec `rich`."""

    def __init__(self) -> None:
        # Imports locaux : le reste de l'outil ne dépend pas de rich.
        from rich.console import Console
        from rich.live import Live

        self._console = Console()
        self._live = Live(console=self._console, auto_refresh=False)
        self._title = ""

    def start(self, title: str) -> None:
        self._title = title
        self._live.start()

    def update(self, fields: Fields) -> None:
        from rich.table import Table

        table = Table(title=self._title, show_header=False, title_justify="left")
        table.add_column(style="bold")
        table.add_column(justify="right")
        for label, value in fields:
            table.add_row(label, value)
        self._live.update(table, refresh=True)

    def stop(self) -> None:
        self._live.stop()


def make_display(plain: bool = False) -> PlainDisplay | RichDisplay:
    """Choisit l'affichage : rich si possible, sinon texte simple.

    `plain=True` (option --plain) force le texte simple.
    """
    if plain or not sys.stdout.isatty():
        return PlainDisplay()
    try:
        return RichDisplay()
    except ImportError:
        return PlainDisplay()
