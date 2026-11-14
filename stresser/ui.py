"""Briques d'interface : écran, menu à flèches, question, message.

La classe UI a deux modes, choisis au démarrage :

- interactif  : le terminal comprend les séquences ANSI et le clavier est lisible
                touche par touche. Les menus se parcourent avec les flèches et
                sont redessinés sur place.
- texte simple : sortie redirigée, option --plain, ou terminal limité. Les menus
                deviennent des listes numérotées et on répond avec input().

Les écrans (menu.py) appellent les mêmes méthodes dans les deux cas : ils ne
savent pas dans quel mode ils tournent.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass

from . import __version__, guard, term

BACK = "<"      # réponse à taper dans une question pour revenir en arrière


@dataclass
class Option:
    """Une ligne de menu."""
    label: str
    value: object = None    # ce que select() retourne si la ligne est choisie
    detail: str = ""        # précision affichée en gris à droite du libellé
    tag: str = ""           # étiquette de mise en garde, ex. « critique »
    hint: str = ""          # aide affichée sous le menu quand la ligne est sélectionnée


class UI:
    def __init__(self, theme: term.Theme, interactive: bool,
                 read_key: Callable[[], str] = term.read_key) -> None:
        self.theme = theme
        self.interactive = interactive
        self._read_key = read_key
        self._lines = 0     # lignes écrites depuis le dernier effacement d'écran

    # -- Écriture --
    def write(self, text: str = "") -> None:
        print(text)
        self._lines += text.count("\n") + 1

    def _raw(self, text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()

    def screen(self, context: list[str] | None = None) -> None:
        """Commence un nouvel écran : bandeau du programme, puis lignes de contexte."""
        if self.interactive:
            self._raw(term.CLEAR_SCREEN)
            self._lines = 0
            self.banner()
        else:
            self.write()
        for line in context or []:
            for part in term.wrap(line, term.size()[0] - 4):
                self.write("  " + self.theme.paint(part, "muted"))
        if context:
            self.write()

    def banner(self) -> None:
        theme = self.theme
        width = term.size()[0]
        name = theme.paint("STRESSER", "bold", "accent")
        version = f"v{__version__}"
        subtitle = "test de charge du homelab"
        gap = " " * max(1, width - 4 - len("STRESSER") - 3 - len(subtitle) - len(version))
        lines = [
            f"{name}   {subtitle}{gap}{theme.paint(version, 'muted')}",
            theme.paint(f"Plage autorisée : {guard.allowed_networks_text()}", "muted"),
        ]
        for line in term.box(lines, theme, width):
            self.write(line)
        self.write()

    def title(self, text: str) -> None:
        self.write("  " + self.theme.paint(text, "bold"))
        self.write()

    def notice(self, text: str, kind: str = "warn") -> None:
        """Message mis en avant. kind : "ok", "warn" ou "err"."""
        symbol = self.theme.paint(self.theme[kind], kind, "bold")
        width = term.size()[0] - 6
        for index, line in enumerate(term.wrap(text, width)):
            self.write(f"  {symbol if index == 0 else ' '} {line}")

    def panel(self, lines: list[str], title: str = "") -> None:
        """Encadre des lignes déjà mises en forme."""
        for line in term.box(lines, self.theme, term.size()[0], title=title):
            self.write(line)

    def pause(self, message: str = "Entrée pour continuer") -> None:
        self.write()
        if self.interactive:
            self._raw("  " + self.theme.paint(message, "muted") + " ")
            with term.key_mode():
                while self._read_key() not in (term.ENTER, term.ESCAPE, " ", "q"):
                    pass
            self.write()
        else:
            input(f"  {message} ")

    # -- Question à réponse libre --
    def ask(self, prompt: str, *, default=None, parse: Callable[[str], object] = str,
            show_default: str | None = None, back_hint: str | None = None):
        """Pose une question. Retourne la valeur convertie par `parse`, ou None pour « retour ».

        Entrée seule garde la valeur par défaut (ou revient en arrière s'il n'y en
        a pas). `parse` lève ValueError pour refuser une réponse : le message est
        affiché et la question reposée.
        """
        if show_default is not None:
            shown = show_default
        elif default is None:
            shown = None
        elif isinstance(default, float):
            shown = f"{default:g}"      # 10.0 s'affiche « 10 »
        else:
            shown = str(default)
        suffix = f" [{shown}]" if shown is not None else ""
        if back_hint is None:
            back_hint = f"({BACK} pour revenir)" if shown is not None else f"(vide ou {BACK} pour revenir)"
        label = f"  {self.theme.paint(prompt, 'bold')}{suffix} {self.theme.paint(back_hint, 'muted')} : "
        while True:
            raw = input(label).strip()
            self._lines += 1
            if raw == BACK or (raw == "" and default is None):
                return None
            if raw == "":
                return default
            try:
                return parse(raw)
            except ValueError as exc:
                self.notice(str(exc), "err")

    # -- Menu --
    def select(self, title: str, options: list[Option], *, start: int = 0, allow_back: bool = True):
        """Affiche un menu et retourne la valeur de l'option choisie, ou None pour « retour »."""
        if not self.interactive:
            return self._select_plain(title, options, allow_back)
        with term.key_mode():
            return self._select_keys(title, options, start, allow_back)

    def _option_text(self, option: Option, label_width: int) -> str:
        """Libellé + étiquette + détail d'une option (sans le marqueur de sélection)."""
        text = term.pad(option.label, label_width)
        if option.detail:
            text += "  " + self.theme.paint(option.detail, "muted")
        if option.tag:
            text += "  " + self.theme.paint(f"[{option.tag}]", "warn")
        return text

    def _select_plain(self, title: str, options: list[Option], allow_back: bool):
        self.title(title)
        label_width = max(len(option.label) for option in options)
        for number, option in enumerate(options, start=1):
            self.write(f"  {number:2d}) {self._option_text(option, label_width)}")
        if allow_back:
            self.write("   0) Retour")
        while True:
            raw = input("  Choix : ").strip()
            if allow_back and raw in ("0", BACK):
                return None
            if raw.isdigit() and 1 <= int(raw) <= len(options):
                return options[int(raw) - 1].value
            self.write(f"  Tape un numéro entre {0 if allow_back else 1} et {len(options)}.")

    def _select_keys(self, title: str, options: list[Option], start: int, allow_back: bool):
        theme = self.theme
        index = min(max(start, 0), len(options) - 1)
        label_width = max(len(option.label) for option in options)
        width, rows = term.size()
        # Nombre d'options visibles : s'il y en a trop pour l'écran, le menu défile.
        visible = max(3, min(len(options), rows - self._lines - 7))
        top = 0
        drawn = 0
        self._raw(term.HIDE_CURSOR)
        try:
            while True:
                top = min(max(top, index - visible + 1), index)     # garde la sélection à l'écran
                lines = ["  " + theme.paint(title, "bold"), ""]
                if top > 0:
                    lines.append(theme.paint(f"      {theme['up']} {top} de plus", "muted"))
                for position in range(top, min(top + visible, len(options))):
                    option = options[position]
                    number = str(position + 1) if position < 9 else " "
                    text = self._option_text(option, label_width)
                    if position == index:
                        text = theme.paint(term.pad(option.label, label_width), "bold", "accent") + text[label_width:]
                        marker = theme.paint(theme["pointer"], "accent", "bold")
                    else:
                        marker = " "
                    lines.append(f"  {marker} {theme.paint(number, 'muted')}  {text}")
                below = len(options) - top - visible
                if below > 0:
                    lines.append(theme.paint(f"      {theme['down']} {below} de plus", "muted"))
                lines.append("")
                hint_lines = term.wrap(options[index].hint, width - 4) if options[index].hint else [""]
                lines += ["  " + theme.paint(line, "muted") for line in hint_lines]
                keys = f"{theme['up']}{theme['down']} naviguer  {theme['dot']}  Entrée valider"
                if allow_back:
                    keys += f"  {theme['dot']}  Échap retour"
                lines += ["", "  " + theme.paint(keys, "muted")]

                output = term.cursor_up(drawn) + "".join(term.CLEAR_LINE + line + "\n" for line in lines)
                if len(lines) < drawn:
                    output += term.CLEAR_BELOW
                self._raw(output)
                drawn = len(lines)

                key = self._read_key()
                if key == term.UP or key == "k":
                    index = (index - 1) % len(options)
                elif key == term.DOWN or key == "j":
                    index = (index + 1) % len(options)
                elif key == term.HOME:
                    index = 0
                elif key == term.END:
                    index = len(options) - 1
                elif key in (term.ENTER, term.RIGHT, " "):
                    return options[index].value
                elif key in (term.ESCAPE, term.LEFT, term.BACKSPACE, "q") and allow_back:
                    return None
                elif key.isdigit() and 1 <= int(key) <= min(9, len(options)):
                    return options[int(key) - 1].value      # raccourci : le numéro de la ligne
        finally:
            self._lines += drawn
            self._raw(term.SHOW_CURSOR)
