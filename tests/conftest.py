"""Fixtures communes : dossier fictif EX-001 (aucune donnée réelle) dans un répertoire temporaire, faux client Agent SDK.

Les tests ne touchent jamais cases/ du projet : chaque test reçoit son propre DATA_DIR temporaire.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIX = Path(__file__).resolve().parent / "fixtures"
EXEMPLO = ROOT / "kyc_agent" / "prompts" / "exemple_state.json"
sys.path.insert(0, str(ROOT))


def construir(case: Path):
    """Génère les livrables de la version de state.json (vrai build, en sous-processus)."""
    r = subprocess.run([sys.executable, "-X", "utf8", "-m", "kyc_agent.build", str(case)], cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8")
    assert r.returncode == 0, r.stderr[-2000:]
    return r


@pytest.fixture
def dados(tmp_path, monkeypatch):
    """DATA_DIR temporaire contenant cases/EX-001 en v1 (construite), pièces en attente : aucune."""
    data = tmp_path / "data"
    case = data / "cases" / "EX-001"
    (case / "work").mkdir(parents=True)
    (case / "documentos").mkdir()
    (case / "documentos" / "contrato_social.pdf").write_bytes(b"%PDF-1.4 ficticio")
    st = json.loads(EXEMPLO.read_text(encoding="utf-8"))
    st.pop("_aviso", None)
    (case / "state.json").write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    shutil.copy(FIX / "screening_ficticio.json", case / "work" / "screening.json")
    (case / "dossie.json").write_text(json.dumps({"nome": "Dossiê fictício", "criado_em": "2026-01-02T10:00:00+00:00"}), encoding="utf-8")
    (case / "documentos.json").write_text(json.dumps({"base": "documentos", "n": 1, "tamanho_total": 17, "arquivos": [
        {"caminho": "contrato_social.pdf", "tamanho": 17, "integrado_em_versao": 1}]}), encoding="utf-8")
    construir(case)
    monkeypatch.setenv("DATA_DIR", str(data))
    monkeypatch.setenv("KYC_BACKUP_DIR", str(tmp_path / "sauvegardes"))  # jamais les sauvegardes réelles
    monkeypatch.setenv("KYC_BACKUP_AUTO", "0")
    from webapp import app as webapp
    monkeypatch.setattr(webapp, "DATA_DIR", data)
    monkeypatch.setattr(webapp, "CASES", data / "cases")
    return data


@pytest.fixture
def caso(dados):
    return dados / "cases" / "EX-001"


def adicionar_pendentes(case: Path, sufixo="1"):
    """Une nouvelle pièce + une réponse en attente ; retourne le dossier de run avec pedido.json."""
    nome = f"nova_{sufixo}.txt"
    (case / "documentos" / nome).write_text("nova peça", encoding="utf-8")
    inv = json.loads((case / "documentos.json").read_text(encoding="utf-8"))
    inv["arquivos"].append({"caminho": nome, "tamanho": 9, "integrado_em_versao": None})
    (case / "documentos.json").write_text(json.dumps(inv), encoding="utf-8")
    rdir = case / "respostas" / f"20260116T100000_T1_upload_{sufixo}"
    rdir.mkdir(parents=True)
    (rdir / "resposta.json").write_text(json.dumps({"item": "T1", "acao": "upload_comprovante", "recebida_em": "2026-01-16",
                                                    "texto": "ok", "arquivos": [], "status": "recebida"}), encoding="utf-8")
    rd = case / "runs" / f"20260116T110000_{sufixo}_pedido"
    rd.mkdir(parents=True)
    (rd / "pedido.json").write_text(json.dumps({"estado": "solicitado", "pendentes": {"pecas": [nome], "respostas": [rdir.name]}}),
                                    encoding="utf-8")
    return rd, nome, rdir


@pytest.fixture
def modo_real(monkeypatch):
    """Force le mode réel du worker (clé factice) : les tests remplacent toujours le client SDK, aucun appel n'est fait."""
    monkeypatch.setenv("KYC_ANTHROPIC_API_KEY", "cle-factice-pour-tests")
    monkeypatch.delenv("KYC_AGENT_SIMULATE", raising=False)


def cliente_falso(roteiro):
    """Fabrique une classe compatible ClaudeSDKClient dont receive_response exécute `roteiro(self)` (générateur async)."""
    class Cliente:
        def __init__(self, options):
            self.o = options
            self.prompt = None
            self.prompts = []  # toutes les requêtes (la première + les relances)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def query(self, prompt):
            if self.prompt is None:
                self.prompt = prompt
            self.prompts.append(prompt)

        def receive_response(self):
            return roteiro(self)

        def receive_messages(self):  # flux continu : fin du roteiro = fin du processus moteur
            return roteiro(self)
    return Cliente


def empreinte(case: Path) -> dict:
    """Contenu exact du dossier (hors runs/)."""
    import hashlib
    return {p.relative_to(case).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(case.rglob("*")) if p.is_file() and "runs" not in p.relative_to(case).parts}


@pytest.fixture(autouse=True)
def _sem_cle_do_registro(monkeypatch):
    """Les tests ne lisent jamais la vraie clé API enregistrée sur la machine (registre Windows)."""
    monkeypatch.setenv("KYC_CLE_REGISTRE", "0")


@pytest.fixture(autouse=True)
def _sem_pecas_bloqueadas():
    """Après chaque test, rend les fichiers temporaires à nouveau modifiables (nettoyage pytest sous Windows)."""
    yield
    os.environ.pop("KYC_AGENT_SIM_PASSO", None)
