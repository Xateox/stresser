"""Compteurs, calcul des métriques, résumé final et export CSV.

Les moteurs (tcp.py, udp.py) ne font qu'incrémenter les compteurs d'un objet
TcpStats ou UdpStats. Tout ce qui est calcul (percentiles, débits) et mise en
forme se trouve ici, pour que les moteurs restent courts.
"""

from __future__ import annotations

import csv
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from .config import RunConfig

# Colonnes du CSV. Les mêmes pour TCP et UDP (les cases sans objet restent vides),
# pour pouvoir empiler tous les tests dans un seul fichier et les comparer.
CSV_FIELDS = [
    "date", "cible", "nom", "port", "proto",
    "duree_prevue_s", "duree_reelle_s", "interrompu",
    "concurrence", "debit_cible_pps", "taille_octets", "ramp", "hold_s",
    "conn_tentees", "conn_reussies", "conn_echouees", "conn_par_s",
    "lat_min_ms", "lat_moy_ms", "lat_p50_ms", "lat_p95_ms", "lat_p99_ms", "lat_max_ms",
    "octets_envoyes", "octets_recus", "mbit_s_envoi",
    "paquets_envoyes", "pps_reel", "paquets_recus", "erreurs",
]


# --- Fonctions utilitaires ----------------------------------------------------

def percentile(sorted_values: list[float], pct: float) -> float:
    """Percentile d'une liste DÉJÀ triée, avec interpolation linéaire.

    Le p95 est la valeur sous laquelle se trouvent 95 % des mesures : il montre
    les cas lents que la moyenne cache.
    """
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = (len(sorted_values) - 1) * pct / 100
    low = int(position)
    high = min(low + 1, len(sorted_values) - 1)
    fraction = position - low
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * fraction


def human_bytes(count: float) -> str:
    """Affiche un nombre d'octets avec une unité lisible (1 Kio = 1024 octets)."""
    for unit in ("octets", "Kio", "Mio", "Gio"):
        if count < 1024 or unit == "Gio":
            return f"{count:.0f} {unit}" if unit == "octets" else f"{count:.1f} {unit}"
        count /= 1024
    return f"{count:.1f} Gio"   # jamais atteint, rassure les outils d'analyse


def mbit_per_s(byte_count: int, seconds: float) -> float:
    """Débit en mégabits par seconde (charge utile seule, hors en-têtes IP/TCP/UDP)."""
    return byte_count * 8 / seconds / 1_000_000 if seconds > 0 else 0.0


def format_counter(counter: Counter) -> str:
    """Met en forme un compteur d'erreurs : « refusée=40, timeout=5 »."""
    return ", ".join(f"{kind}={count}" for kind, count in counter.most_common())


class _BaseStats:
    """Partie commune : horodatage du test."""

    def __init__(self) -> None:
        self.started = 0.0        # time.perf_counter() au début du test
        self.ended = 0.0          # idem à la fin (0 tant que le test tourne)
        self.interrupted = False  # arrêt par Ctrl+C ou SIGTERM avant la fin prévue

    def elapsed(self) -> float:
        """Durée réelle du test, en secondes."""
        if not self.started:
            return 0.0
        return (self.ended or time.perf_counter()) - self.started


# --- TCP ----------------------------------------------------------------------

