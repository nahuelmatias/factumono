"""Punto de entrada del ejecutable sin consola (`factumono-gui.exe`): abre la interfaz gráfica."""

import sys

from factumono.cli.app import main

if __name__ == "__main__":
    sys.argv = [sys.argv[0], "app", *sys.argv[1:]]
    main()
