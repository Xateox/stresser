"""Tests des moteurs TCP et UDP contre de petits serveurs locaux (127.0.0.1).

Les moteurs sont appelés directement, sans passer par la CLI : celle-ci refuse
127.0.0.1, ce qui est vérifié dans test_cli.py. Rien ne sort de la machine.
"""

import contextlib
import io
import ipaddress
import signal
import socket
import socketserver
import sys
import threading
import time
import unittest
from unittest import mock

from stresser import cli, guard
from stresser.config import RunConfig
from stresser.tcp import classify_error, run_tcp
from stresser.udp import run_udp

LOCALHOST = ipaddress.IPv4Address("127.0.0.1")


class FakeDisplay:
    """Affichage muet qui retient ce qu'on lui a demandé d'afficher."""

    def __init__(self):
        self.updates = []
        self.stopped = False

    def start(self, title):
        pass

    def update(self, fields):
        self.updates.append(fields)

    def stop(self):
        self.stopped = True


class _EchoHandler(socketserver.BaseRequestHandler):
    def handle(self):
        with contextlib.suppress(OSError):
            while data := self.request.recv(65536):
                self.request.sendall(data)


class _EchoServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    request_queue_size = 256


def free_port(kind=socket.SOCK_STREAM):
    """Un port local sur lequel rien n'écoute."""
    with socket.socket(socket.AF_INET, kind) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def interrupt_after(delay, signum=signal.SIGINT):
    """Simule un Ctrl+C (ou un autre signal) après `delay` secondes."""
    timer = threading.Timer(delay, signal.raise_signal, args=(signum,))
    timer.daemon = True
    timer.start()
    return timer


