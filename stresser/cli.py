"""Ligne de commande : options, menu interactif, récapitulatif et confirmation.

Deux façons de lancer l'outil :
- sans -t  : mode interactif. Un menu liste les machines de l'inventaire, puis
             l'outil demande le port, le protocole, la durée et l'intensité.
             Les options déjà données sur la ligne de commande ne sont pas redemandées.
- avec -t  : mode non interactif. Tout vient des options ; --yes saute la confirmation.

Dans les deux cas, la cible passe par guard.check_target() AVANT tout envoi.
"""

from __future__ import annotations

import argparse
import ipaddress
import sys
from pathlib import Path

from . import __version__, guard, limits
from .config import RunConfig
from .display import make_display
from .inventory import Machine, find_by_ip, load_inventory
from .stats import TcpStats, UdpStats, write_csv
from .tcp import run_tcp
from .udp import run_udp

DEFAULT_DURATION = 10.0
DEFAULT_TIMEOUT = 3.0
DEFAULT_INVENTORY = Path(__file__).resolve().parent.parent / "inventory.txt"

# Codes de sortie du programme
EXIT_OK = 0
EXIT_CANCELLED = 1      # l'utilisateur a répondu non, ou a quitté le menu
EXIT_REFUSED = 2        # cible refusée ou paramètre invalide (même code qu'argparse)
EXIT_INTERRUPTED = 130  # test arrêté par Ctrl+C (convention Unix : 128 + SIGINT)

EPILOG = """\
exemples :
  python stress.py                                   menu interactif
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
        if too_low or (maximum is not None and value > maximum):
            comparison = ">" if strict else ">="
            upper = f" et <= {maximum}" if maximum is not None else ""
            raise argparse.ArgumentTypeError(f"doit être {comparison} {minimum}{upper}")
        return value
    return convert


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stress",
        description=(
            "Test de charge TCP/UDP pour ton homelab. Sans -t, un menu interactif "
            "permet de choisir la machine. Plages autorisées : " + guard.allowed_networks_text() + "."
        ),
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    target = parser.add_argument_group("cible")
    target.add_argument("-t", "--target", metavar="IP",
                        help="adresse IPv4 de la cible (sans cette option : menu interactif)")
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
                        help="ajoute le résumé du test à ce fichier CSV")
    output.add_argument("--plain", action="store_true",
                        help="affichage en texte simple (sans rich)")
    output.add_argument("--yes", "-y", action="store_true",
                        help="ne demande pas de confirmation (mode scripté ; exige -t)")
    output.add_argument("--inventory", type=Path, metavar="FICHIER", default=DEFAULT_INVENTORY,
                        help="fichier d'inventaire (défaut : inventory.txt à côté de stress.py)")
    output.add_argument("--list", action="store_true",
                        help="affiche l'inventaire et quitte")
    return parser


# --- Saisie interactive ------------------------------------------------------------

def _ask_number(prompt: str, kind, default, minimum, maximum=None, strict=False):
    """Demande un nombre, avec valeur par défaut ; repose la question tant qu'il est invalide."""
    convert = _bounded(kind, minimum, maximum, strict)
    suffix = f" [{default:g}]" if default is not None else ""
    while True:
        raw = input(f"{prompt}{suffix} : ").strip()
        if not raw and default is not None:
            return default
        try:
            return convert(raw)
        except argparse.ArgumentTypeError as exc:
            print(f"  Valeur invalide : {exc}.")


def _ask_proto() -> str:
    while True:
        raw = input("Protocole (tcp/udp) [tcp] : ").strip().lower()
        if raw in ("", "tcp", "udp"):
            return raw or "tcp"
        print("  Réponds tcp ou udp.")


def _print_inventory(machines: list[Machine]) -> list[Machine]:
    """Affiche les machines ciblables, numérotées. Retourne la liste affichée."""
    listed = [machine for machine in machines if guard.is_targetable(machine.ip)]
    hidden = len(machines) - len(listed)
    if listed:
        print("Machines de l'inventaire :")
        for number, machine in enumerate(listed, start=1):
            tag = "  [critique]" if machine.critical else ""
            print(f"  {number:2d}) {str(machine.ip):<15}  {machine.label}{tag}")
    else:
        print("Inventaire vide.")
    if hidden:
        print(f"  ({hidden} machine(s) de l'inventaire hors des plages autorisées : non listée(s))")
    return listed


def _ask_target(machines: list[Machine], allow_hosts) -> ipaddress.IPv4Address:
    """Menu de choix de la cible : numéro dans la liste, ou IP tapée à la main."""
    listed = _print_inventory(machines)
    while True:
        raw = input("Choix (numéro de la liste, ou adresse IP) : ").strip()
        if raw.isdigit():
            if 1 <= int(raw) <= len(listed):
                return guard.check_target(str(listed[int(raw) - 1].ip), allow_hosts)
            print(f"  Numéro hors liste (1 à {len(listed)}).")
            continue
        try:
            guard.parse_ipv4(raw)
        except guard.TargetError:
            print("  Tape un numéro de la liste ou une adresse IPv4 (ex. 192.168.35.14).")
            continue
        # IP bien formée : si elle est hors plage, TargetError remonte et l'outil s'arrête.
        return guard.check_target(raw, allow_hosts)


