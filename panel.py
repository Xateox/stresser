#!/usr/bin/env python3
"""Panel web : `python panel.py`, puis ouvrir l'adresse affichée dans la console.

Tout le code se trouve dans stresser/web.py (serveur) et stresser/panel.html (page).
"""

import sys

if sys.version_info < (3, 11):
    sys.exit("Cet outil demande Python 3.11 ou plus récent (version actuelle : "
             f"{sys.version_info.major}.{sys.version_info.minor}).")

from stresser.web import main

if __name__ == "__main__":
    sys.exit(main())
