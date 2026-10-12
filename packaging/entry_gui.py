"""Punto de entrada del ejecutable sin consola (`facturitas-gui.exe`): abre la interfaz gráfica."""

import sys

from facturitas.cli.app import main

if __name__ == "__main__":
    sys.argv = [sys.argv[0], "app", *sys.argv[1:]]
    main()
