"""Mode UDP : envoi de paquets à un débit cible, avec deux threads.

- Le thread d'ENVOI cadence les paquets avec un « seau à jetons » (token bucket).
- Le thread de RÉCEPTION compte ce que le service renvoie, s'il renvoie quelque chose.
- Le thread principal rafraîchit l'affichage et attend Ctrl+C.

Pourquoi des threads plutôt qu'asyncio : il n'y a qu'une seule socket, donc rien
à multiplexer. Ce qui compte ici, c'est la précision de la cadence, et une boucle
dédiée la tient mieux qu'une boucle d'événements partagée.

Le seau à jetons : le seau se remplit de `rate` jetons par seconde et chaque
paquet envoyé en consomme un. Plutôt que de dormir entre deux paquets (l'horloge
de Windows a une précision d'environ 15 ms, trop grossière), on se réveille
régulièrement et on envoie d'un coup tous les paquets « dus » depuis le dernier
réveil. Le seau est plafonné : après un blocage, on ne rattrape pas le retard par
une rafale énorme.
"""

from __future__ import annotations

import signal
import socket
import threading
import time
from contextlib import suppress

from .config import RunConfig
from .stats import UdpStats

_TICK = 0.005            # intervalle de réveil du thread d'envoi (5 ms)
_MAX_BURST_SECONDS = 0.05   # le seau contient au plus 50 ms de paquets
_SOCKET_TIMEOUT = 0.2    # délai des appels bloquants, pour rester réactif à l'arrêt
_REPLY_GRACE = 0.3       # attente des dernières réponses après la fin de l'envoi
_RECV_BUFFER = 65_535

# Quand rien n'écoute sur le port, la cible répond par un message ICMP « port
# injoignable ». Le système le signale par une de ces deux erreurs sur la socket
# (ConnectionRefused sous Linux, ConnectionReset sous Windows).
_PORT_UNREACHABLE = (ConnectionRefusedError, ConnectionResetError)


def _send_loop(sock: socket.socket, cfg: RunConfig, stats: UdpStats, stop: threading.Event) -> None:
    """Thread d'envoi : émet `rate` paquets par seconde jusqu'à la fin du test."""
    payload = (b"0123456789abcdef" * (cfg.size // 16 + 1))[:cfg.size]
    max_tokens = max(1.0, cfg.rate * _MAX_BURST_SECONDS)
    tokens = 0.0
    last = time.perf_counter()
    deadline = last + cfg.duration

    while not stop.is_set():
        now = time.perf_counter()
        if now >= deadline:
            break
        # Remplissage du seau en fonction du temps écoulé, puis envoi des paquets dus.
        tokens = min(tokens + (now - last) * cfg.rate, max_tokens)
        last = now
        due = int(tokens)
        tokens -= due

        sent = 0
        for _ in range(due):
            try:
                sock.send(payload)
                sent += 1
            except _PORT_UNREACHABLE:
                stats.unreachable_on_send += 1
            except TimeoutError:
                stats.send_errors["tampon d'envoi plein"] += 1
            except OSError as exc:
                stats.send_errors[exc.strerror or type(exc).__name__] += 1
                break   # erreur inattendue : on abandonne cette rafale
        stats.sent += sent
        stats.bytes_sent += sent * len(payload)

        stop.wait(_TICK)


def _recv_loop(sock: socket.socket, stats: UdpStats, stop: threading.Event) -> None:
    """Thread de réception : compte les paquets renvoyés par la cible."""
    while not stop.is_set():
        try:
            data = sock.recv(_RECV_BUFFER)
        except TimeoutError:
            continue    # rien reçu pendant _SOCKET_TIMEOUT : on revérifie `stop`
        except _PORT_UNREACHABLE:
            stats.unreachable_on_recv += 1
            continue
        except OSError:
            break       # socket fermée
        stats.received += 1
        stats.bytes_recv += len(data)


def run_udp(cfg: RunConfig, display) -> UdpStats:
    """Lance le test UDP et retourne les statistiques (partielles si interrompu)."""
    stats = UdpStats()
    stop_send = threading.Event()
    stop_recv = threading.Event()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    # connect() en UDP n'envoie rien : il fixe la destination, ce qui permet
    # d'utiliser send()/recv() et de ne recevoir que les réponses de la cible.
    sock.connect((str(cfg.ip), cfg.port))
    sock.settimeout(_SOCKET_TIMEOUT)

    sender = threading.Thread(target=_send_loop, args=(sock, cfg, stats, stop_send), daemon=True)
    receiver = threading.Thread(target=_recv_loop, args=(sock, stats, stop_recv), daemon=True)

    # SIGTERM (arrêt de la VM, `kill`) : même arrêt propre que Ctrl+C.
    def on_sigterm(signum, frame) -> None:
        stats.interrupted = True
        stop_send.set()

    previous_handler = None
    with suppress(ValueError, OSError):   # ValueError si on n'est pas dans le thread principal
        previous_handler = signal.signal(signal.SIGTERM, on_sigterm)

    display.start(f"Test UDP vers {cfg.ip}:{cfg.port} ({cfg.display_name}) - Ctrl+C pour arrêter")
    stats.started = time.perf_counter()
    receiver.start()
    sender.start()
    try:
        next_refresh = stats.started + 1.0
        while sender.is_alive():
            # time.sleep() par petites tranches : contrairement à Thread.join(),
            # il est interrompu tout de suite par Ctrl+C, y compris sous Windows.
            time.sleep(0.1)
            if time.perf_counter() >= next_refresh:
                next_refresh += 1.0
                display.update(stats.live_fields(cfg))
    except KeyboardInterrupt:
        stats.interrupted = True
    finally:
        try:
            stop_send.set()
            sender.join(timeout=2.0)
            stats.ended = time.perf_counter()
            # Les réponses aux derniers paquets peuvent encore arriver.
            time.sleep(_REPLY_GRACE)
        except KeyboardInterrupt:
            stats.interrupted = True    # second Ctrl+C : on n'attend plus
        finally:
            if not stats.ended:
                stats.ended = time.perf_counter()
            stop_recv.set()
            receiver.join(timeout=2.0)
            sock.close()
            if previous_handler is not None:
                signal.signal(signal.SIGTERM, previous_handler)
            display.stop()
    return stats
