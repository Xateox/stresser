"""Primitives du terminal : couleurs, cadres, lecture des touches.

Tout est fait avec la bibliothèque standard, pour que l'outil reste sans
dépendance et fonctionne pareil sous Windows (PowerShell, cmd) et sous Linux.

Trois notions à connaître pour lire ce fichier :

- Séquences ANSI : des suites de caractères commençant par ESC (\\x1b) que le
  terminal n'affiche pas mais exécute : changer de couleur, déplacer le curseur,
  effacer une ligne. Sous Windows, la console classique ne les comprend que si on
  le lui demande (voir enable_ansi).
- Thème : regroupe les couleurs et les symboles. Sans couleur ou sans Unicode
  (option --ascii, variable NO_COLOR), on change de thème, pas le code d'affichage.
- Touches : lire UNE touche sans attendre Entrée dépend du système. Sous Windows
  on utilise msvcrt ; sous Linux on passe le terminal en mode « cbreak » avec
  termios. Les flèches arrivent sous forme de plusieurs octets à décoder.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import sys

# Noms des touches spéciales retournés par read_key()
UP, DOWN, LEFT, RIGHT = "up", "down", "left", "right"
ENTER, ESCAPE, HOME, END, BACKSPACE = "enter", "escape", "home", "end", "backspace"

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
MAX_WIDTH = 78      # largeur maximale de l'interface, même sur un terminal très large


# --- Capacités du terminal ----------------------------------------------------

def enable_ansi(stream=None) -> bool:
    """Vrai si le terminal comprend les séquences ANSI (en les activant sous Windows)."""
    stream = stream or sys.stdout
    if not hasattr(stream, "isatty") or not stream.isatty():
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    if sys.platform != "win32":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)     # -11 = sortie standard de la console
        mode = ctypes.c_ulong()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        vt_processing = 0x0004                  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        if mode.value & vt_processing:
            return True
        return bool(kernel32.SetConsoleMode(handle, mode.value | vt_processing))
    except (AttributeError, OSError):
        return False


def supports_unicode(stream=None) -> bool:
    """Vrai si la sortie sait écrire les caractères de dessin (cadres, barres)."""
    encoding = (getattr(stream or sys.stdout, "encoding", "") or "").lower()
    return encoding.startswith("utf")


def size() -> tuple[int, int]:
    """(largeur utilisable, hauteur) du terminal. La largeur est plafonnée à MAX_WIDTH."""
    columns, rows = shutil.get_terminal_size((80, 24))
    return max(40, min(columns - 1, MAX_WIDTH)), max(10, rows)


# --- Thème : couleurs et symboles ------------------------------------------------

_COLORS = {
    "bold": "1",
    "accent": "38;5;75",    # bleu : titres, élément sélectionné
    "ok": "38;5;78",        # vert
    "warn": "38;5;214",     # orange
    "err": "38;5;203",      # rouge
    "muted": "38;5;245",    # gris : aides, détails
    "reverse": "7",
}

_UNICODE_GLYPHS = {
    "tl": "┌", "tr": "┐", "bl": "└", "br": "┘", "h": "─", "v": "│",
    "pointer": "›", "dot": "·", "ok": "✓", "err": "✕", "warn": "!",
    "full": "█", "empty": "░", "up": "↑", "down": "↓", "ellipsis": "…",
    "spark": "▁▂▃▄▅▆▇█",
}
_ASCII_GLYPHS = {
    "tl": "+", "tr": "+", "bl": "+", "br": "+", "h": "-", "v": "|",
    "pointer": ">", "dot": "-", "ok": "v", "err": "x", "warn": "!",
    "full": "#", "empty": ".", "up": "^", "down": "v", "ellipsis": "...",
    "spark": "_.-=#",
}


class Theme:
    """Couleurs et symboles utilisés par toute l'interface."""

    def __init__(self, color: bool = True, unicode: bool = True) -> None:
        self.color = color
        self.unicode = unicode
        self.glyphs = _UNICODE_GLYPHS if unicode else _ASCII_GLYPHS

    def __getitem__(self, name: str) -> str:
        """theme["pointer"] retourne le symbole correspondant."""
        return self.glyphs[name]

    def paint(self, text: str, *styles: str) -> str:
        """Entoure le texte des séquences ANSI demandées (ou le rend tel quel sans couleur)."""
        if not self.color or not styles:
            return text
        codes = ";".join(_COLORS[style] for style in styles)
        return f"\x1b[{codes}m{text}\x1b[0m"


