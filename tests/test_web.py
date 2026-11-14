"""Tests du panel web : garde-fous, jeton d'accès, et un test complet sur 127.0.0.1.

Le serveur du panel est démarré sur un port libre de 127.0.0.1 et interrogé en
HTTP, comme le ferait la page. Rien ne sort de la machine.
"""

import contextlib
import io
import ipaddress
import json
import socketserver
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

from stresser import guard, web
from stresser.inventory import parse_inventory

INVENTORY = """\
192.168.35.11  proxmox  [critique]
192.168.35.14  home-assistant
10.10.10.21    gitea
"""
LOOPBACK = (ipaddress.ip_network("127.0.0.0/24"),)


class _EchoHandler(socketserver.BaseRequestHandler):
    def handle(self):
        with contextlib.suppress(OSError):
            while data := self.request.recv(65536):
                self.request.sendall(data)


class _EchoServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    request_queue_size = 256


class PanelTestCase(unittest.TestCase):
    """Démarre un panel par test et fournit de quoi l'interroger."""

    def setUp(self):
        machines, _ = parse_inventory(INVENTORY)
        self.panel = web.Panel(machines, token="jeton-de-test")
        self.server = web.make_server(self.panel, "127.0.0.1", 0)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.panel.shutdown)
        # Le journal du panel (lancement, arrêt) n'a pas à polluer la sortie des tests.
        silence = mock.patch.object(web, "log")
        silence.start()
        self.addCleanup(silence.stop)

    def call(self, path, body=None, token="jeton-de-test", raw=None):
        """Retourne (code HTTP, réponse décodée)."""
        headers = {}
        if token is not None:
            headers[web.TOKEN_HEADER] = token
        data = raw
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base + path, data=data, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                content, status, kind = response.read(), response.status, response.headers["Content-Type"]
        except urllib.error.HTTPError as error:
            with error:     # une réponse d'erreur est aussi une connexion à refermer
                content, status, kind = error.read(), error.code, error.headers["Content-Type"]
        if kind.startswith("application/json"):
            return status, json.loads(content)
        return status, content.decode("utf-8")

    def wait_until_finished(self, timeout=10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            _, status = self.call("/api/status")
            if status["job"] and status["job"]["state"] != "running":
                return status
            time.sleep(0.1)
        self.fail("le test ne s'est pas terminé à temps")


class AccessTests(PanelTestCase):
    def test_la_page_est_servie_sans_jeton_et_ne_le_contient_pas(self):
        status, page = self.call("/", token=None)
        self.assertEqual(status, 200)
        self.assertIn("<title>stresser", page)
        self.assertNotIn("jeton-de-test", page)

    def test_api_refusee_sans_jeton_ou_avec_un_mauvais(self):
        for token in (None, "", "mauvais", "jeton-de-tes"):
            for path, body in (("/api/state", None), ("/api/status", None),
                               ("/api/check", {}), ("/api/start", {}), ("/api/stop", {})):
                with self.subTest(token=token, path=path):
                    status, answer = self.call(path, body, token=token)
                    self.assertEqual(status, 401)
                    self.assertIn("error", answer)
        self.assertIsNone(self.panel.job)

    def test_etat_initial(self):
        status, state = self.call("/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(state["networks"], ["192.168.35.0/24"])
        # gitea (10.10.10.21) est hors plage : il n'est pas proposé.
        self.assertEqual([m["name"] for m in state["machines"]], ["proxmox", "home-assistant"])
        self.assertTrue(state["machines"][0]["critical"])
        _, current = self.call("/api/status")
        self.assertEqual(current, {"job": None, "history": []})

    def test_page_inconnue(self):
        self.assertEqual(self.call("/nimporte")[0], 404)
        self.assertEqual(self.call("/api/nimporte", {})[0], 404)

    def test_corps_invalide(self):
        self.assertEqual(self.call("/api/check", raw=b"{pas du json")[0], 400)
        self.assertEqual(self.call("/api/check", raw=b"[1, 2]")[0], 400)
        self.assertEqual(self.call("/api/check", raw=b" " * (web.MAX_BODY + 1))[0], 413)

    def test_filtre_des_clients(self):
        for address in ("127.0.0.1", "192.168.35.20", "10.0.0.5", "::1"):
            self.assertTrue(web.client_allowed(address), address)
        for address in ("8.8.8.8", "100.64.0.1", "2001:db8::1", "n'importe quoi"):
            self.assertFalse(web.client_allowed(address), address)

    def test_adresse_d_ecoute(self):
        for address in ("127.0.0.1", "0.0.0.0", "192.168.35.20"):
            self.assertEqual(web.check_bind(address), address)
        for address in ("8.8.8.8", "::", "panel.lan"):
            with self.subTest(address=address), self.assertRaises(ValueError):
                web.check_bind(address)


class GuardTests(PanelTestCase):
    """Le panel applique les mêmes refus que la ligne de commande, côté serveur."""

    VALID = {"target": "192.168.35.14", "port": 8123, "proto": "tcp", "duration": 1, "confirmed": True}

    def assert_refused(self, **changes):
        body = {**self.VALID, **changes}
        for path in ("/api/check", "/api/start"):
            with self.subTest(path=path, changes=changes):
                status, answer = self.call(path, body)
                self.assertEqual(status, 400, answer)
                self.assertIn("error", answer)
        self.assertIsNone(self.panel.job)   # rien n'a été lancé

    def test_cibles_refusees(self):
        for target in ("8.8.8.8", "10.10.10.21", "10.30.30.5", "127.0.0.1", "192.168.35.255",
                       "gitea.lan", "", None, 42, ["192.168.35.14"]):
            self.assert_refused(target=target)

    def test_parametres_refuses(self):
        self.assert_refused(port=None)
        self.assert_refused(port=0)
        self.assert_refused(port=70000)
        self.assert_refused(port="8123")            # texte au lieu d'un nombre
        self.assert_refused(port=True)
        self.assert_refused(port=80.5)
        self.assert_refused(proto="icmp")
        self.assert_refused(duration=0)
        self.assert_refused(duration=-1)
        self.assert_refused(concurrency=0)
        self.assert_refused(concurrency=guard.MAX_CONCURRENCY + 1)
        self.assert_refused(proto="udp", rate=guard.MAX_RATE + 1)
        self.assert_refused(proto="udp", size=guard.MAX_UDP_SIZE + 1)
        self.assert_refused(size=-1)
        self.assert_refused(hold=-1)
        self.assert_refused(timeout=0)

    def test_valeurs_non_finies_refusees(self):
        # json.loads de Python accepte NaN et Infinity : une durée infinie doit être refusée.
        for raw in (b'{"target": "192.168.35.14", "port": 80, "duration": Infinity, "confirmed": true}',
                    b'{"target": "192.168.35.14", "port": 80, "duration": NaN, "confirmed": true}',
                    b'{"target": "192.168.35.14", "port": 80, "concurrency": 1e400, "confirmed": true}'):
            with self.subTest(raw=raw):
                self.assertEqual(self.call("/api/start", raw=raw)[0], 400)
        self.assertIsNone(self.panel.job)

    def test_recapitulatif_sans_lancement(self):
        status, recap = self.call("/api/check", {"target": "192.168.35.14", "port": 8123})
        self.assertEqual(status, 200)
        self.assertEqual((recap["target"], recap["name"], recap["port"], recap["proto"]),
                         ("192.168.35.14", "home-assistant", 8123, "TCP"))
        self.assertEqual(recap["intensity"], "50 connexions simultanées, connecter puis fermer")
        self.assertEqual(recap["duration"], "10 s")
        self.assertEqual(recap["warnings"], [])
        self.assertIsNone(self.panel.job)

    def test_avertissement_au_dela_de_la_borne(self):
        _, recap = self.call("/api/check", {**self.VALID, "concurrency": 5000})
        self.assertTrue(any("Concurrence élevée" in warning for warning in recap["warnings"]))

    def test_confirmation_exigee_par_le_serveur(self):
        for confirmed in (None, False, "true", 1):
            with self.subTest(confirmed=confirmed):
                status, _ = self.call("/api/start", {**self.VALID, "confirmed": confirmed})
                self.assertEqual(status, 400)
        self.assertIsNone(self.panel.job)

    def test_machine_critique_exige_son_nom(self):
        body = {**self.VALID, "target": "192.168.35.11"}
        _, recap = self.call("/api/check", body)
        self.assertTrue(recap["critical"])
        self.assertEqual(recap["confirm_name"], "proxmox")
        for name in (None, "", "o", "home-assistant"):
            with self.subTest(name=name):
                status, _ = self.call("/api/start", {**body, "confirm_name": name})
                self.assertEqual(status, 400)
        self.assertIsNone(self.panel.job)

    def test_allow_host_ne_vient_que_du_demarrage(self):
        # Un champ allow_hosts glissé dans la demande n'a aucun effet.
        self.assert_refused(target="10.10.10.21", allow_hosts=["10.10.10.21"], allow_host="10.10.10.21")
        self.panel.allow_hosts = guard.parse_allow_hosts(["10.10.10.21"])
        status, recap = self.call("/api/check", {**self.VALID, "target": "10.10.10.21"})
        self.assertEqual((status, recap["name"]), (200, "gitea"))

    def test_arret_sans_test_en_cours(self):
        self.assertEqual(self.call("/api/stop", {})[0], 409)


class RunTests(PanelTestCase):
    """Un vrai test, de la demande HTTP au résumé, contre un serveur d'écho local."""

    def setUp(self):
        super().setUp()
        self.echo = _EchoServer(("127.0.0.1", 0), _EchoHandler)
        threading.Thread(target=self.echo.serve_forever, daemon=True).start()
        self.addCleanup(self.echo.server_close)
        self.addCleanup(self.echo.shutdown)
        patch = mock.patch.object(guard, "ALLOWED_NETWORKS", LOOPBACK)
        patch.start()
        self.addCleanup(patch.stop)

    def request(self, **changes):
        # hold=0.02 bride la cadence, pour ne pas épuiser les ports source de la machine de test.
        return {"target": "127.0.0.1", "port": self.echo.server_address[1], "proto": "tcp",
                "duration": 2.5, "concurrency": 4, "hold": 0.02, "confirmed": True, **changes}

    def test_test_complet(self):
        status, job = self.call("/api/start", self.request())
        self.assertEqual(status, 200)
        self.assertEqual((job["state"], job["number"], job["target"]), ("running", 1, "127.0.0.1"))

        final = self.wait_until_finished()
        job = final["job"]
        self.assertEqual((job["state"], job["status"], job["interrupted"]), ("done", "terminé", False))
        self.assertEqual(job["series"], ["Connexions réussies/s", "Échecs/s"])
        self.assertGreaterEqual(len(job["samples"]), 2)
        self.assertEqual(job["sample_count"], len(job["samples"]))
        self.assertGreater(job["samples"][0][1], 0)         # des connexions ont réussi
        fields = dict(job["fields"])
        self.assertIn("réussies", fields)
        self.assertGreater(float(fields["conn/s moy."]), 0)     # moyenne du test, pas 0 après l'arrêt
        self.assertIn("===== Résumé TCP =====", job["summary"][0])
        self.assertEqual(len(final["history"]), 1)
        self.assertEqual(final["history"][0]["status"], "terminé")
        self.assertEqual(self.panel.job.stats.active, 0)    # sockets refermées

    def test_points_envoyes_une_seule_fois(self):
        self.call("/api/start", self.request())
        final = self.wait_until_finished()["job"]
        total = final["sample_count"]
        _, partial = self.call(f"/api/status?job=1&since={total - 1}")
        self.assertEqual(len(partial["job"]["samples"]), 1)
        # Numéro de test inconnu de la page : le serveur renvoie tout.
        _, full = self.call(f"/api/status?job=99&since={total}")
        self.assertEqual(len(full["job"]["samples"]), total)

    def test_un_seul_test_a_la_fois_puis_arret(self):
        self.assertEqual(self.call("/api/start", self.request(duration=30, hold=30))[0], 200)
        status, answer = self.call("/api/start", self.request())
        self.assertEqual(status, 409)
        self.assertIn("déjà en cours", answer["error"])

        begin = time.monotonic()
        self.assertEqual(self.call("/api/stop", {})[0], 200)
        final = self.wait_until_finished()
        self.assertLess(time.monotonic() - begin, 5)        # n'a pas attendu les 30 s
        self.assertEqual((final["job"]["status"], final["job"]["interrupted"]), ("interrompu", True))
        self.assertIn("résultats partiels", final["job"]["summary"][0])
        self.assertEqual(self.panel.job.stats.active, 0)

        # Une fois le premier test fini, on peut en relancer un.
        status, job = self.call("/api/start", self.request(duration=0.5))
        self.assertEqual((status, job["number"]), (200, 2))
        self.assertEqual(len(self.wait_until_finished()["history"]), 2)

    def test_udp(self):
        status, _ = self.call("/api/start", self.request(proto="udp", rate=200, size=32, duration=1.5))
        self.assertEqual(status, 200)
        job = self.wait_until_finished()["job"]
        self.assertEqual(job["series"], ["Paquets envoyés/s", "Paquets reçus/s"])
        self.assertIn("===== Résumé UDP =====", job["summary"][0])
        self.assertGreater(self.panel.job.stats.sent, 100)

    def test_fermeture_du_panel_arrete_le_test(self):
        self.call("/api/start", self.request(duration=30, hold=30))
        time.sleep(0.5)
        self.panel.shutdown()
        self.assertEqual(self.panel.job.state, "done")
        self.assertTrue(self.panel.job.stats.interrupted)


class EntryPointTests(unittest.TestCase):
    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = web.main(argv)
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_ecoute_publique_refusee(self):
        code, _, err = self.run_main(["--bind", "8.8.8.8"])
        self.assertEqual(code, 2)
        self.assertIn("refusé", err)

    def test_allow_host_public_refuse(self):
        code, _, err = self.run_main(["--allow-host", "8.8.8.8"])
        self.assertEqual(code, 2)
        self.assertIn("refusé", err)

    def test_aide(self):
        code, out, _ = self.run_main(["--help"])
        self.assertEqual(code, 0)
        for word in ("--bind", "--allow-host", "tunnel SSH"):
            self.assertIn(word, out)


if __name__ == "__main__":
    unittest.main()
