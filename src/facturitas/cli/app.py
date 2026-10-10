"""Punto de entrada de la CLI."""

import typer

from facturitas import __version__

app = typer.Typer(help="Facturitas: facturación electrónica ARCA fácil, local y abierta.")


def _version(value: bool) -> None:
    if value:
        typer.echo(f"facturitas {__version__}")
        raise typer.Exit()


@app.callback()
def root(
    version: bool = typer.Option(
        False, "--version", callback=_version, is_eager=True, help="Muestra la versión."
    ),
) -> None:
    """Facturitas."""


def main() -> None:
    app()
