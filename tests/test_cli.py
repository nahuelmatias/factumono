from typer.testing import CliRunner

from facturitas import __version__
from facturitas.cli.app import app


def test_version() -> None:
    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output