def detect(plain: bool = False, ascii_only: bool = False) -> tuple[Theme, bool]:
    """Choisit le thème adapté au terminal. Retourne (thème, ANSI disponible).

    Sans ANSI (sortie redirigée, option --plain), l'outil écrit du texte simple :
    pas de couleur, pas de réécriture sur place, menus numérotés.
    """
    ansi = not plain and enable_ansi()
    color = ansi and "NO_COLOR" not in os.environ   # convention https://no-color.org
    return Theme(color=color, unicode=supports_unicode() and not ascii_only), ansi


# --- Mise en forme ------------------------------------------------------------------

def visible_len(text: str) -> int:
    """Longueur affichée d'un texte, sans compter les séquences ANSI."""
    return len(_ANSI.sub("", text))


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


def pad(text: str, width: int) -> str:
    """Complète le texte avec des espaces jusqu'à `width` colonnes affichées."""
    return text + " " * max(0, width - visible_len(text))


def truncate(text: str, width: int, ellipsis: str = "…") -> str:
    """Coupe un texte SANS couleur pour qu'il tienne dans `width` colonnes."""
    if len(text) <= width:
        return text
    return text[: max(0, width - len(ellipsis))] + ellipsis


def wrap(text: str, width: int) -> list[str]:
    """Coupe un texte sans couleur en lignes d'au plus `width` colonnes, aux espaces."""
    lines: list[str] = []
    current = ""
    for word in text.split():
        if current and len(current) + 1 + len(word) > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    return lines + [current] if current else lines or [""]


def box(lines: list[str], theme: Theme, width: int, title: str = "", footer: str = "") -> list[str]:
    """Dessine un cadre de `width` colonnes autour de lignes déjà mises en forme."""
    inner = width - 4   # bordure + une espace de chaque côté

    def border(left: str, right: str, label: str) -> str:
        label = f" {truncate(label, inner - 2)} " if label else ""
        fill = theme["h"] * (width - 3 - len(label))
        return theme.paint(theme[left] + theme["h"], "muted") + theme.paint(label, "bold") \
            + theme.paint(fill + theme[right], "muted")

    side = theme.paint(theme["v"], "muted")
    framed = [border("tl", "tr", title)]
    framed += [f"{side} {pad(line, inner)} {side}" for line in lines]
    framed.append(border("bl", "br", footer))
    return framed


def progress_bar(ratio: float, width: int, theme: Theme) -> str:
    """Barre d'avancement de `width` colonnes ; `ratio` va de 0 à 1."""
    ratio = min(1.0, max(0.0, ratio))
    filled = round(ratio * width)
    return theme.paint(theme["full"] * filled, "accent") + theme.paint(theme["empty"] * (width - filled), "muted")


def sparkline(values: list[float], width: int, theme: Theme) -> str:
    """Mini-graphique d'une ligne : une colonne par valeur, la hauteur suit la valeur."""
    levels = theme["spark"]
    values = values[-width:]
    top = max(values, default=0)
    if top <= 0:
        return levels[0] * len(values)
    return "".join(levels[min(len(levels) - 1, int(value / top * (len(levels) - 1) + 0.5))] for value in values)


# --- Écran ---------------------------------------------------------------------------

CLEAR_SCREEN = "\x1b[2J\x1b[H"      # efface l'écran et remet le curseur en haut à gauche
CLEAR_BELOW = "\x1b[J"              # efface du curseur jusqu'en bas
CLEAR_LINE = "\x1b[2K"
HIDE_CURSOR = "\x1b[?25l"
SHOW_CURSOR = "\x1b[?25h"


