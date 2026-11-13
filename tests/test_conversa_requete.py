"""Requête de mise à jour (crayon orange, pastille « R ») et conversation avec l'agent (bulle, lecture seule)."""
import json

import claude_agent_sdk as sdk
import pytest
from fastapi.testclient import TestClient

from conftest import cliente_falso
from kyc_agent import conversa as K
from kyc_agent import worker
from test_worker import ler, produzir_v2, result_ok


@pytest.fixture
def cli(dados):
    from webapp.app import app
    c = TestClient(app)
    c.cookies.set("lang", "fr")
    return c


# ------------------------------------------------------------------ requête de mise à jour
def test_requete_active_le_bouton_et_s_efface(cli, caso):
    page = cli.get("/c/EX-001/v1/painel").text
    assert 'id="btn-requete"' in page and 'id="cta-r"' not in page and 'id="cta-n"' not in page
    assert 'id="btn-atualizar" disabled' in page or 'disabled\n' in page.split('id="btn-atualizar"')[1][:200]
    assert cli.post("/c/EX-001/requete", data={"texto": "  "}, follow_redirects=False).status_code == 400
    assert cli.post("/c/EX-001/requete", data={"texto": "Revoir C17 avec la pièce 12"}, follow_redirects=False).status_code == 303
    rq = ler(caso / "requete.json")
    assert rq["texto"] == "Revoir C17 avec la pièce 12" and rq["em"]
    page = cli.get("/c/EX-001/v1/painel").text
    assert 'id="cta-r"' in page and "Requête de mise à jour" in page and "Delete the request" in page and "Done" in page
    assert "document.getElementById('dlg-agent').showModal()" in page  # bouton dégrisé sans pièce en attente
    assert cli.post("/c/EX-001/requete/apagar", follow_redirects=False).status_code == 303
    assert not (caso / "requete.json").exists()
    assert 'id="cta-r"' not in cli.get("/c/EX-001/v1/painel").text


def test_pastille_documents_en_attente(cli, caso):
    (caso / "documentos" / "x.pdf").write_bytes(b"x")
    inv = ler(caso / "documentos.json")
    inv["arquivos"].append({"caminho": "x.pdf", "tamanho": 1, "integrado_em_versao": None})
    (caso / "documentos.json").write_text(json.dumps(inv), encoding="utf-8")
    page = cli.get("/c/EX-001/v1/painel").text
    assert '<span class="cta-badge cta-n" id="cta-n"' in page


def test_requete_transmise_a_l_agent_puis_effacee(cli, caso, modo_real, monkeypatch):
    from webapp import app as A
    monkeypatch.setattr(A, "lancar_worker", lambda cid, rd: None)
    cli.post("/c/EX-001/requete", data={"texto": "Refaire le screening de Fulano"})
    assert cli.post("/c/EX-001/atualizar", follow_redirects=False).status_code == 303
    rd = sorted((caso / "runs").glob("*_pedido"))[-1]
    assert ler(rd / "pedido.json")["requete"]["texto"] == "Refaire le screening de Fulano"
    assert cli.post("/c/EX-001/requete/apagar", follow_redirects=False).status_code == 409  # en cours de traitement
    vu = {}

    async def roteiro(c):
        vu["prompt"] = c.prompt
        r = await c.o.can_use_tool("Write", {"file_path": str(caso / "requete.json"), "content": "{}"}, None)
        vu["protegida"] = isinstance(r, sdk.PermissionResultDeny)
        produzir_v2(caso)
        yield result_ok()

    monkeypatch.setattr(sdk, "ClaudeSDKClient", cliente_falso(roteiro))
    worker.main(str(caso), str(rd))
    assert "REQUÊTE DE MISE À JOUR DU SUPERVISEUR" in vu["prompt"] and "Refaire le screening de Fulano" in vu["prompt"]
    assert vu["protegida"]
    assert (caso / "versions" / "v2").is_dir() and not (caso / "requete.json").exists()


def test_requete_conservee_si_la_mise_a_jour_echoue(cli, caso, modo_real, monkeypatch):
    from webapp import app as A
    monkeypatch.setattr(A, "lancar_worker", lambda cid, rd: None)
    cli.post("/c/EX-001/requete", data={"texto": "Revoir C03"})
    cli.post("/c/EX-001/atualizar")
    rd = sorted((caso / "runs").glob("*_pedido"))[-1]

    async def roteiro(c):
        raise RuntimeError("panne")
        yield

    monkeypatch.setattr(sdk, "ClaudeSDKClient", cliente_falso(roteiro))
    worker.main(str(caso), str(rd))
    assert ler(caso / "requete.json")["texto"] == "Revoir C03"


# ------------------------------------------------------------------ conversation
def test_bulle_presente_sur_les_pages_du_dossier(cli):
    for tab in ("painel", "matriz", "verificacoes"):
        page = cli.get(f"/c/EX-001/v1/{tab}").text
        assert 'id="chat-fab"' in page and "Conversation avec l&#39;agent" in page, tab
    assert 'id="chat-fab"' not in cli.get("/").text


