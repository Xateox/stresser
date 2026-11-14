"""Panel web : la même chose que la ligne de commande, depuis un navigateur.

Lancement : `python panel.py`, puis ouvrir l'adresse affichée dans la console.

Le panel ne contourne aucun garde-fou : chaque demande repasse côté serveur par
guard.check_target(), exactement comme la ligne de commande. Le navigateur ne
décide de rien.

Sécurité du panel lui-même (c'est un bouton « lancer un test de charge » posé
sur le réseau, donc il faut le protéger) :
- il écoute sur 127.0.0.1 par défaut : seul ce PC peut l'atteindre ;
- un jeton aléatoire, tiré à chaque démarrage, est exigé par toutes les requêtes
  de l'API. Sans lui, une page web malveillante ouverte dans le même navigateur
  ne peut pas déclencher de test à ma place ;
- seuls les clients d'une adresse privée (ou locale) sont acceptés ;
- les IP autorisées en plus (--allow-host) se donnent au démarrage, jamais depuis
  la page ;
- un seul test à la fois.

Serveur : http.server de la bibliothèque standard, un thread par requête. Le test
tourne dans son propre thread ; la page interroge /api/status chaque seconde.
"""

from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import math
import secrets
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import __version__, guard, limits
from .cli import DEFAULT_DURATION, DEFAULT_INVENTORY, DEFAULT_TIMEOUT, intensity_text
from .config import RunConfig
from .inventory import Machine, find_by_ip, load_inventory
from .stats import TcpStats, UdpStats, write_csv
from .tcp import run_tcp
from .udp import run_udp

DEFAULT_PORT = 8080
TOKEN_HEADER = "X-Stresser-Token"
MAX_BODY = 16 * 1024            # taille maximale d'une requête, en octets
HISTORY_SIZE = 30               # nombre de tests gardés dans l'historique de la page
PAGE_PATH = Path(__file__).resolve().parent / "panel.html"