def cursor_up(count: int) -> str:
    """Séquence qui remonte le curseur de `count` lignes, en début de ligne."""
    return f"\x1b[{count}A\r" if count > 0 else "\r"


# --- Clavier ------------------------------------------------------------------------

@contextlib.contextmanager
def key_mode():
    """Prépare le terminal à la lecture touche par touche, puis le remet en état.

    À utiliser autour de toute boucle qui appelle read_key() :

        with term.key_mode():
            key = term.read_key()

    Sous Linux, le terminal passe en mode « cbreak » : les touches arrivent une par
    une, sans attendre Entrée et sans s'afficher, mais Ctrl+C reste actif. On garde
    ce mode pendant TOUTE la boucle : en le changeant à chaque touche, une frappe
    rapide tomberait entre deux lectures et serait perdue ou affichée à l'écran.
    Sous Windows, il n'y a rien à préparer.
    """
    if sys.platform == "win32":
        yield
        return
    import termios
    import tty

    try:
        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
    except (termios.error, OSError, ValueError, AttributeError):
        yield       # l'entrée n'est pas un terminal : rien à régler
        return
    try:
        # TCSANOW : appliquer tout de suite, SANS jeter les touches déjà tapées.
        tty.setcbreak(fd, termios.TCSANOW)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def read_key() -> str:
    """Attend UNE touche et retourne son nom (UP, ENTER...) ou le caractère tapé.

    À appeler à l'intérieur de `with key_mode():`. Lève KeyboardInterrupt sur
    Ctrl+C et EOFError sur Ctrl+D / Ctrl+Z.
    """
    return _read_key_windows() if sys.platform == "win32" else _read_key_unix()


def _read_key_windows() -> str:
    import msvcrt

    char = msvcrt.getwch()
    if char in ("\x00", "\xe0"):
        # Touche spéciale : un préfixe, puis un code. Flèches = H, P, K, M.
        code = msvcrt.getwch()
        return {"H": UP, "P": DOWN, "K": LEFT, "M": RIGHT, "G": HOME, "O": END}.get(code, "")
    if char == "\x03":      # getwch() lit Ctrl+C comme un caractère : on rétablit l'interruption
        raise KeyboardInterrupt
    if char == "\x1a":      # Ctrl+Z
        raise EOFError
    return {"\r": ENTER, "\n": ENTER, "\x1b": ESCAPE, "\x08": BACKSPACE}.get(char, char)


_UNIX_SEQUENCES = {
    b"[A": UP, b"[B": DOWN, b"[C": RIGHT, b"[D": LEFT, b"[H": HOME, b"[F": END,
    b"OA": UP, b"OB": DOWN, b"OC": RIGHT, b"OD": LEFT, b"OH": HOME, b"OF": END,
    b"[1~": HOME, b"[4~": END,
}


def _read_key_unix() -> str:
    import select

    fd = sys.stdin.fileno()
    first = os.read(fd, 1)
    if first == b"\x1b":
        # Une flèche envoie ESC suivi de deux ou trois octets, tout de suite. Si
        # rien ne suit dans les 50 ms, c'est la touche Échap seule.
        if not select.select([fd], [], [], 0.05)[0]:
            return ESCAPE
        sequence = os.read(fd, 2)
        if sequence[-1:].isdigit():     # forme longue, ex. ESC [ 1 ~
            sequence += os.read(fd, 1)
        return _UNIX_SEQUENCES.get(sequence, "")
    if first in (b"\r", b"\n"):
        return ENTER
    if first in (b"\x7f", b"\x08"):
        return BACKSPACE
    if first in (b"", b"\x04"):     # fin de l'entrée, ou Ctrl+D
        raise EOFError
    # Caractère accentué : l'UTF-8 le code sur plusieurs octets, on lit la suite.
    extra = 3 if first[0] >= 0xF0 else 2 if first[0] >= 0xE0 else 1 if first[0] >= 0xC0 else 0
    if extra:
        first += os.read(fd, extra)
    return first.decode("utf-8", errors="ignore")
