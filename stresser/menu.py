"""L'application à menus : ce qui s'affiche quand on lance `python stress.py` sans option.

Organisation :
- Session : ce que l'application retient tant qu'elle est ouverte (inventaire,
  réglages, dernier test, historique).
- Draft   : le test en cours de préparation, rempli étape par étape.
- App     : les écrans. Chaque écran est une méthode ; le menu principal les enchaîne.

L'assistant « Lancer un test » est une suite d'étapes numérotées. Chaque étape
retourne NEXT, BACK ou ABORT, ce qui permet de revenir en arrière avec Échap sans
perdre ce qui a déjà été saisi.

Les garde-fous ne changent pas : la cible passe par guard.check_target(), le
récapitulatif est toujours affiché, et une machine critique exige son nom.
"""

from __future__ import annotations

import ipaddress
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import guard, limits, term
from .config import RunConfig
from .inventory import Machine, find_by_ip, load_inventory
from .stats import TcpStats, UdpStats, write_csv
from .ui import UI, Option

NEXT, BACK, ABORT, EDIT = "next", "back", "abort", "edit"
OTHER_IP = "autre"
CUSTOM = "personnalisé"

# Profils d'intensité proposés : (nom, description, paramètres).
TCP_PROFILES = [
    ("Léger", "10 connexions pendant 10 s", dict(concurrency=10, duration=10.0)),
    ("Modéré", "50 connexions pendant 10 s", dict(concurrency=50, duration=10.0)),
    ("Soutenu", "200 connexions pendant 30 s", dict(concurrency=200, duration=30.0)),
    ("Montée progressive", "de 1 à 200 connexions en 30 s",
     dict(concurrency=200, duration=30.0, ramp=True)),
]
UDP_PROFILES = [
    ("Léger", "100 paquets/s pendant 10 s", dict(rate=100, duration=10.0)),
    ("Modéré", "1000 paquets/s pendant 10 s", dict(rate=1000, duration=10.0)),
    ("Soutenu", "5000 paquets/s pendant 30 s", dict(rate=5000, duration=30.0)),
]
PROFILE_HINTS = {
    "Léger": "Pour vérifier que le service répond, sans le charger.",
    "Modéré": "Le réglage par défaut : une charge nette, sans risque pour un service sain.",
    "Soutenu": "Une charge franche. Surveille le CPU de la cible pendant le test.",
    "Montée progressive": "La charge augmente peu à peu : utile pour repérer le seuil où le service décroche.",
}

# Texte de l'écran d'aide : un paragraphe par élément, coupé à la largeur du cadre à l'affichage.
HELP_TEXT = [
    "Cet outil mesure comment un service de TON réseau réagit à la charge.",
    "TCP : ouvre N connexions en même temps et les renouvelle pendant toute la durée. Il mesure "
    "le nombre de connexions par seconde, leur latence et les échecs (refus, délai dépassé, coupure).",
    "UDP : envoie des paquets à débit fixe. Il mesure le débit réellement émis et compte les "
    "réponses. L'UDP ne garantit ni la livraison ni une réponse.",
    "Garde-fous : seules les adresses de la plage autorisée sont acceptées, un récapitulatif "
    "précède chaque test, et une machine marquée [critique] demande de retaper son nom. "
    "Ctrl+C arrête un test et affiche le résumé partiel.",
    "Sans menu : python stress.py -t IP -p PORT ... (détails avec --help). "
    "Lecture des métriques et limites : voir README.md.",
]


def number(kind, minimum, maximum=None, strict: bool = False) -> Callable[[str], object]:
    """Fabrique un convertisseur de saisie : nombre de type `kind` dans les bornes données."""
    def parse(raw: str):
        try:
            value = kind(raw.replace(",", "."))    # « 0,5 » est accepté comme « 0.5 »
        except ValueError:
            raise ValueError(f"« {raw} » n'est pas un nombre valide.") from None
        too_low = value <= minimum if strict else value < minimum
        if not math.isfinite(value) or too_low or (maximum is not None and value > maximum):
            comparison = "supérieur à" if strict else "au moins"
            upper = f" et au plus {maximum}" if maximum is not None else ""
            raise ValueError(f"Valeur attendue : {comparison} {minimum}{upper}.")
        return value
    return parse


def yes_no(raw: str) -> bool:
    answer = raw.lower()
    if answer in ("o", "oui", "y", "yes"):
        return True
    if answer in ("n", "non", "no"):
        return False
    raise ValueError("Réponds o (oui) ou n (non).")


