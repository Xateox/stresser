"""Tests de l'application à menus (python stress.py sans option).

Pendant les tests, la sortie n'est pas un terminal : les menus passent donc en
mode « texte simple » (listes numérotées, réponses avec input()). Ce sont les
mêmes écrans et le même enchaînement qu'avec les flèches ; la navigation aux
flèches elle-même est testée dans test_ui.py.

Numéros utilisés dans les réponses simulées :
- menu principal : 1 Lancer un test, puis Inventaire, Historique, Réglages, Aide,
  Quitter. Après un premier test, « Relancer le dernier test » s'insère en 2.
- machines (inventaire livré) : 1 proxmox, 2 opnsense, 3 adguard,
  4 home-assistant, 5 truenas, 6 monitor, 7 Autre adresse IP.
- 0 revient à l'écran précédent.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from stresser import cli
from tests.test_cli import launched_config, run_cli

QUIT_BEFORE_TEST = "6"      # « Quitter » tant qu'aucun test n'a été lancé
QUIT_AFTER_TEST = "7"       # « Quitter » une fois « Relancer le dernier test » apparu
# Lancer un test > home-assistant > TCP > port 8123 > profil Modéré > Lancer > Entrée
STANDARD_RUN = ["1", "4", "1", "8123", "2", "1", ""]


class WizardTests(unittest.TestCase):
    def test_parcours_complet(self):
        code, out, _, execute = run_cli([], STANDARD_RUN + [QUIT_AFTER_TEST])
        cfg = launched_config(execute)
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual((str(cfg.ip), cfg.name, cfg.port, cfg.proto), ("192.168.35.14", "home-assistant", 8123, "tcp"))
        self.assertEqual((cfg.concurrency, cfg.duration, cfg.size, cfg.hold, cfg.ramp), (50, 10.0, 0, 0.0, False))
        for expected in ("STRESSER", "Menu principal", "Étape 1/5", "Étape 5/5", "home-assistant",
                         "50 connexions simultanées", "Résumé TCP", "Au revoir."):
            self.assertIn(expected, out)

    def test_profils(self):
        cases = {
            "1": dict(concurrency=10, duration=10.0, ramp=False),
            "3": dict(concurrency=200, duration=30.0, ramp=False),
            "4": dict(concurrency=200, duration=30.0, ramp=True),
        }
        for choice, expected in cases.items():
            with self.subTest(profil=choice):
                _, _, _, execute = run_cli([], ["1", "4", "1", "8123", choice, "1", "", QUIT_AFTER_TEST])
                cfg = launched_config(execute)
                self.assertEqual(dict(concurrency=cfg.concurrency, duration=cfg.duration, ramp=cfg.ramp), expected)

    def test_udp_soutenu(self):
        _, out, _, execute = run_cli([], ["1", "4", "2", "53", "3", "1", "", QUIT_AFTER_TEST])
        cfg = launched_config(execute)
        self.assertEqual((cfg.name, cfg.proto, cfg.port), ("home-assistant", "udp", 53))
        self.assertEqual((cfg.rate, cfg.duration, cfg.size), (5000, 30.0, 64))
        self.assertIn("5000 paquets/s de 64 octets", out)

    def test_profil_personnalise_tcp(self):
        answers = ["1", "4", "1", "8123", "5",
                   "20", "30", "512", "1,5", "o",       # durée, connexions, octets, maintien, montée
                   "1", "", QUIT_AFTER_TEST]
        _, _, _, execute = run_cli([], answers)
        cfg = launched_config(execute)
        self.assertEqual((cfg.duration, cfg.concurrency, cfg.size, cfg.hold, cfg.ramp), (20.0, 30, 512, 1.5, True))

    def test_profil_personnalise_udp_avec_les_defauts(self):
        answers = ["1", "4", "2", "9999", "4", "", "", "", "1", "", QUIT_AFTER_TEST]
        _, _, _, execute = run_cli([], answers)
        cfg = launched_config(execute)
        self.assertEqual((cfg.proto, cfg.duration, cfg.rate, cfg.size), ("udp", 10.0, 1000, 64))

    def test_saisies_invalides_reposees(self):
        answers = ["1", "99", "abc", "4",           # machine : numéro hors liste, texte, puis valide
                   "1",
                   "0", "70000", "huit", "8123",    # port : hors bornes, hors bornes, texte, puis valide
                   "5", "-3", "inf", "20",          # durée personnalisée : négative, infinie, puis valide
                   "0", "70000", "30",              # connexions : trop peu, trop, puis valide
                   "", "", "peut-être", "n",        # octets, maintien par défaut ; montée : réponse floue puis non
                   "1", "", QUIT_AFTER_TEST]
        _, _, _, execute = run_cli([], answers)
        cfg = launched_config(execute)
        self.assertEqual((cfg.port, cfg.duration, cfg.concurrency, cfg.ramp), (8123, 20.0, 30, False))
        self.assertEqual(execute.asked, len(answers))

    def test_options_de_la_ligne_de_commande_proposees_par_defaut(self):
        # Port donné avec -p : Entrée seule le garde.
        _, _, _, execute = run_cli(["-p", "8123"], ["1", "4", "1", "", "2", "1", "", QUIT_AFTER_TEST])
        self.assertEqual(launched_config(execute).port, 8123)

    def test_retour_arriere_puis_autre_machine(self):
        # À l'étape du protocole, 0 revient au choix de la machine.
        _, _, _, execute = run_cli([], ["1", "4", "0", "6", "1", "3001", "2", "1", "", QUIT_AFTER_TEST])
        cfg = launched_config(execute)
        self.assertEqual((cfg.name, cfg.port), ("monitor", 3001))

    def test_retour_depuis_le_port(self):
        # « < » à la question du port revient au protocole, où l'on choisit UDP.
        _, _, _, execute = run_cli([], ["1", "4", "1", "<", "2", "53", "2", "1", "", QUIT_AFTER_TEST])
        self.assertEqual(launched_config(execute).proto, "udp")

    def test_modifier_l_intensite_depuis_le_recapitulatif(self):
        answers = ["1", "4", "1", "8123", "3", "2", "1", "1", "", QUIT_AFTER_TEST]   # Soutenu, Modifier, Léger, Lancer
        _, _, _, execute = run_cli([], answers)
        self.assertEqual(launched_config(execute).concurrency, 10)

    def test_annuler_au_recapitulatif(self):
        code, _, _, execute = run_cli([], ["1", "4", "1", "8123", "2", "3", QUIT_BEFORE_TEST])
        self.assertEqual(code, cli.EXIT_OK)
        execute.assert_not_called()

    def test_avertissement_affiche_au_recapitulatif(self):
        answers = ["1", "4", "1", "8123", "5", "", "5000", "", "", "", "3", QUIT_BEFORE_TEST]
        _, out, _, execute = run_cli([], answers)
        self.assertIn("Concurrence élevée", out)
        execute.assert_not_called()


class GuardTests(unittest.TestCase):
    """Les garde-fous restent les mêmes dans les menus."""

    def test_adresse_tapee_a_la_main(self):
        _, out, _, execute = run_cli([], ["1", "7", "192.168.35.50", "1", "80", "2", "1", "", QUIT_AFTER_TEST])
        cfg = launched_config(execute)
        self.assertEqual((str(cfg.ip), cfg.name), ("192.168.35.50", ""))
        self.assertIn("hors inventaire", out)

    def test_adresses_refusees_puis_corrigee(self):
        answers = ["1", "7", "8.8.8.8", "10.10.10.21", "127.0.0.1", "192.168.35.255", "gitea.lan",
                   "192.168.35.50", "1", "80", "2", "1", "", QUIT_AFTER_TEST]
        _, out, _, execute = run_cli([], answers)
        self.assertEqual(str(launched_config(execute).ip), "192.168.35.50")
        self.assertEqual(out.count("Cible refusée"), 4)     # la 5e saisie n'est pas une IPv4
        self.assertIn("n'est pas une adresse IPv4 valide", out)

    def test_adresse_refusee_puis_abandon(self):
        _, out, _, execute = run_cli([], ["1", "7", "8.8.8.8", "<", "0", QUIT_BEFORE_TEST])
        self.assertIn("Cible refusée", out)
        execute.assert_not_called()

    def test_machine_hors_plage_absente_du_menu(self):
        with tempfile.TemporaryDirectory() as folder:
            inventory = Path(folder) / "inventaire.txt"
            inventory.write_text("192.168.35.14 ha\n10.10.10.21 gitea\n", encoding="utf-8")
            # 2 = « Autre adresse IP » : gitea n'est pas proposé.
            _, out, _, execute = run_cli(["--inventory", str(inventory)],
                                         ["1", "2", "<", "0", QUIT_BEFORE_TEST])
        self.assertIn("1) ha", out)
        self.assertIn("2) Autre adresse IP", out)
        self.assertNotIn("gitea", out)
        execute.assert_not_called()

    def test_allow_host_propose_dans_le_menu(self):
        answers = ["1", "7", "1", "3000", "2", "1", "", QUIT_AFTER_TEST]    # 7 = l'IP autorisée, 8 = Autre
        _, _, _, execute = run_cli(["--allow-host", "10.10.10.21"], answers)
        self.assertEqual(str(launched_config(execute).ip), "10.10.10.21")

    def test_machine_critique_exige_son_nom(self):
        # proxmox, TCP, 8006, Léger, Lancer, puis le nom.
        _, out, _, execute = run_cli([], ["1", "1", "1", "8006", "1", "1", "mauvais", "", QUIT_BEFORE_TEST])
        self.assertIn("[critique]", out)
        self.assertIn("aucun paquet n'a été envoyé", out)
        execute.assert_not_called()

        _, _, _, execute = run_cli([], ["1", "1", "1", "8006", "1", "1", "Proxmox", "", QUIT_AFTER_TEST])
        self.assertTrue(launched_config(execute).critical)

    def test_fin_de_saisie_ou_ctrl_c_quittent_sans_rien_lancer(self):
        for stop in (EOFError(), KeyboardInterrupt()):
            for answers in ([stop], ["1", stop], ["1", "4", "1", stop], ["1", "4", "1", "8123", "2", stop]):
                with self.subTest(stop=type(stop).__name__, etape=len(answers)):
                    code, out, _, execute = run_cli([], answers)
                    self.assertEqual(code, cli.EXIT_OK)
                    self.assertIn("Au revoir.", out)
                    execute.assert_not_called()


class SessionTests(unittest.TestCase):
    def test_relancer_le_dernier_test(self):
        # Après un test : 2 = Relancer > récapitulatif > 1 = Lancer > Entrée.
        _, out, _, execute = run_cli([], STANDARD_RUN + ["2", "1", "", QUIT_AFTER_TEST])
        self.assertEqual(execute.call_count, 2)
        first, second = (call.args[0] for call in execute.call_args_list)
        self.assertEqual(first, second)
        self.assertEqual(out.count("Étape 5/5"), 2)     # le récapitulatif est bien réaffiché

    def test_relancer_puis_renoncer(self):
        _, _, _, execute = run_cli([], STANDARD_RUN + ["2", "3", QUIT_AFTER_TEST])
        self.assertEqual(execute.call_count, 1)

    def test_historique(self):
        # Après un test : 4 = Historique > 1 = le test > Entrée > 0 = retour.
        _, out, _, _ = run_cli([], STANDARD_RUN + ["4", "1", "", "0", QUIT_AFTER_TEST])
        self.assertIn("Historique", out)
        self.assertEqual(out.count("Résumé TCP"), 2)    # une fois après le test, une fois dans l'historique

    def test_historique_vide(self):
        _, out, _, _ = run_cli([], ["3", "", QUIT_BEFORE_TEST])
        self.assertIn("Aucun test lancé", out)

    def test_inventaire_et_aide(self):
        _, out, _, execute = run_cli([], ["2", "1", "0", "5", "", QUIT_BEFORE_TEST])   # Inventaire > Recharger > retour, Aide
        for expected in ("Inventaire", "opnsense", "(WAN / GUI 8443)", "Fichier :", "Aide", "Garde-fous"):
            self.assertIn(expected, out)
        execute.assert_not_called()

    def test_reglages_csv_et_delai(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "essais.csv"
            # Réglages > CSV > chemin > Délai > 1.5 > retour, puis un test.
            answers = ["4", "1", str(path), "2", "1.5", "0"] + STANDARD_RUN + [QUIT_AFTER_TEST]
            _, out, _, execute = run_cli([], answers)
            self.assertTrue(path.exists())
            self.assertIn("proto", path.read_text(encoding="utf-8"))
        self.assertEqual(launched_config(execute).timeout, 1.5)
        self.assertIn("Résumé ajouté à", out)

    def test_reglage_des_symboles(self):
        # Réglages > Symboles : la bascule doit changer le libellé affiché.
        _, out, _, _ = run_cli([], ["4", "3", "3", "0", QUIT_BEFORE_TEST])
        self.assertIn("ASCII", out)
        self.assertIn("Unicode", out)

    def test_erreur_au_lancement(self):
        failure = OSError(101, "Network is unreachable")
        with mock.patch.object(cli, "make_display", side_effect=failure):
            # make_display est appelé par le vrai execute : on ne remplace donc pas celui-ci.
            import contextlib
            import io
            out = io.StringIO()
            answers = STANDARD_RUN[:-1] + ["", QUIT_BEFORE_TEST]
            with mock.patch("builtins.input", side_effect=answers), contextlib.redirect_stdout(out):
                code = cli.main([])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("Impossible de lancer le test", out.getvalue())


if __name__ == "__main__":
    unittest.main()
