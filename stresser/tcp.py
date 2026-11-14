"""Mode TCP : N connexions simultanées vers IP:port, avec asyncio.

Principe : on lance `concurrency` « workers ». Chaque worker répète en boucle,
jusqu'à la fin du test :
    1. ouvrir une connexion (on chronomètre : c'est la latence de connexion) ;
    2. envoyer `size` octets si demandé ;
    3. garder la connexion ouverte `hold` secondes en comptant ce qui est reçu ;
    4. fermer, puis recommencer.

Avec --hold 0 on mesure la cadence d'acceptation de connexions du service ; avec
un --hold égal à la durée du test, on mesure combien de connexions il garde
ouvertes en même temps.

Pourquoi asyncio plutôt que des threads : une connexion en attente ne coûte
qu'une petite tâche, pas un thread système. On tient ainsi des milliers de
connexions dans un seul thread, et les compteurs n'ont pas besoin de verrous.

Boucle d'événements : on garde celle par défaut de chaque système.
- Linux   : SelectorEventLoop (epoll), sans limite propre ; c'est `ulimit -n`
            qui plafonne (voir limits.py).
- Windows : ProactorEventLoop (IOCP). Il ne faut PAS forcer SelectorEventLoop
            sous Windows : elle repose sur select(), limité à 512 sockets.
"""

from __future__ import annotations

import asyncio
import errno
import signal
import threading
import time
from contextlib import suppress

from .config import RunConfig
from .stats import TcpStats

_FAIL_BACKOFF = 0.05    # pause après un échec, pour ne pas boucler à vide sur un port fermé
_CLOSE_TIMEOUT = 1.0    # délai accordé à une fermeture propre avant de couper net
_READ_CHUNK = 65_536


def _codes(*names: str) -> frozenset[int]:
    """Codes errno existant sur ce système (certains noms manquent sous Windows)."""
    return frozenset(code for name in names if (code := getattr(errno, name, None)) is not None)


# Erreurs qui viennent du client : plus de descripteurs, plus de ports source, plus de mémoire.
_LOCAL_ERRNOS = _codes("EMFILE", "ENFILE", "EADDRNOTAVAIL", "EADDRINUSE", "ENOBUFS", "ENOMEM")
_LOCAL_WINERRORS = frozenset({10024, 10048, 10049, 10055})
# Erreurs de routage : la cible ou son réseau n'est pas joignable.
_UNREACHABLE_ERRNOS = _codes("ENETUNREACH", "EHOSTUNREACH", "ENETDOWN", "EHOSTDOWN")
_UNREACHABLE_WINERRORS = frozenset({1231, 1232, 10050, 10051, 10064, 10065})
_TIMEOUT_WINERRORS = frozenset({121, 10060})


def classify_error(exc: BaseException) -> str:
    """Range une exception réseau dans une catégorie lisible pour le résumé."""
    winerror = getattr(exc, "winerror", None)
    code = getattr(exc, "errno", None)
    if isinstance(exc, ConnectionRefusedError):
        return "refusée"            # rien n'écoute, ou file d'attente du serveur pleine
    if isinstance(exc, TimeoutError) or winerror in _TIMEOUT_WINERRORS:
        return "timeout"            # pas de réponse dans le délai --timeout
    if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
        return "reset"              # le serveur a coupé la connexion
    if code in _LOCAL_ERRNOS or winerror in _LOCAL_WINERRORS:
        return "locale (client)"    # limite atteinte sur CETTE machine
    if code in _UNREACHABLE_ERRNOS or winerror in _UNREACHABLE_WINERRORS:
        return "injoignable"
    return "autre"


async def _close(writer: asyncio.StreamWriter) -> None:
    """Ferme une connexion proprement, ou la coupe net si le serveur ne suit pas."""
    writer.close()
    try:
        await asyncio.wait_for(writer.wait_closed(), _CLOSE_TIMEOUT)
    except (OSError, TimeoutError):
        writer.transport.abort()