# --- Construction de la configuration ---------------------------------------------

def build_config(args: argparse.Namespace, machines: list[Machine], allow_hosts) -> RunConfig:
    """Assemble un RunConfig à partir des options et, en mode interactif, des réponses."""
    interactive = args.target is None

    # 1. La cible, validée avant toute autre question.
    if interactive:
        ip = _ask_target(machines, allow_hosts)
    else:
        ip = guard.check_target(args.target, allow_hosts)
    machine = find_by_ip(machines, ip)
    if interactive:
        print(f"Cible : {ip} ({machine.label if machine else 'hors inventaire'})")

    # 2. Port et protocole.
    port, proto = args.port, args.proto
    if port is None:
        if not interactive:
            raise UsageError("le port est obligatoire avec -t : ajoute -p PORT.")
        port = _ask_number("Port", int, None, 1, 65535)
    if proto is None:
        proto = _ask_proto() if interactive else "tcp"

    # 3. Durée et intensité : la valeur de la ligne de commande, sinon une question
    #    (mode interactif), sinon le défaut modéré.
    duration, concurrency, rate = args.duration, args.concurrency, args.rate
    if duration is None:
        duration = (_ask_number("Durée en secondes", float, DEFAULT_DURATION, 0, strict=True)
                    if interactive else DEFAULT_DURATION)
    if proto == "tcp" and concurrency is None:
        concurrency = (_ask_number("Connexions simultanées", int, guard.DEFAULT_CONCURRENCY,
                                   1, guard.MAX_CONCURRENCY)
                       if interactive else guard.DEFAULT_CONCURRENCY)
    if proto == "udp" and rate is None:
        rate = (_ask_number("Paquets par seconde", int, guard.DEFAULT_RATE, 1, guard.MAX_RATE)
                if interactive else guard.DEFAULT_RATE)

    size = args.size
    if size is None:
        size = 0 if proto == "tcp" else guard.DEFAULT_UDP_SIZE
    try:
        guard.check_size(proto, size)
    except ValueError as exc:
        raise UsageError(str(exc)) from None

    return RunConfig(
        ip=ip, port=port, proto=proto, duration=duration,
        name=machine.name if machine else "",
        label=machine.label if machine else "",
        critical=machine.critical if machine else False,
        concurrency=concurrency or guard.DEFAULT_CONCURRENCY,
        rate=rate or guard.DEFAULT_RATE,
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


# --- Récapitulatif et confirmation -------------------------------------------------

def intensity_text(cfg: RunConfig) -> str:
    """Résume l'intensité d'un test en une phrase (récapitulatif, panel web)."""
    if cfg.proto == "udp":
        return f"{cfg.rate} paquets/s de {cfg.size} octets"
    payload = f"{cfg.size} octets par connexion" if cfg.size else "connecter puis fermer"
    text = f"{cfg.concurrency} connexions simultanées, {payload}"
    if cfg.hold:
        text += f", maintien {cfg.hold:g} s"
    if cfg.ramp:
        text += ", montée progressive"
    return text


def recap_lines(cfg: RunConfig) -> list[str]:
    intensity = intensity_text(cfg)
    return [
        "----- Récapitulatif -----",
        f"Cible     : {cfg.ip}",
        f"Machine   : {cfg.display_name}" + ("  [critique]" if cfg.critical else ""),
        f"Port      : {cfg.port}",
        f"Protocole : {cfg.proto.upper()}",
        f"Intensité : {intensity}",
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


# --- Exécution -----------------------------------------------------------------------

def execute(cfg: RunConfig, plain: bool = False) -> TcpStats | UdpStats:
    """Lance le moteur correspondant au protocole. C'est ici que le trafic part."""
    display = make_display(plain)
    return run_tcp(cfg, display) if cfg.proto == "tcp" else run_udp(cfg, display)


def _error(message: str) -> None:
    print(f"Erreur : {message}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    """Point d'entrée. Retourne le code de sortie du programme."""
    # Une console Windows redirigée n'est pas toujours en UTF-8 : on remplace les
    # caractères impossibles à écrire plutôt que de planter sur un accent.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    args = build_parser().parse_args(argv)
    machines, inventory_warnings = load_inventory(args.inventory)
    for warning in inventory_warnings:
        print(f"Attention : {warning}", file=sys.stderr)

    if args.list:
        _print_inventory(machines)
        return EXIT_OK

    if args.yes and args.target is None:
        _error("--yes sert au mode scripté et exige une cible : ajoute -t IP.")
        return EXIT_REFUSED

    try:
        allow_hosts = guard.parse_allow_hosts(args.allow_host)
        cfg = build_config(args, machines, allow_hosts)
    except (guard.TargetError, UsageError) as exc:
        _error(str(exc))
        return EXIT_REFUSED
    except (EOFError, KeyboardInterrupt):
        print("\nAnnulé.")
        return EXIT_CANCELLED

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
        stats = execute(cfg, plain=args.plain)
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
