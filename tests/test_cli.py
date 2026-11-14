"""Tests de la ligne de commande et du mode interactif.

Aucun trafic réseau ici : la fonction cli.execute (celle qui lance les moteurs)
est remplacée par un faux, ce qui permet de vérifier AVEC QUELS PARAMÈTRES le test
aurait été lancé, ou qu'il ne l'a pas été du tout.
"""

import contextlib
import csv
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from stresser import cli
from stresser.stats import TcpStats


def run_cli(argv, answers=(), interrupted=False):
    """Lance cli.main avec des réponses clavier simulées.

    Retourne (code de sortie, sortie standard, sortie d'erreur, faux execute).
    """
    stats = TcpStats()
    stats.started, stats.ended, stats.interrupted = 100.0, 101.0, interrupted
    out, err = io.StringIO(), io.StringIO()
    with (
        mock.patch("builtins.input", side_effect=list(answers)) as fake_input,
        mock.patch.object(cli, "execute", return_value=stats) as fake_execute,
        contextlib.redirect_stdout(out),
        contextlib.redirect_stderr(err),
    ):
        try:
            code = cli.main(list(argv))
        except SystemExit as exc:   # argparse quitte ainsi sur --help ou une option invalide
            code = exc.code
    fake_execute.asked = fake_input.call_count
    return code, out.getvalue(), err.getvalue(), fake_execute


def launched_config(fake_execute):
    """Le RunConfig avec lequel le test aurait été lancé."""
    fake_execute.assert_called_once()
    return fake_execute.call_args.args[0]


class NonInteractiveTests(unittest.TestCase):
    def test_exemple_du_cahier_des_charges(self):
        code, out, _, execute = run_cli(
            ["-t", "192.168.35.14", "-p", "8123", "--proto", "tcp",
             "--duration", "30", "--concurrency", "100", "--yes"])
        cfg = launched_config(execute)
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual((str(cfg.ip), cfg.port, cfg.proto), ("192.168.35.14", 8123, "tcp"))
        self.assertEqual((cfg.duration, cfg.concurrency), (30.0, 100))
        self.assertEqual(cfg.name, "home-assistant")
        self.assertIn("home-assistant", out)        # nom de la machine en confirmation
        self.assertEqual(execute.asked, 0)          # --yes : aucune question posée

    def test_valeurs_par_defaut_tcp(self):
        _, _, _, execute = run_cli(["-t", "192.168.35.14", "-p", "80", "--yes"])
        cfg = launched_config(execute)
        self.assertEqual((cfg.proto, cfg.concurrency, cfg.duration, cfg.size), ("tcp", 50, 10.0, 0))
        self.assertEqual((cfg.ramp, cfg.hold, cfg.timeout), (False, 0.0, 3.0))

    def test_valeurs_par_defaut_udp(self):
        _, _, _, execute = run_cli(["-t", "192.168.35.14", "-p", "53", "--proto", "udp", "--yes"])
        cfg = launched_config(execute)
        self.assertEqual((cfg.proto, cfg.rate, cfg.size), ("udp", 1000, 64))

    def test_options_tcp(self):
        _, _, _, execute = run_cli(
            ["-t", "192.168.35.14", "-p", "80", "--size", "512", "--hold", "1.5", "--ramp",
             "--timeout", "2", "--yes"])
        cfg = launched_config(execute)
        self.assertEqual((cfg.size, cfg.hold, cfg.ramp, cfg.timeout), (512, 1.5, True, 2.0))

    def test_ip_dans_la_plage_mais_hors_inventaire(self):
        _, out, _, execute = run_cli(["-t", "192.168.35.50", "-p", "80", "--yes"])
        self.assertEqual(launched_config(execute).name, "")
        self.assertIn("hors inventaire", out)

    def test_port_obligatoire(self):
        code, _, err, execute = run_cli(["-t", "192.168.35.14", "--yes"])
        self.assertEqual(code, cli.EXIT_REFUSED)
        self.assertIn("-p PORT", err)
        execute.assert_not_called()

    def test_yes_exige_une_cible(self):
        code, _, _, execute = run_cli(["--yes"])
        self.assertEqual(code, cli.EXIT_REFUSED)
        execute.assert_not_called()

    def test_valeurs_invalides_rejetees_par_argparse(self):
        for argv in (["-p", "70000"], ["-p", "0"], ["--duration", "0"], ["--concurrency", "0"],
                     ["--rate", "-5"], ["--proto", "icmp"], ["--size", "abc"],
                     ["--concurrency", "70000"], ["--rate", "2000000"]):   # au-delà des maxima
            with self.subTest(argv=argv):
                code, _, _, execute = run_cli(["-t", "192.168.35.14", *argv])
                self.assertEqual(code, 2)
                execute.assert_not_called()

    def test_taille_udp_trop_grande(self):
        code, _, _, execute = run_cli(
            ["-t", "192.168.35.14", "-p", "53", "--proto", "udp", "--size", "70000", "--yes"])
        self.assertEqual(code, cli.EXIT_REFUSED)
        execute.assert_not_called()

    def test_avertissement_au_dela_de_la_borne(self):
        _, out, _, execute = run_cli(
            ["-t", "192.168.35.14", "-p", "80", "--concurrency", "5000", "--yes"])
        self.assertIn("ATTENTION", out)
        execute.assert_called_once()

    def test_option_sans_effet_signalee(self):
        _, out, _, _ = run_cli(
            ["-t", "192.168.35.14", "-p", "53", "--proto", "udp", "--concurrency", "10", "--yes"])
        self.assertIn("--concurrency est sans effet en UDP", out)

    def test_code_de_sortie_si_interrompu(self):
        code, _, _, _ = run_cli(["-t", "192.168.35.14", "-p", "80", "--yes"], interrupted=True)
        self.assertEqual(code, cli.EXIT_INTERRUPTED)

    def test_aide_et_liste(self):
        code, out, _, _ = run_cli(["--help"])
        self.assertEqual(code, 0)
        for word in ("--concurrency", "--rate", "--allow-host", "exemples", "192.168.35.0/24"):
            self.assertIn(word, out)
        code, out, _, execute = run_cli(["--list"])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("proxmox", out)
        execute.assert_not_called()

    def test_export_csv(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "runs.csv"
            for _ in range(2):
                run_cli(["-t", "192.168.35.14", "-p", "80", "--yes", "--csv", str(path)])
            with path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["cible"], "192.168.35.14")
        self.assertEqual(rows[0]["nom"], "home-assistant")


