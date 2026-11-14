"""Tests du chargement de l'inventaire."""

import unittest
from pathlib import Path

from stresser import guard
from stresser.inventory import find_by_ip, load_inventory, parse_inventory

SAMPLE = """\
# commentaire

192.168.35.11  proxmox                           [critique]
192.168.35.12  opnsense   (WAN / GUI 8443)       [CRITIQUE]
192.168.35.16  monitor    (uptime-kuma)
pas-une-ip     machin
192.168.35.20
192.168.35.11  doublon
"""


class ParseInventoryTests(unittest.TestCase):
    def setUp(self):
        self.machines, self.warnings = parse_inventory(SAMPLE)

    def test_lit_les_lignes_valides(self):
        self.assertEqual([m.name for m in self.machines], ["proxmox", "opnsense", "monitor"])

    def test_separe_nom_et_description(self):
        opnsense = self.machines[1]
        self.assertEqual(opnsense.description, "(WAN / GUI 8443)")
        self.assertEqual(opnsense.label, "opnsense (WAN / GUI 8443)")
        self.assertEqual(self.machines[0].label, "proxmox")

    def test_marqueur_critique(self):
        self.assertEqual([m.critical for m in self.machines], [True, True, False])

    def test_signale_les_lignes_invalides_sans_planter(self):
        self.assertEqual(len(self.warnings), 3)   # IP invalide, nom manquant, doublon

    def test_recherche_par_ip(self):
        found = find_by_ip(self.machines, self.machines[2].ip)
        self.assertEqual(found.name, "monitor")
        self.assertIsNone(find_by_ip(self.machines, guard.parse_ipv4("192.168.35.99")))


class InventoryFileTests(unittest.TestCase):
    def test_fichier_absent(self):
        machines, warnings = load_inventory(Path("fichier-qui-n-existe-pas.txt"))
        self.assertEqual(machines, [])
        self.assertEqual(len(warnings), 1)

    def test_inventaire_livre_est_valide_et_ciblable(self):
        path = Path(__file__).resolve().parent.parent / "inventory.txt"
        machines, warnings = load_inventory(path)
        self.assertEqual(warnings, [])
        self.assertGreaterEqual(len(machines), 6)
        for machine in machines:
            self.assertTrue(guard.is_targetable(machine.ip), machine)


if __name__ == "__main__":
    unittest.main()
