"""Journal complet de l'agent (kyc_agent/journal.py + worker + page /c/<id>/journal)."""
import json
from types import SimpleNamespace

import claude_agent_sdk as sdk
from fastapi.testclient import TestClient

from conftest import adicionar_pendentes, cliente_falso
from kyc_agent import journal as jrn
from kyc_agent import worker
from test_worker import USO, produzir_v2, result_ok


def roteiro_completo(caso, peca):
    async def roteiro(cli):
        # refus d'une écriture sur les pièces (règles du dossier) : doit apparaître dans le journal
        r = await cli.o.can_use_tool("Write", {"file_path": str(caso / "documentos" / "x.txt"), "content": "x"}, SimpleNamespace())
        assert isinstance(r, sdk.PermissionResultDeny)
        await cli.o.can_use_tool("Agent", {"description": "lote 1"}, SimpleNamespace(tool_use_id="tuA"))
        yield sdk.AssistantMessage(content=[sdk.ThinkingBlock(thinking="Je commence par la pièce nouvelle.", signature="s"),
                                            sdk.TextBlock("Lecture de la pièce."),
                                            sdk.ToolUseBlock(id="tu1", name="Read", input={"file_path": f"documentos/{peca}"}),
                                            sdk.ToolUseBlock(id="tuA", name="Agent", input={"description": "lote 1"})],
                                   model="m", usage=USO, message_id="m1")
        yield sdk.UserMessage(content=[sdk.ToolResultBlock(tool_use_id="tu1", content=[{"type": "text", "text": "nova peça " * 5000}])])
        yield sdk.TaskStartedMessage(subtype="task_started", data={}, task_id="t1", description="lote 1", uuid="u", session_id="s",
                                     tool_use_id="tuA", task_type="local_agent")
        yield sdk.AssistantMessage(content=[sdk.TextBlock("Constat du sous-agent.")], model="m", parent_tool_use_id="tuA",
                                   usage=USO, message_id="m2")
        yield sdk.TaskNotificationMessage(subtype="task_notification", data={}, task_id="t1", status="completed", output_file="",
                                          summary="2 constats", uuid="u", session_id="s", tool_use_id="tuA")
        produzir_v2(caso)
        yield result_ok()
    return roteiro


def test_journal_complet_d_une_execution(caso, modo_real, monkeypatch):
    rd, peca, _ = adicionar_pendentes(caso)
    monkeypatch.setattr(sdk, "ClaudeSDKClient", cliente_falso(roteiro_completo(caso, peca)))
    worker.main(str(caso), str(rd))
    evs = jrn.ler(rd / jrn.NOME)
    tipos = [e["tipo"] for e in evs]
    for t in ("inicio", "recusa", "raciocinio", "texto", "ferramenta", "resultado_ferramenta", "subagente_inicio", "subagente_fim",
              "resultado", "fim"):
        assert t in tipos, t
    ini = evs[tipos.index("inicio")]
    assert "Mets à jour le dossier EX-001" in ini["pedido"] and ini["prompt_sistema"]  # consignes complètes
    rec = evs[tipos.index("recusa")]
    assert rec["ferramenta"] == "Write" and "lecture seule" in rec["motivo"]
    res = evs[tipos.index("resultado_ferramenta")]["conteudo"]
    assert res["truncado"] and res["tamanho"] == len("nova peça " * 5000) and len(res["sha256"]) == 64  # long texte : tronqué + trace
    sub = [e for e in evs if e["tipo"] == "texto" and e["agente"] != "principal"]
    assert sub and sub[0]["agente"] == "subagente: lote 1" and sub[0]["texto"] == "Constat du sous-agent."
    assert evs[-1]["tipo"] == "fim" and evs[-1]["estado"] == "concluido" and evs[-1]["versao_produzida"] == 2
    assert jrn.verificar(rd / jrn.NOME) == {"ok": True, "n": len(evs), "primeira_invalida": None}
    # copie figée avec la version + lien version -> exécution
    copia = caso / "versions" / "v2" / jrn.NOME_VERSAO
    assert copia.read_bytes() == (rd / jrn.NOME).read_bytes()
    assert json.loads((caso / "versions" / "v2" / "metricas.json").read_text(encoding="utf-8"))["execucao"] == rd.name

    # page de consultation
    from webapp.app import app
    cli = TestClient(app)
    cli.cookies.set("lang", "fr")
    html = cli.get("/c/EX-001/journal").text
    assert "Journal complet de l&#39;agent" in html or "Journal complet de l'agent" in html
    assert "Chaîne d&#39;empreintes vérifiée" in html or "Chaîne d'empreintes vérifiée" in html
    assert "Action refusée" in html and "subagente: lote 1" in html and "Raisonnement" in html
    assert "Je commence par la pièce nouvelle." in cli.get("/c/EX-001/journal?versao=2").text
    assert cli.get(f"/c/EX-001/journal.jsonl?run={rd.name}").content == (rd / jrn.NOME).read_bytes()
    assert cli.get("/c/EX-001/journal.jsonl?run=../../etc").status_code == 404
    # lien dans l'historique des versions
    assert "/c/EX-001/journal?versao=2" in cli.get("/c/EX-001/v2/painel").text

    # une ligne modifiée après coup est détectée
    linhas = (rd / jrn.NOME).read_text(encoding="utf-8").splitlines()
    e = json.loads(linhas[3])
    e["texto" if "texto" in e else "tipo"] = "modifié"
    linhas[3] = json.dumps(e, ensure_ascii=False)
    (rd / jrn.NOME).write_text("\n".join(linhas) + "\n", encoding="utf-8")
    assert jrn.verificar(rd / jrn.NOME)["primeira_invalida"] == 3
    assert "chaîne d&#39;empreintes rompue" in cli.get(f"/c/EX-001/journal?run={rd.name}").text \
        or "chaîne d'empreintes rompue" in cli.get(f"/c/EX-001/journal?run={rd.name}").text


def test_journal_en_cas_d_echec(caso, modo_real, monkeypatch):
    rd, _, _ = adicionar_pendentes(caso)

    async def roteiro(cli):
        yield sdk.AssistantMessage(content=[sdk.TextBlock("Début.")], model="m", usage=USO, message_id="m1")
        raise RuntimeError("panne réseau simulée")

    monkeypatch.setattr(sdk, "ClaudeSDKClient", cliente_falso(roteiro))
    worker.main(str(caso), str(rd))
    evs = jrn.ler(rd / jrn.NOME)
    assert evs[-1]["tipo"] == "fim" and evs[-1]["estado"] == "erro" and "panne réseau simulée" in evs[-1]["erro"]
    assert evs[-1]["remise_en_etat"] is True
    assert not (caso / "versions" / "v2").exists()  # dossier remis en état ; le journal de l'échec reste dans runs/
    assert jrn.verificar(rd / jrn.NOME)["ok"]


def test_journal_en_simulation(caso, monkeypatch):
    monkeypatch.setenv("KYC_AGENT_SIMULATE", "1")
    monkeypatch.setenv("KYC_AGENT_SIM_PASSO", "0")
    rd, _, _ = adicionar_pendentes(caso)
    worker.main(str(caso), str(rd))
    evs = jrn.ler(rd / jrn.NOME)
    assert evs[0]["tipo"] in ("etapa", "inicio") and evs[-1]["tipo"] == "fim" and evs[-1]["estado"] == "simulado"