class TcpStats(_BaseStats):
    def __init__(self) -> None:
        super().__init__()
        self.attempted = 0        # connexions tentées
        self.succeeded = 0        # connexions établies
        self.failed = 0           # connexions échouées (refus, timeout...)
        self.abandoned = 0        # tentatives encore en cours quand le test s'est arrêté
        self.active = 0           # connexions ouvertes à l'instant t
        self.peak_active = 0      # maximum atteint par `active`
        self.closed_by_peer = 0   # connexions fermées par le serveur pendant le maintien
        self.bytes_sent = 0
        self.bytes_recv = 0
        self.connect_errors: Counter = Counter()   # échecs de connexion, par type
        self.io_errors: Counter = Counter()        # erreurs après connexion, par type
        self.latencies_ms: list[float] = []        # temps d'établissement de chaque connexion
        # Mémoire de l'affichage précédent, pour calculer les valeurs « par seconde ».
        self._prev_time = 0.0
        self._prev_succeeded = 0
        self._prev_latency_count = 0

    def live_fields(self, cfg: RunConfig) -> list[tuple[str, str]]:
        """Valeurs à afficher pendant le test (calculées sur la dernière seconde)."""
        now = time.perf_counter()
        interval = now - (self._prev_time or self.started)
        rate = (self.succeeded - self._prev_succeeded) / interval if interval > 0 else 0.0
        window = sorted(self.latencies_ms[self._prev_latency_count:])
        self._prev_time = now
        self._prev_succeeded = self.succeeded
        self._prev_latency_count = len(self.latencies_ms)
        return [
            ("temps", f"{self.elapsed():.0f}/{cfg.duration:g} s"),
            ("ouvertes", str(self.active)),
            ("tentées", str(self.attempted)),
            ("réussies", str(self.succeeded)),
            ("échouées", str(self.failed)),
            ("conn/s", f"{rate:.0f}"),
            ("p50", f"{percentile(window, 50):.1f} ms"),
            ("p95", f"{percentile(window, 95):.1f} ms"),
            ("envoyé", human_bytes(self.bytes_sent)),
            ("reçu", human_bytes(self.bytes_recv)),
        ]

    def final_fields(self, cfg: RunConfig) -> list[tuple[str, str]]:
        """Valeurs à afficher une fois le test fini : totaux et moyennes sur tout le test."""
        elapsed = self.elapsed()
        latency = self.latency_summary()
        return [
            ("temps", f"{elapsed:.0f}/{cfg.duration:g} s"),
            ("tentées", str(self.attempted)),
            ("réussies", str(self.succeeded)),
            ("échouées", str(self.failed)),
            ("conn/s moy.", f"{self.succeeded / elapsed:.0f}" if elapsed > 0 else "0"),
            ("p50", f"{latency.get('p50', 0.0):.1f} ms"),
            ("p95", f"{latency.get('p95', 0.0):.1f} ms"),
            ("envoyé", human_bytes(self.bytes_sent)),
            ("reçu", human_bytes(self.bytes_recv)),
        ]

    def headline(self) -> str:
        """Le résultat en une ligne, pour l'historique."""
        elapsed = self.elapsed() or 1.0
        return (f"{self.succeeded} réussies, {self.failed} échouées, "
                f"{self.succeeded / elapsed:.0f} conn/s")

    def latency_summary(self) -> dict[str, float]:
        """min / moyenne / percentiles / max des temps de connexion, en millisecondes."""
        values = sorted(self.latencies_ms)
        if not values:
            return {}
        return {
            "min": values[0],
            "moy": sum(values) / len(values),
            "p50": percentile(values, 50),
            "p95": percentile(values, 95),
            "p99": percentile(values, 99),
            "max": values[-1],
        }

    def summary_lines(self, cfg: RunConfig) -> list[str]:
        elapsed = self.elapsed()
        success_pct = 100 * self.succeeded / self.attempted if self.attempted else 0.0
        rate = self.succeeded / elapsed if elapsed > 0 else 0.0
        lines = [
            "===== Résumé TCP =====" + ("  (test interrompu : résultats partiels)" if self.interrupted else ""),
            f"Cible                   : {cfg.ip}:{cfg.port} ({cfg.display_name})",
            f"Durée                   : {elapsed:.1f} s (prévu : {cfg.duration:g} s)",
            f"Concurrence             : {cfg.concurrency}"
            f"{' avec montée progressive' if cfg.ramp else ''}"
            f" (pic de connexions ouvertes : {self.peak_active})",
            f"Connexions tentées      : {self.attempted}",
            f"  réussies              : {self.succeeded} ({success_pct:.1f} %)",
            f"  échouées              : {self.failed}",
        ]
        if self.abandoned:
            lines.append(f"  en cours à l'arrêt    : {self.abandoned}")
        lines.append(f"Connexions par seconde  : {rate:.1f}")

        latency = self.latency_summary()
        if latency:
            def group(*names: str) -> str:
                return " | ".join(f"{name} {latency[name]:.2f}" for name in names) + " (ms)"
            lines.append(f"Latence de connexion    : {group('min', 'moy', 'max')}")
            lines.append(f"  percentiles           : {group('p50', 'p95', 'p99')}")
        lines.append(
            f"Octets envoyés          : {human_bytes(self.bytes_sent)}"
            f" ({mbit_per_s(self.bytes_sent, elapsed):.2f} Mbit/s)"
        )
        lines.append(
            f"Octets reçus            : {human_bytes(self.bytes_recv)}"
            f" ({mbit_per_s(self.bytes_recv, elapsed):.2f} Mbit/s)"
        )
        if cfg.size and not cfg.hold:
            lines.append("  (sans --hold, l'outil ferme la connexion sans attendre de réponse)")
        if self.closed_by_peer:
            lines.append(f"Fermées par le serveur  : {self.closed_by_peer} (pendant le maintien --hold)")
        if self.connect_errors:
            lines.append(f"Échecs de connexion     : {format_counter(self.connect_errors)}")
        if self.io_errors:
            lines.append(f"Erreurs après connexion : {format_counter(self.io_errors)}")
        if self.connect_errors.get("locale (client)"):
            lines.append(
                "  Les erreurs « locale (client) » viennent de CETTE machine (ports source ou "
                "fichiers ouverts épuisés), pas du serveur testé."
            )
        return lines

    def csv_values(self) -> dict[str, object]:
        elapsed = self.elapsed()
        errors = self.connect_errors + self.io_errors
        row: dict[str, object] = {
            "conn_tentees": self.attempted,
            "conn_reussies": self.succeeded,
            "conn_echouees": self.failed,
            "conn_par_s": f"{self.succeeded / elapsed:.1f}" if elapsed > 0 else "0",
            "octets_envoyes": self.bytes_sent,
            "octets_recus": self.bytes_recv,
            "mbit_s_envoi": f"{mbit_per_s(self.bytes_sent, elapsed):.3f}",
            "erreurs": format_counter(errors),
        }
        for name, value in self.latency_summary().items():
            row[f"lat_{name}_ms"] = f"{value:.3f}"
        return row


