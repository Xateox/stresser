"""Tests de l'interface en terminal : mise en forme, menus à flèches, tableau de bord, clavier.

Les menus sont pilotés par une suite de touches simulées, et ce qui est écrit à
l'écran est capturé pour être vérifié.
"""

import contextlib
import io
import ipaddress
import os
import sys
import unittest
from unittest import mock

from stresser import term
from stresser.config import RunConfig
from stresser.display import DashboardDisplay, PlainDisplay, make_display
from stresser.ui import UI, Option

PLAIN = term.Theme(color=False, unicode=True)
COLOR = term.Theme(color=True, unicode=True)
ASCII = term.Theme(color=False, unicode=False)


class FormattingTests(unittest.TestCase):
    def test_couleur(self):
        self.assertEqual(PLAIN.paint("texte", "bold", "accent"), "texte")
        painted = COLOR.paint("texte", "bold")
        self.assertTrue(painted.startswith("\x1b[1m") and painted.endswith("\x1b[0m"))
        self.assertEqual(term.visible_len(painted), 5)
        self.assertEqual(term.strip_ansi(painted), "texte")

    def test_pad_et_truncate(self):
        self.assertEqual(term.pad("ab", 5), "ab   ")
        self.assertEqual(term.visible_len(term.pad(COLOR.paint("ab", "ok"), 5)), 5)
        self.assertEqual(term.truncate("abcdefgh", 5), "abcd…")
        self.assertEqual(term.truncate("abc", 5), "abc")

    def test_wrap(self):
        lines = term.wrap("un deux trois quatre cinq", 10)
        self.assertEqual(lines, ["un deux", "trois", "quatre", "cinq"])
        self.assertEqual(term.wrap("", 10), [""])

    def test_cadre_de_largeur_constante(self):
        for theme in (PLAIN, COLOR, ASCII):
            lines = term.box(["court", theme.paint("ligne en couleur", "accent"), ""], theme, 40,
                             title="Titre", footer="Pied")
            self.assertEqual({term.visible_len(line) for line in lines}, {40})
            self.assertIn("Titre", lines[0])
            self.assertIn("Pied", lines[-1])

    def test_barre_d_avancement(self):
        self.assertEqual(term.progress_bar(0.5, 10, PLAIN), "█████░░░░░")
        self.assertEqual(term.progress_bar(0, 4, ASCII), "....")
        self.assertEqual(term.progress_bar(7, 4, ASCII), "####")      # borné à 100 %

    def test_mini_graphique(self):
        self.assertEqual(term.sparkline([0, 50, 100], 10, PLAIN), "▁▅█")
        self.assertEqual(term.sparkline([0, 0], 10, PLAIN), "▁▁")
        self.assertEqual(len(term.sparkline(list(range(100)), 20, PLAIN)), 20)   # garde les 20 dernières
        self.assertEqual(term.sparkline([], 10, PLAIN), "")

    def test_theme_ascii_sans_caractere_special(self):
        self.assertTrue(all(glyph.isascii() for glyph in ASCII.glyphs.values()))

    def test_sans_terminal_pas_d_ansi(self):
        with contextlib.redirect_stdout(io.StringIO()):
            theme, ansi = term.detect()
        self.assertFalse(ansi)
        self.assertFalse(theme.color)
        self.assertEqual(term.detect(plain=True)[1], False)


def make_ui(keys, theme=PLAIN):
    keys = iter(keys)
    return UI(theme, interactive=True, read_key=lambda: next(keys))


