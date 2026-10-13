"""Smoke test del ejecutable empaquetado: levanta `factumono mcp` por stdio y lista las tools.

python packaging/smoke_mcp.py build/dist/factumono/factumono.exe
"""

import asyncio
import os
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ESPERADAS = {"estado_arca", "preparar_comprobante", "emitir_comprobante", "nota_de_credito"}


async def main(exe: str) -> None:
    with tempfile.TemporaryDirectory() as home:
        env = {"FACTUMONO_HOME": home, "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}
        params = StdioServerParameters(command=exe, args=["mcp"], env=env)
        async with stdio_client(params) as (leer, escribir), ClientSession(leer, escribir) as s:
            await s.initialize()
            tools = {t.name for t in (await s.list_tools()).tools}
            faltan = ESPERADAS - tools
            assert not faltan, f"Faltan tools en el MCP empaquetado: {faltan}"
            print(f"OK: MCP empaquetado con {len(tools)} tools")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