@dataclass
class Draft:
    """Un test en cours de préparation. Les valeurs par défaut sont les réglages modérés."""
    ip: ipaddress.IPv4Address | None = None
    proto: str = "tcp"
    port: int | None = None
    duration: float = 10.0
    concurrency: int = guard.DEFAULT_CONCURRENCY
    rate: int = guard.DEFAULT_RATE
    size: int | None = None     # None = défaut du protocole (0 en TCP, 64 en UDP)
    hold: float = 0.0
    ramp: bool = False

    def effective_size(self) -> int:
        if self.size is not None:
            return self.size
        return 0 if self.proto == "tcp" else guard.DEFAULT_UDP_SIZE

    def to_config(self, machines: list[Machine], timeout: float) -> RunConfig:
        machine = find_by_ip(machines, self.ip)
        return RunConfig(
            ip=self.ip, port=self.port, proto=self.proto, duration=self.duration,
            name=machine.name if machine else "",
            label=machine.label if machine else "",
            critical=machine.critical if machine else False,
            concurrency=self.concurrency, rate=self.rate, size=self.effective_size(),
            ramp=self.ramp and self.proto == "tcp",
            hold=self.hold if self.proto == "tcp" else 0.0,
            timeout=timeout,
        )

    @classmethod
    def from_config(cls, cfg: RunConfig) -> "Draft":
        return cls(ip=cfg.ip, proto=cfg.proto, port=cfg.port, duration=cfg.duration,
                   concurrency=cfg.concurrency, rate=cfg.rate, size=cfg.size,
                   hold=cfg.hold, ramp=cfg.ramp)


@dataclass
class Run:
    """Un test terminé, gardé dans l'historique de la session."""
    when: datetime
    cfg: RunConfig
    stats: TcpStats | UdpStats


@dataclass
class Session:
    machines: list[Machine]
    inventory_path: Path
    allow_hosts: frozenset = frozenset()
    csv_path: Path | None = None
    timeout: float = 3.0
    defaults: Draft = field(default_factory=Draft)     # valeurs passées en ligne de commande
    history: list[Run] = field(default_factory=list)
    last: RunConfig | None = None