class GuardTests(unittest.TestCase):
    """Une cible refusée ne doit provoquer ni question, ni lancement."""

    def assert_refused(self, argv, answers=()):
        code, _, err, execute = run_cli(argv, answers)
        self.assertEqual(code, cli.EXIT_REFUSED)
        self.assertIn("refusé", err)
        execute.assert_not_called()

    def test_ip_publique(self):
        self.assert_refused(["-t", "8.8.8.8", "-p", "53", "--yes"])

    def test_ip_privee_hors_plage(self):
        self.assert_refused(["-t", "10.10.10.21", "-p", "3000", "--yes"])
        self.assert_refused(["-t", "10.30.30.5", "-p", "80", "--yes"])

    def test_loopback_et_broadcast(self):
        self.assert_refused(["-t", "127.0.0.1", "-p", "80", "--yes"])
        self.assert_refused(["-t", "192.168.35.255", "-p", "80", "--proto", "udp", "--yes"])

    def test_allow_host_autorise_une_ip_privee(self):
        code, _, _, execute = run_cli(
            ["-t", "10.10.10.21", "-p", "3000", "--allow-host", "10.10.10.21", "--yes"])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(str(launched_config(execute).ip), "10.10.10.21")

    def test_allow_host_refuse_une_ip_publique(self):
        self.assert_refused(["-t", "8.8.8.8", "-p", "53", "--allow-host", "8.8.8.8", "--yes"])

    def test_ip_publique_tapee_dans_le_menu(self):
        self.assert_refused([], answers=["8.8.8.8"])