class RequestError(Exception):
    """Demande refusée. `status` est le code HTTP, le message est montré dans la page."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


# --- Validation des demandes --------------------------------------------------

def _number(payload: dict, key: str, label: str, kind, default, minimum, maximum=None, strict=False):
    """Lit un nombre dans la demande, avec les mêmes bornes que la ligne de commande."""
    value = payload.get(key)
    if value is None or value == "":
        value = default
    # bool est un sous-type de int en Python : on l'écarte explicitement.
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise RequestError(f"{label} : nombre attendu.")
    if kind is int:
        if value != int(value):
            raise RequestError(f"{label} : nombre entier attendu.")
        value = int(value)
    else:
        value = float(value)
    too_low = value <= minimum if strict else value < minimum
    if too_low or (maximum is not None and value > maximum):
        comparison = ">" if strict else ">="
        upper = f" et <= {maximum}" if maximum is not None else ""
        raise RequestError(f"{label} : doit être {comparison} {minimum}{upper}.")
    return value


def build_config(payload: dict, machines: list[Machine], allow_hosts) -> RunConfig:
    """Transforme une demande de la page en RunConfig validé, ou lève RequestError."""
    target = payload.get("target")
    if not isinstance(target, str):
        raise RequestError("Cible : adresse IPv4 attendue.")
    try:
        ip = guard.check_target(target, allow_hosts)
    except guard.TargetError as exc:
        raise RequestError(str(exc)) from None

    proto = payload.get("proto", "tcp")
    if proto not in ("tcp", "udp"):
        raise RequestError("Protocole : tcp ou udp.")

    port = _number(payload, "port", "Port", int, None, 1, 65535)
    duration = _number(payload, "duration", "Durée", float, DEFAULT_DURATION, 0, strict=True)
    concurrency = _number(payload, "concurrency", "Connexions simultanées", int,
                          guard.DEFAULT_CONCURRENCY, 1, guard.MAX_CONCURRENCY)
    rate = _number(payload, "rate", "Paquets par seconde", int,
                   guard.DEFAULT_RATE, 1, guard.MAX_RATE)
    size = _number(payload, "size", "Taille", int,
                   0 if proto == "tcp" else guard.DEFAULT_UDP_SIZE, 0)
    hold = _number(payload, "hold", "Maintien", float, 0.0, 0)
    timeout = _number(payload, "timeout", "Délai de connexion", float, DEFAULT_TIMEOUT, 0, strict=True)
    try:
        guard.check_size(proto, size)
    except ValueError as exc:
        raise RequestError(str(exc).replace("--size", "La taille")) from None

    machine = find_by_ip(machines, ip)
    return RunConfig(
        ip=ip, port=port, proto=proto, duration=duration,
        name=machine.name if machine else "",
        label=machine.label if machine else "",
        critical=machine.critical if machine else False,
        concurrency=concurrency, rate=rate, size=size,
        ramp=payload.get("ramp") is True, hold=hold, timeout=timeout,
    )


def headline(cfg: RunConfig, stats: TcpStats | UdpStats) -> str:
    """Résultat d'un test en une ligne, pour l'historique."""
    elapsed = stats.elapsed() or 1.0
    if cfg.proto == "tcp":
        return (f"{stats.succeeded} réussies, {stats.failed} échouées, "
                f"{stats.succeeded / elapsed:.0f} conn/s")
    return f"{stats.sent} envoyés, {stats.sent / elapsed:.0f} paquets/s, {stats.received} reçus"


# --- Un test en cours ou terminé ----------------------------------------------

class Job:
    """Un test lancé depuis le panel.

    L'objet sert aussi d'« affichage » aux moteurs (méthodes start / update / stop,
    comme display.py) : chaque seconde, il reçoit les valeurs en direct et en
    profite pour ajouter un point au graphique.
    """

    def __init__(self, number: int, cfg: RunConfig) -> None:
        self.number = number
        self.cfg = cfg
        self.stats: TcpStats | UdpStats = TcpStats() if cfg.proto == "tcp" else UdpStats()
        self.stop_event = threading.Event()
        self.state = "running"              # "running", "done" ou "error"
        self.error = ""
        self.started_at = datetime.now()
        self.fields: list[tuple[str, str]] = []     # valeurs en direct, déjà mises en forme
        self.samples: list[list[float]] = []        # points du graphique : [temps, série 1, série 2]
        self.summary: list[str] = []
        self.thread: threading.Thread | None = None
        self._previous = (0.0, 0, 0)

    @property
    def series(self) -> list[str]:
        """Noms des deux courbes du graphique (même unité : par seconde)."""
        if self.cfg.proto == "tcp":
            return ["Connexions réussies/s", "Échecs/s"]
        return ["Paquets envoyés/s", "Paquets reçus/s"]

    def _counters(self) -> tuple[int, int]:
        if self.cfg.proto == "tcp":
            return self.stats.succeeded, self.stats.failed
        return self.stats.sent, self.stats.received

    def _add_sample(self, minimum_interval: float = 0.0) -> None:
        """Ajoute un point : les deux compteurs, ramenés à un débit par seconde."""
        now = self.stats.elapsed()
        first, second = self._counters()
        previous_time, previous_first, previous_second = self._previous
        interval = now - previous_time
        if interval <= minimum_interval:
            return
        self.samples.append([
            round(now, 2),
            round((first - previous_first) / interval, 1),
            round((second - previous_second) / interval, 1),
        ])
        self._previous = (now, first, second)

    # Interface « affichage » appelée par les moteurs
    def start(self, title: str) -> None:
        pass

    def update(self, fields: list[tuple[str, str]]) -> None:
        self.fields = fields
        self._add_sample()

    def stop(self) -> None:
        pass

    def run(self, csv_path: Path | None) -> None:
        """Corps du thread : lance le moteur puis fige le résultat."""
        engine = run_tcp if self.cfg.proto == "tcp" else run_udp
        try:
            engine(self.cfg, self, stop_event=self.stop_event, stats=self.stats)
            self._add_sample(minimum_interval=0.25)     # dernière fraction de seconde
            self.fields = self.stats.final_fields(self.cfg)   # totaux et moyennes du test
            self.summary = self.stats.summary_lines(self.cfg)
            if csv_path:
                try:
                    write_csv(csv_path, self.cfg, self.stats)
                    self.summary.append(f"Résumé ajouté à {csv_path}")
                except OSError as exc:
                    self.summary.append(f"Écriture du CSV impossible : {exc.strerror or exc}")
            self.state = "done"
        except Exception as exc:    # le thread ne doit jamais mourir en silence
            self.error = str(getattr(exc, "strerror", None) or exc)
            self.state = "error"

    def status_label(self) -> str:
        if self.state == "error":
            return "erreur"
        if self.state == "running":
            return "en cours"
        return "interrompu" if self.stats.interrupted else "terminé"

    def to_json(self, since: int = 0) -> dict:
        cfg = self.cfg
        return {
            "number": self.number,
            "state": self.state,
            "status": self.status_label(),
            "error": self.error,
            "proto": cfg.proto,
            "target": str(cfg.ip),
            "name": cfg.display_name,
            "port": cfg.port,
            "intensity": intensity_text(cfg),
            "duration": cfg.duration,
            "elapsed": round(self.stats.elapsed(), 1),
            "interrupted": self.stats.interrupted,
            "fields": self.fields,
            "series": self.series,
            "samples": self.samples[since:],
            "sample_count": len(self.samples),
            "summary": self.summary,
        }

    def history_entry(self) -> dict:
        return {
            "time": self.started_at.strftime("%H:%M:%S"),
            "target": str(self.cfg.ip),
            "name": self.cfg.display_name,
            "port": f"{self.cfg.port}/{self.cfg.proto}",
            "intensity": intensity_text(self.cfg),
            "duration": f"{self.stats.elapsed():.1f} s",
            "result": self.error or headline(self.cfg, self.stats),
            "status": self.status_label(),
        }


# --- État du panel -------------------------------------------------------------

class Panel:
    """Tout ce que le serveur partage entre les requêtes."""

    def __init__(self, machines: list[Machine], allow_hosts=frozenset(),
                 csv_path: Path | None = None, token: str | None = None) -> None:
        self.machines = machines
        self.allow_hosts = allow_hosts
        self.csv_path = csv_path
        self.token = token or secrets.token_urlsafe(24)
        self.lock = threading.Lock()        # protège `job` et `history`
        self.job: Job | None = None
        self.history: list[dict] = []
        self._counter = 0

    def describe(self) -> dict:
        """Ce dont la page a besoin pour dessiner le formulaire."""
        return {
            "version": __version__,
            "networks": [str(net) for net in guard.ALLOWED_NETWORKS],
            "allow_hosts": sorted(str(ip) for ip in self.allow_hosts),
            "machines": [
                {"ip": str(m.ip), "name": m.name, "label": m.label, "critical": m.critical}
                for m in self.machines if guard.is_targetable(m.ip)
            ],
            "defaults": {
                "duration": DEFAULT_DURATION, "timeout": DEFAULT_TIMEOUT,
                "concurrency": guard.DEFAULT_CONCURRENCY, "rate": guard.DEFAULT_RATE,
                "udp_size": guard.DEFAULT_UDP_SIZE,
            },
            "limits": {
                "max_concurrency": guard.MAX_CONCURRENCY, "max_rate": guard.MAX_RATE,
                "max_tcp_size": guard.MAX_TCP_SIZE, "max_udp_size": guard.MAX_UDP_SIZE,
            },
            "csv": str(self.csv_path) if self.csv_path else None,
        }

    def check(self, payload: dict) -> dict:
        """Valide une demande SANS rien lancer : sert au récapitulatif de confirmation."""
        cfg = build_config(payload, self.machines, self.allow_hosts)
        warnings = (guard.intensity_warnings(cfg.proto, cfg.concurrency, cfg.rate, cfg.size)
                    + limits.system_warnings(cfg.proto, cfg.concurrency))
        return {
            "target": str(cfg.ip),
            "name": cfg.display_name,
            "confirm_name": cfg.name if cfg.critical else "",
            "critical": cfg.critical,
            "port": cfg.port,
            "proto": cfg.proto.upper(),
            "intensity": intensity_text(cfg),
            "duration": f"{cfg.duration:g} s",
            "warnings": warnings,
        }

    def start(self, payload: dict, client: str = "?") -> dict:
        """Valide puis lance un test. La confirmation est vérifiée ici, pas dans la page."""
        cfg = build_config(payload, self.machines, self.allow_hosts)
        if payload.get("confirmed") is not True:
            raise RequestError("Le test doit être confirmé avant d'être lancé.")
        if cfg.critical:
            typed = payload.get("confirm_name")
            if not isinstance(typed, str) or typed.strip().lower() != cfg.name.lower():
                raise RequestError(
                    f"Machine critique : il faut retaper son nom ({cfg.name}) pour confirmer.")

        with self.lock:
            if self.job and self.job.state == "running":
                raise RequestError("Un test est déjà en cours.", status=409)
            self._counter += 1
            job = Job(self._counter, cfg)
            job.thread = threading.Thread(target=self._run_job, args=(job,), daemon=True)
            self.job = job
            job.thread.start()
        log(f"test n°{job.number} lancé par {client} : {cfg.proto.upper()} "
            f"{cfg.ip}:{cfg.port} ({cfg.display_name}), {intensity_text(cfg)}, {cfg.duration:g} s")
        return job.to_json()

    def _run_job(self, job: Job) -> None:
        job.run(self.csv_path)
        with self.lock:
            self.history.insert(0, job.history_entry())
            del self.history[HISTORY_SIZE:]
        log(f"test n°{job.number} {job.status_label()} : {job.error or headline(job.cfg, job.stats)}")

    def stop(self, client: str = "?") -> dict:
        with self.lock:
            job = self.job
        if not job or job.state != "running":
            raise RequestError("Aucun test en cours.", status=409)
        job.stop_event.set()
        log(f"arrêt du test n°{job.number} demandé par {client}")
        return {"stopping": True}

    def status(self, since: int = 0, number: int | None = None) -> dict:
        """État du test. `since` évite de renvoyer les points que la page a déjà.

        `number` est le numéro du test que la page connaît : si un autre test a
        démarré entre-temps, on renvoie tous ses points.
        """
        with self.lock:
            job, history = self.job, list(self.history)
        if job and number != job.number:
            since = 0
        return {"job": job.to_json(since) if job else None, "history": history}

    def shutdown(self) -> None:
        """Arrête proprement le test en cours quand on ferme le panel."""
        job = self.job
        if job and job.state == "running":
            job.stop_event.set()
            if job.thread:
                job.thread.join(timeout=5.0)


# --- Serveur HTTP -----------------------------------------------------------------

def log(message: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {message}", flush=True)


def client_allowed(address: str) -> bool:
    """Seuls les clients locaux ou d'une adresse privée peuvent parler au panel."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return ip.is_loopback or (ip.version == 4 and guard.is_rfc1918(ip))