def run_select(keys, options, **kwargs):
    """Pilote un menu avec des touches simulées. Retourne (valeur choisie, écran capturé)."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        value = make_ui(keys, kwargs.pop("theme", PLAIN)).select("Titre", options, **kwargs)
    return value, out.getvalue()


LETTERS = [Option("Alpha", "a", detail="premier", hint="aide alpha"),
           Option("Bravo", "b", tag="critique"),
           Option("Charlie", "c", hint="aide charlie")]


class KeyMenuTests(unittest.TestCase):
    def test_fleches_puis_entree(self):
        self.assertEqual(run_select([term.ENTER], LETTERS)[0], "a")
        self.assertEqual(run_select([term.DOWN, term.ENTER], LETTERS)[0], "b")
        self.assertEqual(run_select([term.DOWN, term.DOWN, term.UP, term.ENTER], LETTERS)[0], "b")

    def test_le_menu_boucle(self):
        self.assertEqual(run_select([term.UP, term.ENTER], LETTERS)[0], "c")
        self.assertEqual(run_select([term.DOWN] * 3 + [term.ENTER], LETTERS)[0], "a")

    def test_debut_fin_et_position_de_depart(self):
        self.assertEqual(run_select([term.END, term.ENTER], LETTERS)[0], "c")
        self.assertEqual(run_select([term.END, term.HOME, term.ENTER], LETTERS)[0], "a")
        self.assertEqual(run_select([term.ENTER], LETTERS, start=2)[0], "c")

    def test_raccourci_par_numero(self):
        self.assertEqual(run_select(["3"], LETTERS)[0], "c")
        self.assertEqual(run_select(["9", "2"], LETTERS)[0], "b")      # 9 : hors liste, ignoré

    def test_echap_revient_en_arriere(self):
        for key in (term.ESCAPE, term.LEFT, "q"):
            self.assertIsNone(run_select([key], LETTERS)[0])
        # Au menu principal (allow_back=False), Échap est ignoré.
        self.assertEqual(run_select([term.ESCAPE, term.DOWN, term.ENTER], LETTERS, allow_back=False)[0], "b")

    def test_touches_inconnues_ignorees(self):
        self.assertEqual(run_select(["", "x", "é", term.DOWN, term.ENTER], LETTERS)[0], "b")

    def test_contenu_affiche(self):
        _, screen = run_select([term.DOWN, term.DOWN, term.ENTER], LETTERS)
        for expected in ("Titre", "Alpha", "premier", "[critique]", "aide alpha", "aide charlie",
                         "Entrée valider", "Échap retour"):
            self.assertIn(expected, screen)
        self.assertIn(term.HIDE_CURSOR, screen)
        self.assertTrue(screen.endswith(term.SHOW_CURSOR))      # le curseur est toujours rendu
        self.assertIn("\x1b[", screen.split(term.HIDE_CURSOR, 1)[1])   # redessiné sur place

    def test_curseur_rendu_meme_si_interrompu(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(KeyboardInterrupt):
            UI(PLAIN, True, read_key=mock.Mock(side_effect=KeyboardInterrupt)).select("Titre", LETTERS)
        self.assertTrue(out.getvalue().endswith(term.SHOW_CURSOR))

    def test_menu_long_defile(self):
        many = [Option(f"Machine {number}", number) for number in range(1, 41)]
        with mock.patch.object(term, "size", return_value=(78, 14)):
            value, screen = run_select([term.END, term.ENTER], many)
            self.assertEqual(value, 40)
            self.assertIn("de plus", screen)
            first_draw = screen.split("\x1b[", 3)[2]
            self.assertNotIn("Machine 40", first_draw)      # hors écran au départ
            value, _ = run_select([term.DOWN] * 12 + [term.ENTER], many)
            self.assertEqual(value, 13)

    def test_theme_ascii(self):
        _, screen = run_select([term.ENTER], LETTERS, theme=ASCII)
        visible = term.strip_ansi(screen)
        self.assertIn("> 1  Alpha", visible)
        for glyph in "›↑↓·…":
            self.assertNotIn(glyph, visible)


class QuestionTests(unittest.TestCase):
    def ask(self, answers, **kwargs):
        ui = UI(PLAIN, interactive=False)
        out = io.StringIO()
        with mock.patch("builtins.input", side_effect=answers), contextlib.redirect_stdout(out):
            return ui.ask("Port", **kwargs), out.getvalue()

    def test_valeur_et_defaut(self):
        self.assertEqual(self.ask(["80"], parse=int)[0], 80)
        self.assertEqual(self.ask([""], parse=int, default=8123)[0], 8123)

    def test_retour(self):
        self.assertIsNone(self.ask([""], parse=int)[0])            # vide sans défaut
        self.assertIsNone(self.ask(["<"], parse=int, default=80)[0])

    def test_reponse_refusee_puis_reposee(self):
        value, out = self.ask(["abc", "80"], parse=int)
        self.assertEqual(value, 80)
        self.assertIn("invalid literal", out)


def sample_cfg(**overrides):
    values = dict(ip=ipaddress.IPv4Address("192.168.35.14"), port=8123, proto="tcp", duration=30,
                  name="home-assistant", label="home-assistant")
    values.update(overrides)
    return RunConfig(**values)


TCP_FIELDS = [("temps", "12/30 s"), ("ouvertes", "50"), ("tentées", "3811"), ("réussies", "3798"),
              ("échouées", "13"), ("conn/s", "318"), ("p50", "3.2 ms"), ("p95", "11.7 ms"),
              ("envoyé", "0 octets"), ("reçu", "0 octets")]


class DashboardTests(unittest.TestCase):
    def test_cycle_complet(self):
        stream = io.StringIO()
        board = DashboardDisplay(sample_cfg(), PLAIN, stream=stream)
        board.start("titre")
        first = stream.getvalue()
        self.assertTrue(first.startswith(term.HIDE_CURSOR))
        self.assertIn("première mesure", first)
        self.assertIn("192.168.35.14:8123", first)
        self.assertIn("Ctrl+C pour arrêter", first)

        board.update(TCP_FIELDS)
        board.update(TCP_FIELDS)
        text = stream.getvalue()
        for expected in ("Tentées", "3811", "Latence p95", "11.7 ms", "Connexions/s", "max 318", "0 s / 30 s"):
            self.assertIn(expected, text)
        self.assertNotIn("temps", text)                         # remplacé par la barre d'avancement
        self.assertIn(term.cursor_up(len(board.frame())), text)     # redessiné sur place

        board.stop()
        self.assertTrue(stream.getvalue().endswith(term.CLEAR_BELOW + term.SHOW_CURSOR))

    def test_lignes_de_largeur_constante(self):
        for theme in (PLAIN, COLOR, ASCII):
            board = DashboardDisplay(sample_cfg(), theme, stream=io.StringIO())
            board.start("titre")
            board.update(TCP_FIELDS)
            widths = {term.visible_len(line) for line in board.frame()}
            self.assertEqual(len(widths), 1, widths)
            self.assertLessEqual(widths.pop(), 72)

    def test_udp(self):
        board = DashboardDisplay(sample_cfg(proto="udp"), PLAIN, stream=io.StringIO())
        board.start("titre")
        board.update([("temps", "1/30 s"), ("envoyés", "1000"), ("paquets/s", "998"), ("Mbit/s", "0.51"),
                      ("reçus", "0"), ("port injoignable", "0")])
        text = "\n".join(board.frame())
        for expected in ("Test UDP", "Paquets envoyés", "Paquets/s", "max 998"):
            self.assertIn(expected, text)

    def test_choix_de_l_affichage(self):
        self.assertIsInstance(make_display(sample_cfg(), PLAIN, ansi=False), PlainDisplay)
        self.assertIsInstance(make_display(sample_cfg(), PLAIN, ansi=True), DashboardDisplay)
        with mock.patch.object(term, "size", return_value=(45, 24)):    # terminal trop étroit
            self.assertIsInstance(make_display(sample_cfg(), PLAIN, ansi=True), PlainDisplay)


@unittest.skipUnless(sys.platform == "win32", "lecture des touches propre à Windows")
class WindowsKeyTests(unittest.TestCase):
    def read(self, *chars):
        with mock.patch("msvcrt.getwch", side_effect=list(chars)):
            return term.read_key()

    def test_touches(self):
        self.assertEqual(self.read("\xe0", "H"), term.UP)
        self.assertEqual(self.read("\xe0", "P"), term.DOWN)
        self.assertEqual(self.read("\x00", "K"), term.LEFT)     # pavé numérique : préfixe \x00
        self.assertEqual(self.read("\xe0", "M"), term.RIGHT)
        self.assertEqual(self.read("\xe0", "G"), term.HOME)
        self.assertEqual(self.read("\xe0", "O"), term.END)
        self.assertEqual(self.read("\xe0", "S"), "")            # Suppr : touche spéciale non gérée
        self.assertEqual(self.read("\r"), term.ENTER)
        self.assertEqual(self.read("\x1b"), term.ESCAPE)
        self.assertEqual(self.read("a"), "a")
        self.assertEqual(self.read("é"), "é")

    def test_ctrl_c(self):
        with self.assertRaises(KeyboardInterrupt):
            self.read("\x03")


@unittest.skipIf(sys.platform == "win32", "lecture des touches propre à Unix")
class UnixKeyTests(unittest.TestCase):
    """Lit de vraies séquences à travers un pseudo-terminal, comme un vrai clavier."""

    def setUp(self):
        self.master, self.slave = os.openpty()
        self.addCleanup(os.close, self.master)
        self.addCleanup(os.close, self.slave)
        patch = mock.patch.object(sys, "stdin", mock.Mock(fileno=lambda: self.slave))
        patch.start()
        self.addCleanup(patch.stop)

    def read(self, data: bytes) -> str:
        with term.key_mode():
            os.write(self.master, data)
            return term.read_key()

    def test_frappes_rapides_non_perdues(self):
        # Trois touches arrivent d'un coup, la première avant même le passage en mode
        # « touche par touche » : aucune ne doit être perdue.
        os.write(self.master, b"\x1b[B")
        with term.key_mode():
            os.write(self.master, b"\x1b[B\r")
            self.assertEqual([term.read_key() for _ in range(3)], [term.DOWN, term.DOWN, term.ENTER])

    def test_touches(self):
        self.assertEqual(self.read(b"\x1b[A"), term.UP)
        self.assertEqual(self.read(b"\x1b[B"), term.DOWN)
        self.assertEqual(self.read(b"\x1bOA"), term.UP)         # variante de certains terminaux
        self.assertEqual(self.read(b"\x1b[H"), term.HOME)
        self.assertEqual(self.read(b"\x1b[F"), term.END)
        self.assertEqual(self.read(b"\x1b"), term.ESCAPE)       # Échap seul : rien ne suit
        self.assertEqual(self.read(b"\r"), term.ENTER)
        self.assertEqual(self.read(b"\x7f"), term.BACKSPACE)
        self.assertEqual(self.read(b"a"), "a")
        self.assertEqual(self.read("é".encode()), "é")

    def test_terminal_remis_en_etat(self):
        import termios
        before = termios.tcgetattr(self.slave)
        self.read(b"x")
        self.assertEqual(termios.tcgetattr(self.slave), before)


if __name__ == "__main__":
    unittest.main()
