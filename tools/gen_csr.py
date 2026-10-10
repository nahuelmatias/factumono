"""Genera la clave privada y el pedido de certificado (CSR) para los web services de ARCA.

Uso (desde la carpeta del repo):
    uv run --with cryptography tools/gen_csr.py --cuit 20123456789 --nombre "Matias Perez" --alias facturitas-homo

Por defecto guarda todo en %USERPROFILE%\\.facturitas\\homo\\ (fuera del repo).
La clave privada (.key) NO se comparte con nadie ni se sube a ningún lado.
Lo único que se sube a ARCA es el .csr.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import stat
import sys

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def cuit_valido(cuit: str) -> bool:
    if len(cuit) != 11 or not cuit.isdigit():
        return False
    pesos = [5, 4, 3, 2, 7, 6, 5, 4, 3, 2]
    suma = sum(int(d) * p for d, p in zip(cuit[:10], pesos))
    dv = 11 - (suma % 11)
    if dv == 11:
        dv = 0
    elif dv == 10:
        dv = 9
    return dv == int(cuit[10])


def main() -> int:
    p = argparse.ArgumentParser(description="Genera clave privada + CSR para ARCA")
    p.add_argument("--cuit", required=True, help="CUIT sin guiones (el tuyo, dueño del certificado)")
    p.add_argument("--nombre", required=True, help="Nombre o razón social")
    p.add_argument("--alias", required=True, help="Alias del certificado, ej: facturitas-homo")
    p.add_argument("--ambiente", choices=["homo", "prod"], default="homo")
    p.add_argument("--out", help="Carpeta de salida (default: ~/.facturitas/<ambiente>)")
    a = p.parse_args()

    cuit = a.cuit.replace("-", "").strip()
    if not cuit_valido(cuit):
        print(f"CUIT inválido: {a.cuit}", file=sys.stderr)
        return 1

    out = pathlib.Path(a.out) if a.out else pathlib.Path.home() / ".facturitas" / a.ambiente
    out.mkdir(parents=True, exist_ok=True)
    key_path = out / f"{a.alias}.key"
    csr_path = out / f"{a.alias}.csr"

    if key_path.exists():
        print(f"Ya existe {key_path}. No lo piso para no perder una clave en uso.", file=sys.stderr)
        print("Si querés regenerar, usá otro --alias o borrá el archivo a mano.", file=sys.stderr)
        return 1

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    try:
        os.chmod(key_path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass

    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "AR"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, a.nombre),
            x509.NameAttribute(NameOID.COMMON_NAME, a.alias),
            x509.NameAttribute(NameOID.SERIAL_NUMBER, f"CUIT {cuit}"),
        ]
    )
    csr = x509.CertificateSigningRequestBuilder().subject_name(subject).sign(key, hashes.SHA256())
    csr_pem = csr.public_bytes(serialization.Encoding.PEM)
    csr_path.write_bytes(csr_pem)

    print(f"OK. Clave privada: {key_path}  (no la compartas)")
    print(f"OK. CSR:           {csr_path}  (esto es lo que se pega/sube en ARCA)\n")
    print(csr_pem.decode())
    print("Siguiente paso: guardá el certificado que te devuelva ARCA como")
    print(f"  {out / (a.alias + '.crt')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
