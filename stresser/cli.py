"""Ligne de commande : analyse des options, puis l'un des deux modes.

- sans -t  : l'application à menus (menu.py). On choisit la machine, le port et
             l'intensité à l'écran. Les options de test données sur la ligne de
             commande servent de valeurs par défaut.
- avec -t  : mode non interactif, pour les scripts. Tout vient des options ;
             --yes saute la confirmation.

Dans les deux cas, la cible passe par guard.check_target() AVANT tout envoi.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, guard, limits, term
from .config import RunConfig
from .display import make_display
from .inventory import Machine, find_by_ip, load_inventory
from .menu import App, Draft, Session
from .stats import TcpStats, UdpStats, write_csv
from .tcp import run_tcp
from .udp import run_udp
from .ui import UI

DEFAULT_DURATION = 10.0
DEFAULT_TIMEOUT = 3.0
DEFAULT_INVENTORY = Path(__file__).resolve().parent.parent / "inventory.txt"

# Codes de sortie du programme
EXIT_OK = 0
EXIT_CANCELLED = 1      # l'utilisateur a répondu non, ou le test n'a pas pu démarrer
EXIT_REFUSED = 2        # cible refusée ou paramètre invalide (même code qu'argparse)
EXIT_INTERRUPTED = 130  # test arrêté par Ctrl+C (convention Unix : 128 + SIGINT)

EPILOG = """\
exemples :
  python stress.py                                   menus (flèches, Entrée, Échap)
  python stress.py --list                            affiche l'inventaire
  python stress.py -t 192.168.35.14 -p 8123 --proto tcp --duration 30 --concurrency 100
  python stress.py -t 192.168.35.14 -p 8123 --size 512 --hold 1 --ramp
  python stress.py -t 192.168.35.13 -p 53 --proto udp --rate 500 --size 64 --csv tests.csv
  python stress.py -t 192.168.35.16 -p 3001 --duration 10 --yes    (sans confirmation)

