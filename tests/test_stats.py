"""Tests des calculs (percentiles, débits) et de l'export CSV."""

import csv
import ipaddress
import tempfile
import unittest
from pathlib import Path

from stresser.config import RunConfig
from stresser.stats import CSV_FIELDS, TcpStats, UdpStats, human_bytes, mbit_per_s, percentile, write_csv


def make_cfg(proto="tcp"):
    return RunConfig(ip=ipaddress.IPv4Address("192.168.35.14"), port=8123, proto=proto,
                     duration=10, name="home-assistant", label="home-assistant")


class HelperTests(unittest.TestCase):
    def test_percentile(self):
        values = [float(n) for n in range(1, 101)]   # 1 à 100
        self.assertAlmostEqual(percentile(values, 50), 50.5)
        self.assertAlmostEqual(percentile(values, 95), 95.05)
        self.assertEqual(percentile(values, 0), 1.0)
        self.assertEqual(percentile(values, 100), 100.0)

    def test_percentile_cas_limites(self):
        self.assertEqual(percentile([], 95), 0.0)
        self.assertEqual(percentile([7.0], 95), 7.0)

    def test_unites(self):
        self.assertEqual(human_bytes(512), "512 octets")
        self.assertEqual(human_bytes(2048), "2.0 Kio")
        self.assertEqual(human_bytes(5 * 1024 * 1024), "5.0 Mio")
        self.assertAlmostEqual(mbit_per_s(1_250_000, 10), 1.0)
        self.assertEqual(mbit_per_s(1000, 0), 0.0)


class SummaryTests(unittest.TestCase):
    def test_resume_tcp(self):
        stats = TcpStats()
        stats.started, stats.ended = 100.0, 110.0
        stats.attempted, stats.succeeded, stats.failed = 1000, 990, 10
        stats.connect_errors["refusée"] = 10
        stats.latencies_ms = [1.0, 2.0, 3.0, 4.0]
        text = "\n".join(stats.summary_lines(make_cfg()))
        self.assertIn("192.168.35.14:8123 (home-assistant)", text)
        self.assertIn("990 (99.0 %)", text)
        self.assertIn("Connexions par seconde  : 99.0", text)
        self.assertIn("refusée=10", text)
        self.assertIn("p95", text)
        self.assertNotIn("interrompu", text)

    def test_resume_tcp_interrompu_et_vide(self):
        stats = TcpStats()
        stats.started, stats.ended, stats.interrupted = 100.0, 100.5, True
        text = "\n".join(stats.summary_lines(make_cfg()))
        self.assertIn("résultats partiels", text)

    def test_resume_udp_sans_reponse(self):
        stats = UdpStats()
        stats.started, stats.ended = 100.0, 110.0
        stats.sent, stats.bytes_sent = 10_000, 640_000
        cfg = make_cfg("udp")
        cfg.size = 64
        text = "\n".join(stats.summary_lines(cfg))
        self.assertIn("1000.0 paquets/s", text)
        self.assertIn("ne garantit ni la livraison ni une réponse", text)


class CsvTests(unittest.TestCase):
    def test_en_tete_une_seule_fois_puis_ajout(self):
        tcp = TcpStats()
        tcp.started, tcp.ended = 100.0, 110.0
        tcp.attempted = tcp.succeeded = 500
        tcp.latencies_ms = [1.0, 2.0]
        udp = UdpStats()
        udp.started, udp.ended = 100.0, 110.0
        udp.sent = 10_000

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "tests.csv"
            write_csv(path, make_cfg("tcp"), tcp)
            write_csv(path, make_cfg("udp"), udp)
            with path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))

        self.assertEqual(list(rows[0].keys()), CSV_FIELDS)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["proto"], "tcp")
        self.assertEqual(rows[0]["conn_reussies"], "500")
        self.assertEqual(rows[0]["conn_par_s"], "50.0")
        self.assertEqual(rows[0]["lat_p50_ms"], "1.500")
        self.assertEqual(rows[1]["proto"], "udp")
        self.assertEqual(rows[1]["pps_reel"], "1000.0")
        self.assertEqual(rows[1]["conn_tentees"], "")   # sans objet en UDP


if __name__ == "__main__":
    unittest.main()
