#!/usr/bin/env python3
"""Point d'entrée : `python stress.py` (ou `py stress.py` sous Windows).

Tout le code se trouve dans le dossier stresser/ ; ce fichier ne fait que
vérifier la version de Python et lancer la ligne de commande.
"""

import sys

if sys.version_info < (3, 11):
    sys.exit("Cet outil demande Python 3.11 ou plus récent (version actuelle : "
             f"{sys.version_info.major}.{sys.version_info.minor}).")

from stresser.cli import main

if __name__ == "__main__":
    sys.exit(main())