def check_bind(address: str) -> str:
    """Valide l'adresse d'écoute : locale, privée, ou 0.0.0.0 (toutes les interfaces)."""
    try:
        ip = ipaddress.IPv4Address(address)
    except ipaddress.AddressValueError:
        raise ValueError(f"--bind {address} : adresse IPv4 attendue.") from None
    if not (ip.is_loopback or ip.is_unspecified or guard.is_rfc1918(ip)):
        raise ValueError(f"--bind {ip} refusé : le panel n'écoute que sur une adresse locale ou privée.")
    return str(ip)


class PanelHandler(BaseHTTPRequestHandler):
    """Une requête HTTP. `panel` est rempli par make_server()."""

    panel: Panel
    server_version = "stresser-panel"
    sys_version = ""

    def log_message(self, format, *args) -> None:   # noqa: A002 (nom imposé par http.server)
        pass    # pas de journal par requête : la page interroge le serveur chaque seconde

    # -- Réponses --
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")   # le jeton est dans l'adresse d'arrivée
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
            "connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'",
        )
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: dict) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message})

    # -- Contrôles communs --
    def _client(self) -> str:
        return self.client_address[0]

    def _authorized(self) -> bool:
        given = self.headers.get(TOKEN_HEADER, "")
        return hmac.compare_digest(given.encode("utf-8"), self.panel.token.encode("utf-8"))

    def _guard(self, api: bool) -> bool:
        """Vrai si la requête peut continuer ; sinon la réponse d'erreur est déjà partie."""
        if not client_allowed(self._client()):
            self._error(403, "Client refusé : adresse non privée.")
            return False
        if api and not self._authorized():
            self._error(401, "Jeton manquant ou invalide.")
            return False
        return True

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise RequestError("Content-Length invalide.") from None
        if length > MAX_BODY:
            raise RequestError("Requête trop volumineuse.", status=413)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, UnicodeDecodeError):
            raise RequestError("Corps de requête JSON invalide.") from None
        if not isinstance(payload, dict):
            raise RequestError("Objet JSON attendu.")
        return payload

    # -- Routes --
    def do_GET(self) -> None:   # noqa: N802 (nom imposé par http.server)
        url = urlsplit(self.path)
        if url.path in ("/", "/index.html"):
            if self._guard(api=False):
                # La page ne contient aucun secret : c'est l'API qui exige le jeton.
                self._send(200, PAGE_PATH.read_bytes(), "text/html; charset=utf-8")
        elif url.path == "/api/state":
            if self._guard(api=True):
                self._json(200, self.panel.describe())
        elif url.path == "/api/status":
            if self._guard(api=True):
                query = parse_qs(url.query)
                try:
                    since = max(0, int(query.get("since", ["0"])[0]))
                    number = int(query.get("job", ["0"])[0])
                except ValueError:
                    since, number = 0, 0
                self._json(200, self.panel.status(since, number))
        else:
            self._error(404, "Page inconnue.")

    def do_POST(self) -> None:   # noqa: N802
        path = urlsplit(self.path).path
        actions = {
            "/api/check": lambda body: self.panel.check(body),
            "/api/start": lambda body: self.panel.start(body, self._client()),
            "/api/stop": lambda body: self.panel.stop(self._client()),
        }
        if path not in actions:
            self._error(404, "Page inconnue.")
            return
        if not self._guard(api=True):
            return
        try:
            self._json(200, actions[path](self._body()))
        except RequestError as exc:
            self._error(exc.status, str(exc))


