"""Tests des garde-fous : quelles cibles passent, lesquelles sont refusées."""

import ipaddress
import unittest

from stresser import guard


class CheckTargetTests(unittest.TestCase):
    def test_accepte_une_ip_de_la_plage(self):
        self.assertEqual(guard.check_target("192.168.35.14"), ipaddress.IPv4Address("192.168.35.14"))

    def test_tolere_les_espaces(self):
        self.assertEqual(str(guard.check_target("  192.168.35.14 ")), "192.168.35.14")

    def test_refuse_une_ip_publique(self):
        for target in ("8.8.8.8", "1.1.1.1", "203.0.113.5"):
            with self.subTest(target=target), self.assertRaises(guard.TargetError):
                guard.check_target(target)

    def test_refuse_une_ip_privee_hors_plage(self):
        # Les anciens VLAN (services, école) et un autre /24 en 192.168 sont hors plage.
        for target in ("10.10.10.21", "10.30.30.5", "192.168.1.1", "172.16.0.1"):
            with self.subTest(target=target), self.assertRaises(guard.TargetError):
                guard.check_target(target)

    def test_refuse_reseau_et_broadcast(self):
        for target in ("192.168.35.0", "192.168.35.255"):
            with self.subTest(target=target), self.assertRaises(guard.TargetError):
                guard.check_target(target)

    def test_refuse_loopback_multicast_et_adresses_speciales(self):
        for target in ("127.0.0.1", "169.254.1.1", "224.0.0.1", "255.255.255.255", "0.0.0.0"):
            with self.subTest(target=target), self.assertRaises(guard.TargetError):
                guard.check_target(target)

    def test_refuse_ce_qui_n_est_pas_une_ipv4(self):
        for target in ("gitea.lan", "localhost", "", "192.168.35", "192.168.35.14/24",
                       "192.168.35.014", "::1", "192.168.35.14:80"):
            with self.subTest(target=target), self.assertRaises(guard.TargetError):
                guard.check_target(target)


class AllowHostTests(unittest.TestCase):
    def test_autorise_une_ip_privee_hors_plage(self):
        allowed = guard.parse_allow_hosts(["10.10.10.21"])
        self.assertEqual(str(guard.check_target("10.10.10.21", allowed)), "10.10.10.21")

    def test_n_autorise_que_l_ip_citee(self):
        allowed = guard.parse_allow_hosts(["10.10.10.21"])
        with self.assertRaises(guard.TargetError):
            guard.check_target("10.10.10.22", allowed)

    def test_refuse_une_ip_publique(self):
        with self.assertRaises(guard.TargetError):
            guard.parse_allow_hosts(["8.8.8.8"])

    def test_refuse_loopback_et_link_local(self):
        # ip.is_private de Python les accepterait : on vérifie qu'on est plus strict.
        for value in ("127.0.0.1", "169.254.10.10", "100.64.0.1"):
            with self.subTest(value=value), self.assertRaises(guard.TargetError):
                guard.parse_allow_hosts([value])

    def test_refuse_reseau_et_broadcast(self):
        for value in ("10.10.10.0", "10.10.10.255"):
            with self.subTest(value=value), self.assertRaises(guard.TargetError):
                guard.parse_allow_hosts([value])


class IntensityTests(unittest.TestCase):
    def test_pas_d_avertissement_aux_valeurs_par_defaut(self):
        self.assertEqual(guard.intensity_warnings("tcp", guard.DEFAULT_CONCURRENCY, 0, 0), [])
        self.assertEqual(
            guard.intensity_warnings("udp", 0, guard.DEFAULT_RATE, guard.DEFAULT_UDP_SIZE), [])

    def test_avertit_au_dela_des_bornes(self):
        self.assertEqual(len(guard.intensity_warnings("tcp", guard.WARN_CONCURRENCY + 1, 0, 0)), 1)
        self.assertEqual(len(guard.intensity_warnings("udp", 0, guard.WARN_RATE + 1, 64)), 1)
        self.assertEqual(len(guard.intensity_warnings("udp", 0, 1000, 2000)), 1)

    def test_taille_maximale(self):
        guard.check_size("udp", guard.MAX_UDP_SIZE)
        with self.assertRaises(ValueError):
            guard.check_size("udp", guard.MAX_UDP_SIZE + 1)
        with self.assertRaises(ValueError):
            guard.check_size("tcp", guard.MAX_TCP_SIZE + 1)


if __name__ == "__main__":
    unittest.main()