def test_question_reponse_en_simulation(cli, caso, monkeypatch):
    from webapp import app as A
    lancados = []
    monkeypatch.setattr(A, "_lancar", lambda mod, args, log: lancados.append(mod))
    monkeypatch.delenv("KYC_ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(K.time, "sleep", lambda s: None)
    r = cli.post("/c/EX-001/conversa", data={"texto": "Pourquoi C17 est-il en attente ?"})
    assert r.status_code == 200 and r.json()["estado"] == "pensando" and lancados == ["kyc_agent.conversa"]
    assert cli.post("/c/EX-001/conversa", data={"texto": "encore"}).status_code == 409
    K.main(str(caso))
    c = cli.get("/c/EX-001/conversa.json").json()
    assert c["estado"] == "pronto" and [m["papel"] for m in c["mensagens"]] == ["usuario", "agente"]
    assert c["mensagens"][0]["versao"] == 1 and "C17" in c["mensagens"][1]["texto"]
    # aucune version produite, aucun dossier modifié
    assert not (caso / "versions" / "v2").exists()
    # nouvelle conversation : l'ancienne est archivée
    assert cli.post("/c/EX-001/conversa/nova").json()["mensagens"] == []
    assert list((caso / "conversa").glob("conversa_*.json"))


def test_conversation_en_lecture_seule_et_suivie(cli, caso, modo_real, monkeypatch, tmp_path):
    from webapp import app as A
    monkeypatch.setattr(A, "_lancar", lambda mod, args, log: None)
    vu = []

    async def roteiro(c):
        p = c.o.can_use_tool
        vu.append({
            "lit": isinstance(await p("Read", {"file_path": str(caso / "state.json")}, None), sdk.PermissionResultAllow),
            "hors": isinstance(await p("Read", {"file_path": str(tmp_path / "autre.txt")}, None), sdk.PermissionResultDeny),
            "ecrit": isinstance(await p("Write", {"file_path": str(caso / "state.json"), "content": "x"}, None), sdk.PermissionResultDeny),
            "bash": isinstance(await p("Bash", {"command": "dir"}, None), sdk.PermissionResultDeny),
            "agent": isinstance(await p("Agent", {"description": "x"}, None), sdk.PermissionResultDeny),
            "web": isinstance(await p("WebFetch", {"url": "https://www.gov.br"}, None), sdk.PermissionResultAllow),
            "resume": c.o.resume, "prompt": c.prompt, "opts": c.o,
            "sistema": __import__("pathlib").Path(c.o.system_prompt["path"]).read_text(encoding="utf-8")})
        yield sdk.ResultMessage(subtype="success", duration_ms=10, duration_api_ms=9, is_error=False, num_turns=1,
                                session_id="sess-1", total_cost_usd=0.12, usage={}, result="C17 attend le comprovante.\n\nRequête suggérée : intégrer la pièce 12.")

    monkeypatch.setattr(sdk, "ClaudeSDKClient", cliente_falso(roteiro))
    monkeypatch.setattr(K, "sessao_disponivel", lambda sid: sid == "sess-1")
    antes = ler(caso / "state.json")
    cli.post("/c/EX-001/conversa", data={"texto": "Où en est C17 ?"})
    K.main(str(caso))
    v = vu[0]
    assert v["lit"] and v["hors"] and v["ecrit"] and v["bash"] and v["agent"] and v["web"]
    assert v["resume"] is None and v["prompt"] == "Où en est C17 ?" and "EX-001" in v["sistema"] and "lecture seule" in v["sistema"]
    c = cli.get("/c/EX-001/conversa.json").json()
    assert c["mensagens"][-1]["custo_usd"] == 0.12 and "Requête suggérée" in c["mensagens"][-1]["texto"]
    # deuxième question : la conversation reprend la session précédente
    cli.post("/c/EX-001/conversa", data={"texto": "Et C18 ?"})
    K.main(str(caso))
    assert vu[1]["resume"] == "sess-1"
    from test_worker import ligne_de_commande
    assert len(ligne_de_commande(v["opts"])) < 30_000  # limite de la ligne de commande Windows
    assert ler(caso / "state.json") == antes and not (caso / "versions" / "v2").exists()


def test_erreur_de_l_agent_debloque_la_conversation(cli, caso, modo_real, monkeypatch):
    from webapp import app as A
    monkeypatch.setattr(A, "_lancar", lambda mod, args, log: None)

    async def roteiro(c):
        raise RuntimeError("réseau indisponible")
        yield

    monkeypatch.setattr(sdk, "ClaudeSDKClient", cliente_falso(roteiro))
    cli.post("/c/EX-001/conversa", data={"texto": "?"})
    K.main(str(caso))
    c = cli.get("/c/EX-001/conversa.json").json()
    assert c["estado"] == "pronto" and c["mensagens"][-1]["erro"] and "réseau indisponible" in c["mensagens"][-1]["texto"]


def test_choix_du_mode_au_lancement(cli, caso, monkeypatch):
    from webapp import app as A
    monkeypatch.setattr(A, "lancar_worker", lambda cid, rd: None)
    cli.post("/c/EX-001/requete", data={"texto": "x"})
    page = cli.get("/c/EX-001/v1/painel").text
    assert 'name="modo" value="agente" checked' in page and 'name="modo" value="multi"' in page and "version bêta" in page
    cli.post("/c/EX-001/atualizar", data={"modo": "multi"})
    rd = sorted((caso / "runs").glob("*_pedido"))[-1]
    assert ler(rd / "pedido.json")["modo"] == "multi"
    (rd / "status.json").write_text(json.dumps({"estado": "erro", "fim": "2026-01-01T00:00:00+00:00"}), encoding="utf-8")
    for x in (caso / "runs").iterdir():
        __import__("shutil").rmtree(x)
    cli.post("/c/EX-001/atualizar", data={"modo": "n'importe quoi"})
    rd = sorted((caso / "runs").glob("*_pedido"))[-1]
    assert ler(rd / "pedido.json")["modo"] == "agente"