def make_server(panel: Panel, bind: str = "127.0.0.1", port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    """Crée le serveur HTTP du panel (sans le démarrer)."""
    handler = type("BoundPanelHandler", (PanelHandler,), {"panel": panel})
    server = ThreadingHTTPServer((bind, port), handler)
    server.daemon_threads = True
    return server


# --- Point d'entrée ------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="panel",
        description=(
            "Panel web de stresser : lance les tests de charge depuis un navigateur. "
            "Mêmes garde-fous que la ligne de commande (plages autorisées : "
            + guard.allowed_networks_text() + ")."
        ),
        epilog=(
            "Par défaut le panel n'est joignable que depuis ce PC. Pour l'utiliser depuis\n"
            "un autre poste, le plus sûr est un tunnel SSH :\n"
            "  ssh -L 8080:127.0.0.1:8080 utilisateur@vm\n"
            "puis ouvrir l'adresse affichée par le panel dans le navigateur du poste."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--bind", default="127.0.0.1", metavar="IP",
                        help="adresse d'écoute (défaut : 127.0.0.1, ce PC seulement) ; "
                             "0.0.0.0 pour toutes les interfaces")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, metavar="PORT",
                        help=f"port d'écoute (défaut : {DEFAULT_PORT})")
    parser.add_argument("--allow-host", action="append", metavar="IP", default=[],
                        help="autorise une IP PRIVÉE hors plages pour cette session (répétable)")
    parser.add_argument("--csv", type=Path, metavar="FICHIER",
                        help="ajoute le résumé de chaque test à ce fichier CSV")
    parser.add_argument("--inventory", type=Path, metavar="FICHIER", default=DEFAULT_INVENTORY,
                        help="fichier d'inventaire (défaut : inventory.txt à côté de panel.py)")
    parser.add_argument("--open", action="store_true",
                        help="ouvre le panel dans le navigateur par défaut")
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    args = build_parser().parse_args(argv)
    machines, inventory_warnings = load_inventory(args.inventory)
    for warning in inventory_warnings:
        print(f"Attention : {warning}", file=sys.stderr)
    try:
        bind = check_bind(args.bind)
        allow_hosts = guard.parse_allow_hosts(args.allow_host)
        if not 1 <= args.port <= 65535:
            raise ValueError("--port doit être compris entre 1 et 65535.")
    except ValueError as exc:   # TargetError en hérite
        print(f"Erreur : {exc}", file=sys.stderr)
        return 2

    panel = Panel(machines, allow_hosts, args.csv)
    try:
        server = make_server(panel, bind, args.port)
    except OSError as exc:
        print(f"Erreur : impossible d'écouter sur {bind}:{args.port} ({exc.strerror or exc}).",
              file=sys.stderr)
        return 2

    local = bind in ("127.0.0.1", "0.0.0.0")
    url = f"http://{'127.0.0.1' if local else bind}:{args.port}/?token={panel.token}"
    print(f"Panel stresser {__version__} démarré. Ouvre cette adresse :")
    print(f"  {url}")
    if bind == "127.0.0.1":
        print("Joignable depuis ce PC uniquement. Ctrl+C pour arrêter.")
    else:
        print(f"ATTENTION : le panel écoute sur {bind}. Toute personne de ton réseau qui "
              "connaît le jeton peut lancer un test.")
        print("Depuis un autre poste, remplace l'adresse par celle de cette machine. "
              "Le jeton circule en clair (HTTP) : préfère un tunnel SSH.")
        print("Ctrl+C pour arrêter.")
    sys.stdout.flush()      # sortie redirigée (service, fichier) : l'adresse doit apparaître tout de suite
    if args.open:
        webbrowser.open(url)

    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\nArrêt du panel...")
    finally:
        panel.shutdown()
        server.server_close()
        time.sleep(0.1)     # laisse le thread du test écrire sa dernière ligne de journal
    return 0