class App:
    def __init__(self, ui: UI, session: Session, execute: Callable[[RunConfig], TcpStats | UdpStats]) -> None:
        self.ui = ui
        self.session = session
        self.execute = execute      # lance réellement le test : fourni par cli.py

    # -- Menu principal ---------------------------------------------------------------
    def run(self) -> int:
        try:
            self._main_menu()
        except (KeyboardInterrupt, EOFError):
            self.ui.write()
        self.ui.write("  " + self.ui.theme.paint("Au revoir.", "muted"))
        return 0

    def _main_menu(self) -> None:
        position = 0
        while True:
            session = self.session
            options = [Option("Lancer un test", self._new_test,
                              hint="Choisir une machine, un port et une intensité.")]
            if session.last:
                last = session.last
                options.append(Option(
                    "Relancer le dernier test", self._repeat_last,
                    detail=f"{last.proto.upper()} {last.display_name}:{last.port}",
                    hint="Reprend les mêmes paramètres ; le récapitulatif est affiché avant le lancement."))
            options += [
                Option("Inventaire des machines", self._inventory, detail=f"{len(session.machines)} machine(s)",
                       hint="Les machines connues, lues dans le fichier d'inventaire."),
                Option("Historique de la session", self._history, detail=f"{len(session.history)} test(s)",
                       hint="Revoir le résumé des tests lancés depuis l'ouverture du programme."),
                Option("Réglages", self._settings,
                       hint="Export CSV, délai de connexion, apparence."),
                Option("Aide", self._help, hint="Ce que mesure l'outil et comment lire les résultats."),
                Option("Quitter", None),
            ]
            self.ui.screen()
            action = self.ui.select("Menu principal", options, start=position, allow_back=False)
            if action is None:
                return
            position = next(index for index, option in enumerate(options) if option.value == action)
            action()

    # -- Assistant « Lancer un test » ---------------------------------------------------
    def _new_test(self) -> None:
        draft = Draft(**vars(self.session.defaults))
        self._wizard(draft, first_step=0)

    def _repeat_last(self) -> None:
        self._wizard(Draft.from_config(self.session.last), first_step=4)

    def _wizard(self, draft: Draft, first_step: int) -> None:
        steps = [self._step_target, self._step_proto, self._step_port, self._step_profile, self._step_confirm]
        step = first_step
        while 0 <= step < len(steps):
            outcome = steps[step](draft)
            if outcome == ABORT:
                return
            if outcome == EDIT:
                step, first_step = 3, 0     # retour au choix de l'intensité
                continue
            if outcome == BACK and step == first_step and first_step > 0:
                return      # « relancer » : Échap ramène au menu principal
            step += 1 if outcome == NEXT else -1
        if step == len(steps):
            self._launch(draft.to_config(self.session.machines, self.session.timeout))

    def _context(self, draft: Draft, upto: int) -> list[str]:
        """Rappel, en tête d'écran, des choix déjà faits aux étapes précédentes."""
        parts = []
        if upto > 0 and draft.ip:
            machine = find_by_ip(self.session.machines, draft.ip)
            parts.append(f"Cible : {machine.name if machine else 'hors inventaire'} ({draft.ip})")
        if upto > 1:
            parts.append(f"Protocole : {draft.proto.upper()}")
        if upto > 2 and draft.port:
            parts.append(f"Port : {draft.port}")
        dot = f"  {self.ui.theme['dot']}  "
        return [dot.join(parts)] if parts else []

    def _step_target(self, draft: Draft) -> str:
        ui, session = self.ui, self.session
        machines = [machine for machine in session.machines if guard.is_targetable(machine.ip)]
        options = [
            Option(machine.name, machine.ip, detail=f"{machine.ip}  {machine.description}".rstrip(),
                   tag="critique" if machine.critical else "",
                   hint="Machine critique : il faudra retaper son nom pour confirmer." if machine.critical else "")
            for machine in machines
        ]
        options += [
            Option(f"{ip}", ip, detail="autorisée par --allow-host")
            for ip in sorted(session.allow_hosts) if not find_by_ip(machines, ip)
        ]
        options.append(Option(f"Autre adresse IP{ui.theme['ellipsis']}", OTHER_IP,
                              hint=f"Une adresse de la plage {guard.allowed_networks_text()}."))
        start = next((i for i, option in enumerate(options) if option.value == draft.ip), 0)

        ui.screen()
        choice = ui.select("Étape 1/5 : quelle machine tester ?", options, start=start)
        if choice is None:
            return BACK
        if choice == OTHER_IP:
            # check_target lève TargetError (un ValueError) : la question est reposée
            # avec le message de refus, et rien n'est envoyé.
            choice = ui.ask("Adresse IP", parse=lambda raw: guard.check_target(raw, session.allow_hosts))
            if choice is None:
                return self._step_target(draft)
        draft.ip = choice
        return NEXT

    def _step_proto(self, draft: Draft) -> str:
        options = [
            Option("TCP", "tcp", detail="ouvrir des connexions simultanées",
                   hint="Pour les services web, bases de données, SSH : tout ce qui écoute en TCP."),
            Option("UDP", "udp", detail="envoyer des paquets à débit fixe",
                   hint="Pour DNS, jeux, VPN WireGuard : sans connexion, donc sans garantie de réponse."),
        ]
        self.ui.screen(self._context(draft, 1))
        choice = self.ui.select("Étape 2/5 : quel protocole ?", options, start=0 if draft.proto == "tcp" else 1)
        if choice is None:
            return BACK
        draft.proto = choice
        return NEXT

    def _step_port(self, draft: Draft) -> str:
        self.ui.screen(self._context(draft, 2))
        self.ui.title("Étape 3/5 : quel port ?")
        port = self.ui.ask("Port (1 à 65535)", default=draft.port, parse=number(int, 1, 65535))
        if port is None:
            return BACK
        draft.port = port
        return NEXT

    def _step_profile(self, draft: Draft) -> str:
        ui = self.ui
        profiles = TCP_PROFILES if draft.proto == "tcp" else UDP_PROFILES
        options = [Option(name, values, detail=description, hint=PROFILE_HINTS[name])
                   for name, description, values in profiles]
        options.append(Option(f"Personnalisé{ui.theme['ellipsis']}", CUSTOM,
                              hint="Régler soi-même la durée, l'intensité et la taille des envois."))
        ui.screen(self._context(draft, 3))
        choice = ui.select("Étape 4/5 : quelle intensité ?", options, start=1)
        if choice is None:
            return BACK
        if choice == CUSTOM:
            return NEXT if self._custom_profile(draft) else self._step_profile(draft)
        # Un profil remet les réglages fins à zéro, puis applique ses propres valeurs.
        draft.size, draft.hold, draft.ramp = None, 0.0, False
        for name, value in choice.items():
            setattr(draft, name, value)
        return NEXT

    def _custom_profile(self, draft: Draft) -> bool:
        """Questions du profil personnalisé. Retourne False si l'utilisateur revient en arrière."""
        ui = self.ui
        ui.screen(self._context(draft, 3))
        ui.title("Intensité personnalisée")
        questions = [("duration", "Durée du test (s)", number(float, 0, strict=True), draft.duration)]
        if draft.proto == "tcp":
            questions += [
                ("concurrency", "Connexions simultanées", number(int, 1, guard.MAX_CONCURRENCY), draft.concurrency),
                ("size", "Octets envoyés par connexion (0 = aucun)", number(int, 0, guard.MAX_TCP_SIZE),
                 draft.effective_size()),
                ("hold", "Maintien de chaque connexion (s)", number(float, 0), draft.hold),
                ("ramp", "Montée progressive (o/n)", yes_no, draft.ramp),
            ]
        else:
            questions += [
                ("rate", "Paquets par seconde", number(int, 1, guard.MAX_RATE), draft.rate),
                ("size", "Taille d'un paquet (octets)", number(int, 0, guard.MAX_UDP_SIZE), draft.effective_size()),
            ]
        answers = {}
        for name, prompt, parse, default in questions:
            shown = ("o" if default else "n") if isinstance(default, bool) else None
            value = ui.ask(prompt, default=default, parse=parse, show_default=shown)
            if value is None:
                return False
            answers[name] = value
        for name, value in answers.items():
            setattr(draft, name, value)
        return True

    def _step_confirm(self, draft: Draft) -> str:
        ui, theme = self.ui, self.ui.theme
        cfg = draft.to_config(self.session.machines, self.session.timeout)
        rows = [
            ("Cible", str(cfg.ip)),
            ("Machine", cfg.display_name + ("  [critique]" if cfg.critical else "")),
            ("Port", str(cfg.port)),
            ("Protocole", cfg.proto.upper()),
            ("Intensité", cfg.intensity_text()),
            ("Durée", f"{cfg.duration:g} s"),
        ]
        ui.screen()
        value_width = term.size()[0] - 4 - 11      # largeur du cadre, moins la colonne des libellés
        lines = []
        for name, value in rows:
            for index, part in enumerate(term.wrap(value, value_width)):
                label = name if index == 0 else ""  # suite d'une valeur trop longue : libellé vide
                lines.append(f"{theme.paint(term.pad(label, 10), 'muted')} {theme.paint(part, 'bold')}")
        ui.panel(lines, title="Étape 5/5 : récapitulatif")
        warnings = (guard.intensity_warnings(cfg.proto, cfg.concurrency, cfg.rate, cfg.size)
                    + limits.system_warnings(cfg.proto, cfg.concurrency))
        for warning in warnings:
            ui.notice(f"Attention : {warning}", "warn")
        ui.write()

        options = [
            Option("Lancer le test", NEXT, hint="Le trafic part dès la validation. Ctrl+C l'arrête à tout moment."),
            Option("Modifier l'intensité", EDIT, hint="Revenir au choix du profil d'intensité."),
            Option("Annuler", ABORT, hint="Retour au menu principal, rien n'est envoyé."),
        ]
        choice = ui.select("Lancer ce test ?", options, start=0)
        if choice is None:
            return BACK
        if choice in (ABORT, EDIT):
            return choice
        if cfg.critical:
            ui.write()
            ui.notice("Cette machine est marquée [critique] : la saturer peut couper d'autres services.", "warn")
            typed = ui.ask(f"Tape son nom ({cfg.name}) pour confirmer", parse=str)
            if typed is None or typed.strip().lower() != cfg.name.lower():
                ui.notice("Nom incorrect : aucun paquet n'a été envoyé.", "err")
                ui.pause()
                return ABORT
        return NEXT

    def _launch(self, cfg: RunConfig) -> None:
        ui, session = self.ui, self.session
        ui.screen()
        try:
            stats = self.execute(cfg)
        except OSError as exc:
            ui.notice(f"Impossible de lancer le test : {exc.strerror or exc}", "err")
            ui.pause()
            return
        session.last = cfg
        session.history.insert(0, Run(datetime.now(), cfg, stats))
        self._show_summary(cfg, stats)
        if session.csv_path:
            try:
                write_csv(session.csv_path, cfg, stats)
                ui.notice(f"Résumé ajouté à {session.csv_path}", "ok")
            except OSError as exc:
                ui.notice(f"Écriture du CSV impossible ({session.csv_path}) : {exc.strerror or exc}", "err")
        ui.pause("Entrée pour revenir au menu")

    def _show_summary(self, cfg: RunConfig, stats: TcpStats | UdpStats) -> None:
        theme = self.ui.theme
        lines = stats.summary_lines(cfg)
        self.ui.write("  " + theme.paint(lines[0], "bold", "warn" if stats.interrupted else "accent"))
        width = term.size()[0] - 4
        for line in lines[1:]:
            indent = " " * (len(line) - len(line.lstrip()))
            for part in term.wrap(line, width - len(indent)) if len(line) > width else [line.lstrip()]:
                self.ui.write("  " + indent + part)

    # -- Autres écrans ---------------------------------------------------------------------
    def _inventory(self) -> None:
        ui, theme, session = self.ui, self.ui.theme, self.session
        while True:
            ui.screen()
            name_width = max((len(machine.name) for machine in session.machines), default=4)
            # Place restante dans le cadre : largeur, moins l'IP, le nom et la note de fin de ligne.
            room = max(8, term.size()[0] - 4 - 15 - 2 - name_width - 2 - 22)
            description_width = min(room, max((len(machine.description) for machine in session.machines), default=0))
            lines = []
            for machine in session.machines:
                if not guard.is_targetable(machine.ip):
                    note = theme.paint("hors plage autorisée", "muted")
                elif machine.critical:
                    note = theme.paint("[critique]", "warn")
                else:
                    note = ""
                lines.append(f"{term.pad(str(machine.ip), 15)}  {theme.paint(term.pad(machine.name, name_width), 'bold')}"
                             f"  {term.pad(term.truncate(machine.description, description_width, theme['ellipsis']), description_width)}"
                             f"  {note}".rstrip())
            ui.panel(lines or [theme.paint("Aucune machine dans l'inventaire.", "muted")], title="Inventaire")
            ui.write("  " + theme.paint(f"Fichier : {session.inventory_path}", "muted"))
            ui.write()
            choice = ui.select("Inventaire", [
                Option("Recharger le fichier", "reload", hint="À utiliser après avoir modifié le fichier d'inventaire."),
            ])
            if choice is None:
                return
            session.machines, warnings = load_inventory(session.inventory_path)
            if warnings:
                ui.write()
                for warning in warnings:
                    ui.notice(warning, "warn")
                ui.pause()

    def _history(self) -> None:
        ui, session = self.ui, self.session
        while True:
            ui.screen()
            if not session.history:
                ui.notice("Aucun test lancé depuis l'ouverture du programme.", "warn")
                ui.pause()
                return
            options = [
                Option(f"{run.when:%H:%M:%S}  {run.cfg.proto.upper()} {run.cfg.display_name}:{run.cfg.port}", run,
                       detail=run.stats.headline(), tag="interrompu" if run.stats.interrupted else "",
                       hint=run.cfg.intensity_text())
                for run in session.history
            ]
            run = ui.select("Historique : choisir un test pour revoir son résumé", options)
            if run is None:
                return
            ui.screen()
            self._show_summary(run.cfg, run.stats)
            ui.pause()

    def _settings(self) -> None:
        ui, session = self.ui, self.session
        position = 0
        while True:
            theme = ui.theme
            options = [
                Option("Export CSV", "csv", detail=str(session.csv_path) if session.csv_path else "désactivé",
                       hint="Ajoute le résumé de chaque test à un fichier, pour comparer plusieurs essais."),
                Option("Délai de connexion TCP", "timeout", detail=f"{session.timeout:g} s",
                       hint="Au-delà de ce délai, une connexion sans réponse est comptée comme un échec."),
                Option("Symboles", "unicode", detail="Unicode" if theme.unicode else "ASCII",
                       hint="Passe en ASCII si les cadres ou les barres s'affichent mal dans ta console."),
            ]
            if ui.interactive:      # sans terminal ANSI, les couleurs ne peuvent pas s'afficher
                options.append(Option("Couleurs", "color", detail="activées" if theme.color else "désactivées"))
            ui.screen()
            choice = ui.select("Réglages (valables jusqu'à la fermeture du programme)", options, start=position)
            if choice is None:
                return
            position = [option.value for option in options].index(choice)
            if choice == "csv":
                ui.write()
                session.csv_path = ui.ask("Fichier CSV", parse=Path, back_hint="(vide pour désactiver)")
            elif choice == "timeout":
                ui.write()
                value = ui.ask("Délai de connexion (s)", default=session.timeout, parse=number(float, 0, strict=True))
                if value is not None:
                    session.timeout = value
            elif choice == "unicode":
                ui.theme = term.Theme(color=theme.color, unicode=not theme.unicode)
            elif choice == "color":
                ui.theme = term.Theme(color=not theme.color, unicode=theme.unicode)

    def _help(self) -> None:
        self.ui.screen()
        inner = term.size()[0] - 4
        lines: list[str] = []
        for paragraph in HELP_TEXT:
            lines += ([""] if lines else []) + term.wrap(paragraph, inner)
        self.ui.panel(lines, title="Aide")
        self.ui.pause()