Réservé à tes propres machines : toute cible hors des plages autorisées est refusée.
"""


class UsageError(Exception):
    """Paramètre invalide ou manquant. Le message est affiché tel quel."""


# --- Analyse des options ---------------------------------------------------------

def _bounded(kind, minimum, maximum=None, strict=False):
    """Fabrique un validateur argparse : nombre de type `kind` dans [minimum, maximum]."""
    def convert(text: str):
        try:
            value = kind(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"« {text} » n'est pas un nombre valide") from None
        too_low = value <= minimum if strict else value < minimum
        if too_low or (maximum is not None and value > maximum) or value != value or value == float("inf"):
            comparison = ">" if strict else ">="
            upper = f" et <= {maximum}" if maximum is not None else ""
            raise argparse.ArgumentTypeError(f"doit être {comparison} {minimum}{upper}")
        return value
    return convert


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stress",
        description=(
            "Test de charge TCP/UDP pour ton homelab. Sans -t, l'outil s'ouvre sur des "
            "menus. Plages autorisées : " + guard.allowed_networks_text() + "."
        ),
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    target = parser.add_argument_group("cible")
    target.add_argument("-t", "--target", metavar="IP",
                        help="adresse IPv4 de la cible (sans cette option : menus)")
    target.add_argument("-p", "--port", type=_bounded(int, 1, 65535), metavar="PORT",
                        help="port de la cible (1 à 65535)")
    target.add_argument("--proto", choices=("tcp", "udp"),
                        help="protocole (défaut : tcp)")
    target.add_argument("--allow-host", action="append", metavar="IP", default=[],
                        help="autorise ponctuellement une IP PRIVÉE hors plages (répétable) ; "
                             "une IP publique est toujours refusée")

    load = parser.add_argument_group("intensité")
    load.add_argument("--duration", type=_bounded(float, 0, strict=True), metavar="S",
                      help=f"durée du test en secondes (défaut : {DEFAULT_DURATION:g})")
    load.add_argument("--concurrency", type=_bounded(int, 1, guard.MAX_CONCURRENCY), metavar="N",
                      help=f"TCP : connexions simultanées (défaut : {guard.DEFAULT_CONCURRENCY}, "
                           f"avertissement au-delà de {guard.WARN_CONCURRENCY})")
    load.add_argument("--rate", type=_bounded(int, 1, guard.MAX_RATE), metavar="PPS",
                      help=f"UDP : paquets par seconde (défaut : {guard.DEFAULT_RATE}, "
                           f"avertissement au-delà de {guard.WARN_RATE})")
    load.add_argument("--size", type=_bounded(int, 0), metavar="OCTETS",
                      help="TCP : octets envoyés sur chaque connexion (défaut : 0 = connecter "
                           f"puis fermer) ; UDP : taille d'un paquet (défaut : {guard.DEFAULT_UDP_SIZE})")
    load.add_argument("--ramp", action="store_true",
                      help="TCP : montée progressive de 1 à N connexions sur la durée du test")
    load.add_argument("--hold", type=_bounded(float, 0), metavar="S", default=0.0,
                      help="TCP : garde chaque connexion ouverte S secondes et compte les octets "
                           "reçus (défaut : 0 = fermer tout de suite)")
    load.add_argument("--timeout", type=_bounded(float, 0, strict=True), metavar="S",
                      default=DEFAULT_TIMEOUT,
                      help=f"TCP : délai maximal d'une connexion (défaut : {DEFAULT_TIMEOUT:g})")

    output = parser.add_argument_group("affichage et sortie")
    output.add_argument("--csv", type=Path, metavar="FICHIER",
                        help="ajoute le résumé de chaque test à ce fichier CSV")
    output.add_argument("--plain", action="store_true",
                        help="texte simple : ni couleurs, ni menus à flèches, ni tableau de bord")
    output.add_argument("--ascii", action="store_true",
                        help="dessine les cadres et les barres en ASCII (console qui affiche mal l'Unicode)")
    output.add_argument("--yes", "-y", action="store_true",
                        help="ne demande pas de confirmation (mode scripté ; exige -t)")
    output.add_argument("--inventory", type=Path, metavar="FICHIER", default=DEFAULT_INVENTORY,
                        help="fichier d'inventaire (défaut : inventory.txt à côté de stress.py)")
    output.add_argument("--list", action="store_true",
                        help="affiche l'inventaire et quitte")
    return parser


# --- Mode non interactif ------------------------------------------------------------

def build_config(args: argparse.Namespace, machines: list[Machine], allow_hosts) -> RunConfig:
    """Assemble un RunConfig à partir des options de la ligne de commande."""
    ip = guard.check_target(args.target, allow_hosts)   # la cible d'abord
    machine = find_by_ip(machines, ip)

    if args.port is None:
        raise UsageError("le port est obligatoire avec -t : ajoute -p PORT.")
    proto = args.proto or "tcp"
    size = args.size
    if size is None:
        size = 0 if proto == "tcp" else guard.DEFAULT_UDP_SIZE
    try:
        guard.check_size(proto, size)
    except ValueError as exc:
        raise UsageError(str(exc)) from None

    return RunConfig(
        ip=ip, port=args.port, proto=proto,
        duration=args.duration if args.duration is not None else DEFAULT_DURATION,
        name=machine.name if machine else "",
        label=machine.label if machine else "",
        critical=machine.critical if machine else False,
        concurrency=args.concurrency or guard.DEFAULT_CONCURRENCY,
        rate=args.rate or guard.DEFAULT_RATE,
        size=size, ramp=args.ramp, hold=args.hold, timeout=args.timeout,
    )


def ignored_options(args: argparse.Namespace, proto: str) -> list[str]:
    """Options données mais sans effet pour le protocole choisi."""
    if proto == "udp":
        given = {"--concurrency": args.concurrency is not None, "--ramp": args.ramp,
                 "--hold": bool(args.hold)}
    else:
        given = {"--rate": args.rate is not None}
    return [name for name, used in given.items() if used]


def recap_lines(cfg: RunConfig) -> list[str]:
    return [
        "----- Récapitulatif -----",
        f"Cible     : {cfg.ip}",
        f"Machine   : {cfg.display_name}" + ("  [critique]" if cfg.critical else ""),
        f"Port      : {cfg.port}",
        f"Protocole : {cfg.proto.upper()}",
        f"Intensité : {cfg.intensity_text()}",
        f"Durée     : {cfg.duration:g} s",
    ]


def confirm(cfg: RunConfig) -> bool:
    """Demande l'accord avant de lancer. Une machine critique exige de retaper son nom."""
    if cfg.critical:
        print("Cette machine est marquée [critique] : la saturer peut couper d'autres services.")
        answer = input(f"Tape son nom ({cfg.name}) pour confirmer : ")
        return answer.strip().lower() == cfg.name.lower()
    answer = input("Lancer le test ? [o/N] : ")
    return answer.strip().lower() in ("o", "oui", "y", "yes")