async def _one_connection(cfg: RunConfig, stats: TcpStats, deadline: float, payload: bytes) -> None:
    """Un cycle complet : connexion, envoi éventuel, maintien, fermeture."""
    stats.attempted += 1
    begin = time.perf_counter()
    try:
        async with asyncio.timeout(cfg.timeout):
            reader, writer = await asyncio.open_connection(str(cfg.ip), cfg.port)
    except asyncio.CancelledError:
        stats.abandoned += 1    # le test s'arrête alors que la connexion n'a pas abouti
        raise
    except OSError as exc:      # TimeoutError et ConnectionRefusedError en font partie
        stats.failed += 1
        stats.connect_errors[classify_error(exc)] += 1
        await asyncio.sleep(_FAIL_BACKOFF)
        return

    stats.latencies_ms.append((time.perf_counter() - begin) * 1000)
    stats.succeeded += 1
    stats.active += 1
    stats.peak_active = max(stats.peak_active, stats.active)
    try:
        if payload:
            writer.write(payload)
            await writer.drain()    # attend que les octets soient réellement partis
            stats.bytes_sent += len(payload)

        # Maintien de la connexion : on lit ce que le serveur renvoie, sans
        # jamais dépasser la fin du test.
        hold_end = min(time.perf_counter() + cfg.hold, deadline)
        while (remaining := hold_end - time.perf_counter()) > 0:
            try:
                async with asyncio.timeout(remaining):
                    data = await reader.read(_READ_CHUNK)
            except TimeoutError:
                break
            if not data:            # lecture vide = le serveur a fermé de son côté
                stats.closed_by_peer += 1
                break
            stats.bytes_recv += len(data)
    except OSError as exc:
        stats.io_errors[classify_error(exc)] += 1
    finally:
        stats.active -= 1
        await _close(writer)


async def _worker(cfg: RunConfig, stats: TcpStats, deadline: float, start_delay: float, payload: bytes) -> None:
    """Enchaîne les connexions jusqu'à la fin du test."""
    if start_delay > 0:             # option --ramp : ce worker démarre plus tard
        await asyncio.sleep(start_delay)
    while time.perf_counter() < deadline:
        await _one_connection(cfg, stats, deadline, payload)


async def _reporter(cfg: RunConfig, stats: TcpStats, display) -> None:
    """Rafraîchit l'affichage une fois par seconde."""
    while True:
        await asyncio.sleep(1.0)
        display.update(stats.live_fields(cfg))


async def _watch_external_stop(external: threading.Event, stop: asyncio.Event) -> None:
    """Relaie une demande d'arrêt venue d'un autre thread (bouton du panel web)."""
    while not external.is_set():
        await asyncio.sleep(0.1)
    stop.set()


async def _run(cfg: RunConfig, stats: TcpStats, display, external_stop: threading.Event | None = None) -> None:
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    # SIGTERM (arrêt de la VM, `kill`) : même arrêt propre que Ctrl+C. Windows ne
    # sait pas brancher un signal sur la boucle, d'où le suppress.
    with suppress(NotImplementedError, AttributeError, ValueError, RuntimeError):
        loop.add_signal_handler(signal.SIGTERM, stop.set)

    # Contenu sans importance : un motif répété, tronqué à la taille demandée.
    payload = (b"0123456789abcdef" * (cfg.size // 16 + 1))[:cfg.size]

    stats.started = time.perf_counter()
    deadline = stats.started + cfg.duration
    workers = [
        asyncio.create_task(_worker(
            cfg, stats, deadline,
            # --ramp : départs répartis sur la durée, de 1 worker à `concurrency`.
            cfg.duration * index / cfg.concurrency if cfg.ramp else 0.0,
            payload,
        ))
        for index in range(cfg.concurrency)
    ]
    stopper = asyncio.create_task(stop.wait())
    helpers = [asyncio.create_task(_reporter(cfg, stats, display)), stopper]
    if external_stop is not None:
        helpers.append(asyncio.create_task(_watch_external_stop(external_stop, stop)))
    try:
        # On attend la fin du test, ou une demande d'arrêt (stop.wait() se termine alors).
        await asyncio.wait([stopper], timeout=cfg.duration)
        stats.interrupted = stop.is_set()
    except asyncio.CancelledError:
        # Ctrl+C : asyncio.run() annule cette tâche. On note l'interruption et on
        # laisse le bloc finally fermer toutes les connexions.
        stats.interrupted = True
    finally:
        stats.ended = time.perf_counter()
        tasks = workers + helpers
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        with suppress(NotImplementedError, AttributeError, ValueError, RuntimeError):
            loop.remove_signal_handler(signal.SIGTERM)


def run_tcp(
    cfg: RunConfig,
    display,
    stop_event: threading.Event | None = None,
    stats: TcpStats | None = None,
) -> TcpStats:
    """Lance le test TCP et retourne les statistiques (partielles si interrompu).

    `stop_event` et `stats` servent au panel web : le premier permet d'arrêter le
    test depuis un autre thread, le second de lire les compteurs pendant le test.
    """
    if stats is None:
        stats = TcpStats()
    display.start(f"Test TCP vers {cfg.ip}:{cfg.port} ({cfg.display_name}) - Ctrl+C pour arrêter")
    try:
        asyncio.run(_run(cfg, stats, display, stop_event))
    except KeyboardInterrupt:
        # Second Ctrl+C pendant le nettoyage : on rend la main tout de suite.
        stats.interrupted = True
    finally:
        if not stats.ended:
            stats.ended = time.perf_counter()
        display.stop()
    return stats