# --- UDP ----------------------------------------------------------------------

class UdpStats(_BaseStats):
    """Compteurs UDP.

    Deux threads écrivent ici : celui d'envoi et celui de réception. Chaque
    compteur n'est modifié que par UN seul des deux, donc aucun verrou n'est
    nécessaire.
    """

    def __init__(self) -> None:
        super().__init__()
        # Écrits par le thread d'envoi
        self.sent = 0
        self.bytes_sent = 0
        self.unreachable_on_send = 0    # « port injoignable » signalé lors d'un envoi
        self.send_errors: Counter = Counter()
        # Écrits par le thread de réception
        self.received = 0
        self.bytes_recv = 0
        self.unreachable_on_recv = 0    # « port injoignable » signalé lors d'une réception
        # Mémoire de l'affichage précédent
        self._prev_time = 0.0
        self._prev_sent = 0
        self._prev_bytes = 0

    @property
    def unreachable(self) -> int:
        """Nombre de messages ICMP « port injoignable » renvoyés par la cible."""
        return self.unreachable_on_send + self.unreachable_on_recv

    def live_fields(self, cfg: RunConfig) -> list[tuple[str, str]]:
        now = time.perf_counter()
        interval = now - (self._prev_time or self.started)
        pps = (self.sent - self._prev_sent) / interval if interval > 0 else 0.0
        mbit = mbit_per_s(self.bytes_sent - self._prev_bytes, interval)
        self._prev_time = now
        self._prev_sent = self.sent
        self._prev_bytes = self.bytes_sent
        return [
            ("temps", f"{self.elapsed():.0f}/{cfg.duration:g} s"),
            ("envoyés", str(self.sent)),
            ("paquets/s", f"{pps:.0f}"),
            ("Mbit/s", f"{mbit:.2f}"),
            ("reçus", str(self.received)),
            ("port injoignable", str(self.unreachable)),
        ]

    def final_fields(self, cfg: RunConfig) -> list[tuple[str, str]]:
        """Valeurs à afficher une fois le test fini : totaux et moyennes sur tout le test."""
        elapsed = self.elapsed()
        return [
            ("temps", f"{elapsed:.0f}/{cfg.duration:g} s"),
            ("envoyés", str(self.sent)),
            ("paquets/s moy.", f"{self.sent / elapsed:.0f}" if elapsed > 0 else "0"),
            ("Mbit/s moy.", f"{mbit_per_s(self.bytes_sent, elapsed):.2f}"),
            ("reçus", str(self.received)),
            ("port injoignable", str(self.unreachable)),
        ]

    def headline(self) -> str:
        """Le résultat en une ligne, pour l'historique."""
        elapsed = self.elapsed() or 1.0
        return f"{self.sent} envoyés, {self.sent / elapsed:.0f} paquets/s, {self.received} reçus"

    def summary_lines(self, cfg: RunConfig) -> list[str]:
        elapsed = self.elapsed()
        pps = self.sent / elapsed if elapsed > 0 else 0.0
        lines = [
            "===== Résumé UDP =====" + ("  (test interrompu : résultats partiels)" if self.interrupted else ""),
            f"Cible                   : {cfg.ip}:{cfg.port} ({cfg.display_name})",
            f"Durée                   : {elapsed:.1f} s (prévu : {cfg.duration:g} s)",
            f"Paquets envoyés         : {self.sent} ({cfg.size} octets chacun, {human_bytes(self.bytes_sent)} au total)",
            f"Débit réel              : {pps:.1f} paquets/s (cible : {cfg.rate}), "
            f"{mbit_per_s(self.bytes_sent, elapsed):.2f} Mbit/s",
            f"Paquets reçus en retour : {self.received} ({human_bytes(self.bytes_recv)})",
        ]
        if not self.received:
            lines.append(
                "  Aucune réponse : c'est normal si le service ne répond pas à ces données. "
                "L'UDP ne garantit ni la livraison ni une réponse."
            )
        if self.unreachable:
            lines.append(
                f"Port injoignable (ICMP) : {self.unreachable} signalement(s). "
                "Rien n'écoute sur ce port UDP, ou un pare-feu rejette les paquets."
            )
        if self.send_errors:
            lines.append(f"Erreurs d'envoi         : {format_counter(self.send_errors)}")
        if elapsed > 0 and pps < cfg.rate * 0.9 and not self.interrupted:
            lines.append(
                "  Le débit réel est nettement sous la cible : c'est cette machine (Python) "
                "qui plafonne, pas le serveur testé."
            )
        return lines

    def csv_values(self) -> dict[str, object]:
        elapsed = self.elapsed()
        errors = Counter(self.send_errors)
        if self.unreachable:
            errors["port injoignable"] = self.unreachable
        return {
            "octets_envoyes": self.bytes_sent,
            "octets_recus": self.bytes_recv,
            "mbit_s_envoi": f"{mbit_per_s(self.bytes_sent, elapsed):.3f}",
            "paquets_envoyes": self.sent,
            "pps_reel": f"{self.sent / elapsed:.1f}" if elapsed > 0 else "0",
            "paquets_recus": self.received,
            "erreurs": format_counter(errors),
        }


# --- Export CSV ---------------------------------------------------------------

def write_csv(path: Path, cfg: RunConfig, stats: TcpStats | UdpStats) -> None:
    """Ajoute une ligne de résumé au fichier CSV (créé avec son en-tête s'il n'existe pas)."""
    row: dict[str, object] = {field: "" for field in CSV_FIELDS}
    row.update({
        "date": datetime.now().isoformat(timespec="seconds"),
        "cible": str(cfg.ip),
        "nom": cfg.name,
        "port": cfg.port,
        "proto": cfg.proto,
        "duree_prevue_s": f"{cfg.duration:g}",
        "duree_reelle_s": f"{stats.elapsed():.2f}",
        "interrompu": "oui" if stats.interrupted else "non",
        "taille_octets": cfg.size,
    })
    if cfg.proto == "tcp":
        row.update({
            "concurrence": cfg.concurrency,
            "ramp": "oui" if cfg.ramp else "non",
            "hold_s": f"{cfg.hold:g}",
        })
    else:
        row["debit_cible_pps"] = cfg.rate
    row.update(stats.csv_values())

    is_new = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        if is_new:
            writer.writeheader()
        writer.writerow(row)