class ConfirmationTests(unittest.TestCase):
    ARGV = ["-t", "192.168.35.14", "-p", "8123"]

    def test_recapitulatif_avant_confirmation(self):
        _, out, _, _ = run_cli(self.ARGV + ["--duration", "30"], answers=["n"])
        for expected in ("192.168.35.14", "home-assistant", "8123", "TCP",
                         "50 connexions simultanées", "30 s"):
            self.assertIn(expected, out)

    def test_refus(self):
        for answer in ("n", "", "non", "peut-être"):
            with self.subTest(answer=answer):
                code, out, _, execute = run_cli(self.ARGV, answers=[answer])
                self.assertEqual(code, cli.EXIT_CANCELLED)
                self.assertIn("aucun paquet", out)
                execute.assert_not_called()

    def test_accord(self):
        for answer in ("o", "O", "oui", "y"):
            with self.subTest(answer=answer):
                code, _, _, execute = run_cli(self.ARGV, answers=[answer])
                self.assertEqual(code, cli.EXIT_OK)
                execute.assert_called_once()

    def test_fin_de_saisie_vaut_refus(self):
        code, _, _, execute = run_cli(self.ARGV, answers=[EOFError()])
        self.assertEqual(code, cli.EXIT_CANCELLED)
        execute.assert_not_called()

    def test_machine_critique_exige_son_nom(self):
        argv = ["-t", "192.168.35.11", "-p", "8006"]
        code, out, _, execute = run_cli(argv, answers=["o"])
        self.assertEqual(code, cli.EXIT_CANCELLED)
        self.assertIn("[critique]", out)
        execute.assert_not_called()

        code, _, _, execute = run_cli(argv, answers=["Proxmox"])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertTrue(launched_config(execute).critical)


class InteractiveTests(unittest.TestCase):
    def test_choix_par_numero(self):
        # 4e machine de l'inventaire = home-assistant ; protocole laissé par défaut.
        code, out, _, execute = run_cli([], answers=["4", "8123", "", "5", "20", "o"])
        cfg = launched_config(execute)
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual((str(cfg.ip), cfg.port, cfg.proto), ("192.168.35.14", 8123, "tcp"))
        self.assertEqual((cfg.duration, cfg.concurrency), (5.0, 20))
        self.assertIn("Machines de l'inventaire", out)
        self.assertIn("Cible : 192.168.35.14 (home-assistant)", out)

    def test_ip_tapee_a_la_main_en_udp_avec_les_defauts(self):
        _, out, _, execute = run_cli([], answers=["192.168.35.50", "9999", "udp", "", "", "o"])
        cfg = launched_config(execute)
        self.assertEqual((str(cfg.ip), cfg.port, cfg.proto), ("192.168.35.50", 9999, "udp"))
        self.assertEqual((cfg.duration, cfg.rate, cfg.size), (10.0, 1000, 64))
        self.assertIn("hors inventaire", out)

    def test_saisies_invalides_reposees(self):
        answers = ["99", "abc", "1",          # cible : hors liste, illisible, puis proxmox
                   "0", "huit", "8006",       # port : hors bornes, illisible, puis valide
                   "icmp", "tcp",             # protocole
                   "-3", "5",                 # durée
                   "", "proxmox"]             # concurrence par défaut, confirmation par le nom
        code, _, _, execute = run_cli([], answers=answers)
        cfg = launched_config(execute)
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual((cfg.name, cfg.port, cfg.duration, cfg.concurrency), ("proxmox", 8006, 5.0, 50))
        self.assertEqual(execute.asked, len(answers))

    def test_options_deja_donnees_non_redemandees(self):
        _, _, _, execute = run_cli(
            ["-p", "8123", "--proto", "tcp", "--duration", "5", "--concurrency", "10"],
            answers=["4", "o"])
        cfg = launched_config(execute)
        self.assertEqual((cfg.port, cfg.duration, cfg.concurrency), (8123, 5.0, 10))
        self.assertEqual(execute.asked, 2)

    def test_abandon_dans_le_menu(self):
        for stop in (EOFError(), KeyboardInterrupt()):
            with self.subTest(stop=type(stop).__name__):
                code, _, _, execute = run_cli([], answers=["4", stop])
                self.assertEqual(code, cli.EXIT_CANCELLED)
                execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