class TcpEngineTests(unittest.TestCase):
    def setUp(self):
        self.server = _EchoServer(("127.0.0.1", 0), _EchoHandler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def cfg(self, **overrides):
        values = dict(ip=LOCALHOST, port=self.port, proto="tcp", duration=1.0, concurrency=5)
        values.update(overrides)
        return RunConfig(**values)

    def test_connecter_puis_fermer(self):
        display = FakeDisplay()
        stats = run_tcp(self.cfg(), display)
        self.assertGreater(stats.succeeded, 0)
        self.assertEqual(stats.failed, 0)
        self.assertEqual(stats.attempted, stats.succeeded + stats.failed + stats.abandoned)
        self.assertEqual(len(stats.latencies_ms), stats.succeeded)
        self.assertEqual(stats.active, 0)               # toutes les connexions sont refermées
        self.assertLessEqual(stats.peak_active, 5)
        self.assertFalse(stats.interrupted)
        self.assertAlmostEqual(stats.elapsed(), 1.0, delta=0.5)
        self.assertTrue(display.stopped)

    def test_charge_utile_et_reponse(self):
        stats = run_tcp(self.cfg(size=100, hold=0.1), FakeDisplay())
        self.assertGreater(stats.bytes_sent, 0)
        self.assertEqual(stats.bytes_sent % 100, 0)
        self.assertGreater(stats.bytes_recv, 0)         # le serveur d'écho a répondu
        self.assertLessEqual(stats.bytes_recv, stats.bytes_sent)

    def test_maintien_des_connexions(self):
        # hold plus long que le test : chaque worker garde UNE connexion ouverte.
        stats = run_tcp(self.cfg(concurrency=8, hold=30), FakeDisplay())
        self.assertEqual(stats.succeeded, 8)
        self.assertEqual(stats.peak_active, 8)
        self.assertEqual(stats.active, 0)
        self.assertAlmostEqual(stats.elapsed(), 1.0, delta=0.5)

    def test_montee_progressive(self):
        display = FakeDisplay()
        stats = run_tcp(self.cfg(concurrency=10, hold=30, duration=2.0, ramp=True), display)
        self.assertEqual(stats.peak_active, 10)
        # Après 1 s, seule la moitié environ des workers a démarré.
        opened_at_first_refresh = int(dict(display.updates[0])["ouvertes"])
        self.assertLess(opened_at_first_refresh, 10)

    def test_affichage_rafraichi_chaque_seconde(self):
        display = FakeDisplay()
        run_tcp(self.cfg(duration=2.5), display)
        self.assertEqual(len(display.updates), 2)
        self.assertIn("conn/s", dict(display.updates[0]))

    def test_port_ferme(self):
        stats = run_tcp(self.cfg(port=free_port(), duration=1.0, timeout=0.5), FakeDisplay())
        self.assertEqual(stats.succeeded, 0)
        self.assertGreater(stats.failed, 0)
        self.assertTrue(set(stats.connect_errors) <= {"refusée", "timeout"}, stats.connect_errors)
        self.assertEqual(sum(stats.connect_errors.values()), stats.failed)

    def test_ctrl_c_arret_propre_avec_resultats_partiels(self):
        interrupt_after(0.7)
        begin = time.perf_counter()
        stats = run_tcp(self.cfg(duration=30, concurrency=10, hold=30), FakeDisplay())
        self.assertLess(time.perf_counter() - begin, 5)   # n'a pas attendu les 30 s
        self.assertTrue(stats.interrupted)
        self.assertEqual(stats.succeeded, 10)
        self.assertEqual(stats.active, 0)                 # sockets refermées
        self.assertIn("résultats partiels", stats.summary_lines(self.cfg())[0])

    @unittest.skipIf(sys.platform == "win32", "SIGTERM n'est pas géré par asyncio sous Windows")
    def test_sigterm_arret_propre(self):
        interrupt_after(0.7, signal.SIGTERM)
        stats = run_tcp(self.cfg(duration=30, hold=30), FakeDisplay())
        self.assertTrue(stats.interrupted)
        self.assertEqual(stats.active, 0)
        self.assertLess(stats.elapsed(), 5)


class ClassifyErrorTests(unittest.TestCase):
    def test_categories(self):
        import errno
        self.assertEqual(classify_error(ConnectionRefusedError()), "refusée")
        self.assertEqual(classify_error(TimeoutError()), "timeout")
        self.assertEqual(classify_error(ConnectionResetError()), "reset")
        self.assertEqual(classify_error(OSError(errno.EMFILE, "trop de fichiers")), "locale (client)")
        self.assertEqual(classify_error(OSError(errno.EHOSTUNREACH, "pas de route")), "injoignable")
        self.assertEqual(classify_error(OSError("mystère")), "autre")


class UdpEngineTests(unittest.TestCase):
    def setUp(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.running = True
        self.thread = threading.Thread(target=self._echo, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)

    def _echo(self):
        while self.running:
            try:
                data, peer = self.sock.recvfrom(65535)
                self.sock.sendto(data, peer)
            except OSError:
                continue

    def _stop(self):
        self.running = False
        self.thread.join(timeout=2)
        self.sock.close()

    def cfg(self, **overrides):
        values = dict(ip=LOCALHOST, port=self.port, proto="udp", duration=1.0, rate=500, size=64)
        values.update(overrides)
        return RunConfig(**values)

    def test_debit_cible_respecte(self):
        stats = run_udp(self.cfg(), FakeDisplay())
        self.assertAlmostEqual(stats.sent, 500, delta=75)      # 500 paquets/s pendant 1 s
        self.assertEqual(stats.bytes_sent, stats.sent * 64)
        self.assertFalse(stats.interrupted)

    def test_reponses_comptees(self):
        stats = run_udp(self.cfg(), FakeDisplay())
        self.assertGreater(stats.received, stats.sent * 0.9)   # le serveur d'écho renvoie tout
        self.assertEqual(stats.bytes_recv, stats.received * 64)

    def test_debit_faible(self):
        stats = run_udp(self.cfg(rate=10, duration=1.0), FakeDisplay())
        self.assertAlmostEqual(stats.sent, 10, delta=2)

    def test_port_ferme_aucune_reponse(self):
        stats = run_udp(self.cfg(port=free_port(socket.SOCK_DGRAM)), FakeDisplay())
        self.assertEqual(stats.received, 0)
        self.assertGreater(stats.sent + stats.unreachable, 0)
        self.assertIn("ne garantit ni la livraison", "\n".join(stats.summary_lines(self.cfg())))

    def test_affichage_rafraichi(self):
        display = FakeDisplay()
        run_udp(self.cfg(duration=2.5), display)
        self.assertEqual(len(display.updates), 2)
        self.assertIn("paquets/s", dict(display.updates[0]))

    def test_ctrl_c_arret_propre_avec_resultats_partiels(self):
        interrupt_after(0.7)
        begin = time.perf_counter()
        display = FakeDisplay()
        stats = run_udp(self.cfg(duration=30), display)
        self.assertLess(time.perf_counter() - begin, 5)
        self.assertTrue(stats.interrupted)
        self.assertGreater(stats.sent, 0)
        self.assertTrue(display.stopped)

    @unittest.skipIf(sys.platform == "win32", "SIGTERM ne peut pas être simulé ainsi sous Windows")
    def test_sigterm_arret_propre(self):
        interrupt_after(0.7, signal.SIGTERM)
        stats = run_udp(self.cfg(duration=30), FakeDisplay())
        self.assertTrue(stats.interrupted)
        self.assertLess(stats.elapsed(), 5)


class EndToEndTests(unittest.TestCase):
    """Toute la chaîne, de la ligne de commande au résumé, sur 127.0.0.1.

    La plage autorisée est remplacée le temps du test : c'est le seul moyen de
    faire accepter le loopback à la CLI.
    """

    def test_cli_complete_en_tcp(self):
        server = _EchoServer(("127.0.0.1", 0), _EchoHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        out = io.StringIO()
        loopback = (ipaddress.ip_network("127.0.0.0/24"),)
        with mock.patch.object(guard, "ALLOWED_NETWORKS", loopback), contextlib.redirect_stdout(out):
            code = cli.main(["-t", "127.0.0.1", "-p", str(server.server_address[1]),
                             "--duration", "1.5", "--concurrency", "4", "--yes", "--plain"])
        text = out.getvalue()
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("Récapitulatif", text)
        self.assertIn("conn/s", text)              # ligne d'affichage en direct
        self.assertIn("===== Résumé TCP =====", text)
        self.assertIn("Latence de connexion", text)


if __name__ == "__main__":
    unittest.main()
