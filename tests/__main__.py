"""Lance toute la suite de tests et garde un rapport daté (traçabilité après chaque modification).

    .venv/Scripts/python -m tests            tous les tests (dont la non-régression TC-187, ~1 min)
    .venv/Scripts/python -m tests rapide     sans les tests lents

Rapport : tests/rapports/AAAA-MM-JJ_HHMMSS.txt (résultat de chaque test + empreinte du code testé).
Aucun test n'appelle l'API Anthropic ni ne modifie cases/ : tout se passe dans des répertoires temporaires.
"""
from __future__ import annotations

import hashlib
import io
import sys
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
RAPPORTS = Path(__file__).resolve().parent / "rapports"
CODE = ["kyc_agent", "webapp", "tests"]


def empreinte_code() -> list[str]:
    linhas = []
    for d in CODE:
        for p in sorted((ROOT / d).rglob("*")):
            if p.is_file() and p.suffix in (".py", ".html", ".md", ".json") and "rapports" not in p.parts and "__pycache__" not in p.parts:
                linhas.append(f"{hashlib.sha256(p.read_bytes()).hexdigest()[:12]}  {p.relative_to(ROOT).as_posix()}")
    return linhas


def main() -> int:
    args = ["-v", "-p", "no:cacheprovider", str(Path(__file__).parent)]
    if "rapide" in sys.argv[1:]:
        args += ["-m", "not lento"]
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = pytest.main(args)
    saida = buf.getvalue()
    print(saida)
    RAPPORTS.mkdir(exist_ok=True)
    agora = datetime.now()
    rel = RAPPORTS / f"{agora:%Y-%m-%d_%H%M%S}.txt"
    rel.write_text("\n".join([
        f"Rapport de tests — {agora:%d/%m/%Y %H:%M:%S}",
        f"Résultat : {'SUCCÈS' if code == 0 else 'ÉCHEC'} (code {code})",
        f"Commande : python -m tests {' '.join(sys.argv[1:])}".rstrip(),
        "", "=" * 100, saida, "=" * 100,
        "Empreinte du code testé (SHA-256, 12 premiers caractères) :", *empreinte_code(), ""]), encoding="utf-8")
    print(f"\nRapport écrit : {rel.relative_to(ROOT)}")
    return int(code)


if __name__ == "__main__":
    sys.exit(main())
