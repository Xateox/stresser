"""Affichage en direct pendant le test.

Deux modes, avec la même interface (start / update / stop) :

- DashboardDisplay : un tableau de bord redessiné sur place chaque seconde (barre
                     d'avancement, compteurs, mini-graphique du débit). Demande un
                     terminal qui comprend les séquences ANSI.
- PlainDisplay     : une ligne de texte par seconde. Fonctionne partout : sortie
                     redirigée vers un fichier, option --plain, terminal limité.

Les moteurs (tcp.py, udp.py) appellent update() une fois par seconde avec une
liste de mesures déjà mises en forme : [(libellé, valeur), ...].
"""

from __future__ import annotations

import sys
import time

from . import term
from .config import RunConfig

# Une « mesure » affichée : (libellé, valeur déjà mise en forme).
Fields = list[tuple[str, str]]

# Libellés complets du tableau de bord (les moteurs utilisent des libellés courts).
LABELS = {
    "ouvertes": "Ouvertes", "tentées": "Tentées", "réussies": "Réussies", "échouées": "Échouées",
    "conn/s": "Connexions/s", "p50": "Latence p50", "p95": "Latence p95",
    "envoyé": "Envoyé", "reçu": "Reçu", "envoyés": "Paquets envoyés", "paquets/s": "Paquets/s",
    "Mbit/s": "Mbit/s", "reçus": "Paquets reçus", "port injoignable": "Port injoignable",
}
RATE_FIELDS = ("conn/s", "paquets/s")   # la mesure tracée par le mini-graphique
MIN_DASHBOARD_WIDTH = 56


class PlainDisplay:
    """Repli universel : une ligne par rafraîchissement."""

    def start(self, title: str) -> None:
        print(title, flush=True)

    def update(self, fields: Fields) -> None:
        print(" | ".join(f"{label} {value}" for label, value in fields), flush=True)

    def stop(self) -> None:
        pass


class DashboardDisplay:
    """Tableau de bord redessiné sur place.

    Principe : après le premier dessin, on remonte le curseur du nombre de lignes
    déjà écrites et on réécrit par-dessus. À la fin du test, le tableau est effacé
    pour laisser la place au résumé.
    """

    def __init__(self, cfg: RunConfig, theme: term.Theme, stream=None) -> None:
        self._cfg = cfg
        self._theme = theme
        self._stream = stream or sys.stdout
        self._started = 0.0
        self._fields: Fields = []
        self._rates: list[float] = []   # historique du débit, pour le mini-graphique
        self._drawn = 0                 # nombre de lignes du dernier dessin

    # -- Interface appelée par les moteurs --
    def start(self, title: str) -> None:
        self._started = time.perf_counter()
        self._stream.write(term.HIDE_CURSOR)
        self._draw()

    def update(self, fields: Fields) -> None:
        self._fields = fields
        for label, value in fields:
            if label in RATE_FIELDS:
                try:
                    self._rates.append(float(value))
                except ValueError:
                    pass
        self._draw()

    def stop(self) -> None:
        # Efface le tableau de bord : le résumé final le remplace.
        self._stream.write(term.cursor_up(self._drawn) + term.CLEAR_BELOW + term.SHOW_CURSOR)
        self._stream.flush()
        self._drawn = 0

    # -- Dessin --
    def frame(self) -> list[str]:
        """Les lignes du tableau de bord, prêtes à écrire."""
        theme, cfg = self._theme, self._cfg
        width = min(term.size()[0], 72)
        inner = width - 4

        elapsed = min(time.perf_counter() - self._started, cfg.duration) if self._started else 0.0
        ratio = elapsed / cfg.duration if cfg.duration else 1.0
        counter = f"  {elapsed:.0f} s / {cfg.duration:g} s  {theme['dot']}  {ratio * 100:.0f} %"
        lines = [term.progress_bar(ratio, max(10, inner - len(counter)), theme) + counter, ""]

        # Les mesures, sur trois colonnes : libellé en gris, valeur en gras.
        cells = [(LABELS.get(label, label), value) for label, value in self._fields if label != "temps"]
        if not cells:
            lines.append(theme.paint("Démarrage, première mesure dans une seconde" + theme["ellipsis"], "muted"))
        columns = 3
        cell_width = inner // columns
        for index in range(0, len(cells), columns):
            row = ""
            for label, value in cells[index:index + columns]:
                label = term.truncate(label, cell_width - len(value) - 2, theme["ellipsis"])
                gap = " " * max(1, cell_width - len(label) - len(value) - 2)
                row += theme.paint(label, "muted") + gap + theme.paint(value, "bold") + "  "
            lines.append(row.rstrip())

        if self._rates:
            name = next(LABELS[label] for label, _ in self._fields if label in RATE_FIELDS)
            peak = f"  max {max(self._rates):.0f}"
            graph = term.sparkline(self._rates, inner - len(name) - len(peak) - 2, theme)
            lines += ["", theme.paint(name, "muted") + "  " + theme.paint(graph, "accent") + theme.paint(peak, "muted")]

        title = f"Test {cfg.proto.upper()} {theme['dot']} {cfg.display_name} {theme['dot']} {cfg.ip}:{cfg.port}"
        return term.box(lines, theme, width, title=title, footer="Ctrl+C pour arrêter")

    def _draw(self) -> None:
        lines = self.frame()
        output = term.cursor_up(self._drawn)
        output += "".join(term.CLEAR_LINE + line + "\n" for line in lines)
        if len(lines) < self._drawn:
            output += term.CLEAR_BELOW      # l'ancien dessin était plus haut : on efface le reste
        self._stream.write(output)
        self._stream.flush()
        self._drawn = len(lines)


def make_display(cfg: RunConfig, theme: term.Theme | None = None, ansi: bool = False):
    """Choisit l'affichage : tableau de bord si le terminal le permet, sinon texte simple."""
    if ansi and theme is not None and term.size()[0] >= MIN_DASHBOARD_WIDTH:
        return DashboardDisplay(cfg, theme)
    return PlainDisplay()