def print_inventory(machines: list[Machine]) -> None:
    """Option --list : les machines ciblables, une par ligne."""
    listed = [machine for machine in machines if guard.is_targetable(machine.ip)]
    hidden = len(machines) - len(listed)
    if listed:
        print("Machines de l'inventaire :")
        for machine in listed:
            tag = "  [critique]" if machine.critical else ""
            print(f"  {str(machine.ip):<15}  {machine.label}{tag}")
    else:
        print("Inventaire vide.")
    if hidden:
        print(f"  ({hidden} machine(s) de l'inventaire hors des plages autorisées : non listée(s))")


# --- Exécution -----------------------------------------------------------------------

def execute(cfg: RunConfig, theme: term.Theme | None = None, ansi: bool = False) -> TcpStats | UdpStats:
    """Lance le moteur correspondant au protocole. C'est ici que le trafic part."""
    display = make_display(cfg, theme, ansi)
    return run_tcp(cfg, display) if cfg.proto == "tcp" else run_udp(cfg, display)


def _error(message: str) -> None:
    print(f"Erreur : {message}", file=sys.stderr)


def run_menus(args: argparse.Namespace, machines: list[Machine], allow_hosts,
              theme: term.Theme, ansi: bool) -> int:
    """Ouvre l'application à menus."""
    ui = UI(theme, interactive=ansi and sys.stdin.isatty())
    session = Session(
        machines=machines, inventory_path=args.inventory, allow_hosts=allow_hosts,
        csv_path=args.csv, timeout=args.timeout,
        # Les options de test données sans -t deviennent les valeurs proposées par défaut.
        defaults=Draft(
            proto=args.proto or "tcp", port=args.port,
            duration=args.duration if args.duration is not None else DEFAULT_DURATION,
            concurrency=args.concurrency or guard.DEFAULT_CONCURRENCY,
            rate=args.rate or guard.DEFAULT_RATE,
            size=args.size, hold=args.hold, ramp=args.ramp,
        ),
    )
    if not ui.interactive:
        ui.banner()     # en texte simple, le bandeau n'est écrit qu'une fois
    # ui.theme est relu à chaque test : les réglages d'apparence s'appliquent aussitôt.
    return App(ui, session, lambda cfg: execute(cfg, ui.theme, ansi)).run()


def main(argv: list[str] | None = None) -> int:
    """Point d'entrée. Retourne le code de sortie du programme."""
    # Une console Windows redirigée n'est pas toujours en UTF-8 : on remplace les
    # caractères impossibles à écrire plutôt que de planter sur un accent.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    args = build_parser().parse_args(argv)
    theme, ansi = term.detect(plain=args.plain, ascii_only=args.ascii)
    machines, inventory_warnings = load_inventory(args.inventory)
    for warning in inventory_warnings:
        print(f"Attention : {warning}", file=sys.stderr)

    if args.list:
        print_inventory(machines)
        return EXIT_OK
    if args.yes and args.target is None:
        _error("--yes sert au mode scripté et exige une cible : ajoute -t IP.")
        return EXIT_REFUSED

    try:
        allow_hosts = guard.parse_allow_hosts(args.allow_host)
        if args.target is None:
            return run_menus(args, machines, allow_hosts, theme, ansi)
        cfg = build_config(args, machines, allow_hosts)
    except (guard.TargetError, UsageError) as exc:
        _error(str(exc))
        return EXIT_REFUSED

    print()
    for line in recap_lines(cfg):
        print(line)
    for name in ignored_options(args, cfg.proto):
        print(f"Note : {name} est sans effet en {cfg.proto.upper()}.")
    warnings = (guard.intensity_warnings(cfg.proto, cfg.concurrency, cfg.rate, cfg.size)
                + limits.system_warnings(cfg.proto, cfg.concurrency))
    for warning in warnings:
        print(f"ATTENTION : {warning}")

    if not args.yes:
        try:
            accepted = confirm(cfg)
        except (EOFError, KeyboardInterrupt):
            accepted = False
            print()
        if not accepted:
            print("Annulé : aucun paquet n'a été envoyé.")
            return EXIT_CANCELLED

    print()
    try:
        stats = execute(cfg, theme, ansi)
    except OSError as exc:
        # Par exemple : aucune route vers la cible depuis cette machine.
        _error(f"impossible de lancer le test : {exc.strerror or exc}")
        return EXIT_CANCELLED

    print()
    for line in stats.summary_lines(cfg):
        print(line)

    if args.csv:
        try:
            write_csv(args.csv, cfg, stats)
            print(f"Résumé ajouté à {args.csv}")
        except OSError as exc:
            _error(f"écriture du CSV impossible ({args.csv}) : {exc.strerror or exc}")

    return EXIT_INTERRUPTED if stats.interrupted else EXIT_OK
