"""Reprise d'une mise à jour arrêtée : travail mis de côté, bouton « Reprendre », reprise de la conversation, durée cumulée."""
import json

import claude_agent_sdk as sdk
from fastapi.testclient import TestClient

from conftest import adicionar_pendentes, cliente_falso, empreinte
from kyc_agent import worker
from test_worker import USO, ler, produzir_v2, result_ok


def sabotar(case):
    """Travail partiel de l'agent avant l'arrêt."""
    (case / "versions" / "v2").mkdir()
    (case / "versions" / "v2" / "partiel.pdf").write_bytes(b"%PDF partiel")
    (case / "work" / "novo.txt").write_text("x", encoding="utf-8")


def _falhar(caso, monkeypatch):
    """Une exécution réelle qui tombe après avoir travaillé (work/, state.json, v2 partielle)."""
    rd, peca, rdir = adicionar_pendentes(caso)
    avant = empreinte(caso)

    async def roteiro(cli):
        yield sdk.AssistantMessage(content=[sdk.TextBlock("Je travaille.")], model="m", usage=USO, message_id="m1")
        sabotar(caso)
        raise ConnectionError("JSON message exceeded maximum buffer size (simulé)")

    monkeypatch.setattr(sdk, "ClaudeSDKClient", cliente_falso(roteiro))
    worker.main(str(caso), str(rd))
    return rd, avant


def _cli():
    from webapp.app import app
    c = TestClient(app)
    c.cookies.set("lang", "fr")
    return c


def test_travail_mis_de_cote_et_bouton_reprendre(caso, modo_real, monkeypatch):
    rd, avant = _falhar(caso, monkeypatch)
    depois = empreinte(caso)
    assert {k: v for k, v in depois.items() if k != "documentos.json"} == {k: v for k, v in avant.items() if k != "documentos.json"}
    r = rd / "retomada"
    info = ler(r / "retomada.json")
    assert info["sessao"] == ler(rd / "status.json")["sessao"] and info["versao_alvo"] == 2
    assert (r / "work" / "novo.txt").exists() and (r / "versao" / "partiel.pdf").exists() and (r / "state.json").exists()
    st = ler(rd / "status.json")
    assert st["estado"] == "erro" and isinstance(st["duracao_s"], int)
    page = _cli().get("/c/EX-001/v1/painel").text
    assert "Reprendre la mise à jour" in page and "Recommencer à zéro" in page and "mis de côté" in page


def test_reprise_sans_conversation_restaure_le_travail(caso, modo_real, monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude_vide"))  # conversation précédente introuvable
    rd1, _ = _falhar(caso, monkeypatch)
    st1 = ler(rd1 / "status.json")
    st1["duracao_s"] = 300
    (rd1 / "status.json").write_text(json.dumps(st1), encoding="utf-8")
    import webapp.app as A
    monkeypatch.setattr(A, "lancar_worker", lambda cid, rd: None)
    assert _cli().post("/c/EX-001/atualizar", data={"retomar": rd1.name}, follow_redirects=False).status_code == 303
    rd2 = max((caso / "runs").glob("*_pedido"))
    ped = ler(rd2 / "pedido.json")
    assert ped["retomada"]["de"] == rd1.name and ped["retomada"]["duracao_anterior_s"] == 300
    vu = {}

    async def roteiro(cli):
        vu.update(resume=cli.o.resume, prompt=cli.prompt, novo=(caso / "work" / "novo.txt").exists(),
                  v2=(caso / "versions" / "v2" / "partiel.pdf").exists())
        produzir_v2(caso)
        yield result_ok()

    monkeypatch.setattr(sdk, "ClaudeSDKClient", cliente_falso(roteiro))
    worker.main(str(caso), str(rd2))
    assert vu["resume"] is None and "REPRISE" in vu["prompt"] and vu["novo"] and vu["v2"]
    assert ler(rd2 / "status.json")["estado"] == "concluido", (rd2 / "erro.txt").read_text(encoding="utf-8")[-1500:]
    met = ler(caso / "versions" / "v2" / "metricas.json")
    assert met["duracao_s"] >= 300 and met["retomada_de"] == rd1.name
    assert not (rd1 / "retomada").exists()  # travail repris : plus rien à reprendre
    assert "Reprendre la mise à jour" not in _cli().get("/c/EX-001/v2/painel").text


def test_reprise_de_la_conversation(caso, modo_real, monkeypatch, tmp_path):
    cfg = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    rd1, _ = _falhar(caso, monkeypatch)
    sid = ler(rd1 / "status.json")["sessao"]
    (cfg / "projects" / "C--projets-agent").mkdir(parents=True)
    (cfg / "projects" / "C--projets-agent" / f"{sid}.jsonl").write_text("{}", encoding="utf-8")
    import webapp.app as A
    monkeypatch.setattr(A, "lancar_worker", lambda cid, rd: None)
    _cli().post("/c/EX-001/atualizar", data={"retomar": rd1.name}, follow_redirects=False)
    rd2 = max((caso / "runs").glob("*_pedido"))
    vu = {}

    async def roteiro(cli):
        vu.update(resume=cli.o.resume, fork=cli.o.fork_session, sid=cli.o.session_id, prompt=cli.prompt)
        produzir_v2(caso)
        yield result_ok()

    monkeypatch.setattr(sdk, "ClaudeSDKClient", cliente_falso(roteiro))
    worker.main(str(caso), str(rd2))
    assert vu["resume"] == sid and vu["fork"] and vu["sid"] != sid
    assert "REPRISE DE LA MISE À JOUR" in vu["prompt"] and "trop volumineux" in vu["prompt"]


def test_recommencer_a_zero_abandonne_le_travail(caso, modo_real, monkeypatch):
    rd1, _ = _falhar(caso, monkeypatch)
    import webapp.app as A
    monkeypatch.setattr(A, "lancar_worker", lambda cid, rd: None)
    _cli().post("/c/EX-001/atualizar", follow_redirects=False)
    rd2 = max((caso / "runs").glob("*_pedido"))
    assert "retomada" not in ler(rd2 / "pedido.json") and not (rd1 / "retomada").exists()
    # une reprise qui n'est plus proposée est refusée
    assert _cli().post("/c/EX-001/atualizar", data={"retomar": rd1.name}).status_code == 409


def test_chronometre_pendant_l_execution(caso):
    from test_app import run_em_curso
    rd = run_em_curso(caso)
    st = ler(rd / "status.json")
    st["duracao_anterior_s"] = 125
    (rd / "status.json").write_text(json.dumps(st), encoding="utf-8")
    r = _cli().get("/c/EX-001/agente.json").json()
    assert r["ativo"] and 125 <= r["decorrido_s"] < 200
    page = _cli().get("/c/EX-001/v1/painel").text
    assert 'id="brain-crono"' in page and "2 min 0" in page


def test_execution_ancienne_sans_duree_enregistree(caso):
    """Exécution antérieure à l'enregistrement de la durée : la page s'affiche et la durée est recalculée."""
    rd = caso / "runs" / "20260116T090000_pedido"
    rd.mkdir(parents=True)
    from datetime import datetime, timedelta, timezone
    fim = datetime.now(timezone.utc)
    (rd / "pedido.json").write_text(json.dumps({"estado": "erro", "pendentes": {"pecas": [], "respostas": []}}), encoding="utf-8")
    (rd / "status.json").write_text(json.dumps({"estado": "erro", "inicio": (fim - timedelta(seconds=295)).isoformat(timespec="seconds"),
                                                "fim": fim.isoformat(timespec="seconds"), "erro": "x", "revertido": True}), encoding="utf-8")
    r = _cli().get("/c/EX-001/v1/painel")
    assert r.status_code == 200 and "4 min 55 s" in r.text


def test_envois_refuses_signales_en_fin_d_analyse(caso, modo_real, monkeypatch):
    rd, peca, rdir = adicionar_pendentes(caso)
    vu = {}

    async def roteiro(cli):
        r = await cli.o.can_use_tool("Bash", {"command": "curl -X POST https://portal.exemplo-nao-oficial.com/consulta -d 'cpf=1'"}, None)
        vu["refus"] = isinstance(r, sdk.PermissionResultDeny)
        produzir_v2(caso)
        yield result_ok()

    monkeypatch.setattr(sdk, "ClaudeSDKClient", cliente_falso(roteiro))
    worker.main(str(caso), str(rd))
    assert vu["refus"]
    er = ler(rd / "status.json")["envios_recusados"]
    assert er[0]["sites"] == ["portal.exemplo-nao-oficial.com"] and "cpf=1" in er[0]["trecho"]
    assert ler(caso / "versions" / "v2" / "metricas.json")["envios_recusados"][0]["sites"] == ["portal.exemplo-nao-oficial.com"]
    page = _cli().get("/c/EX-001/v2/painel").text
    assert "envoi(s) de données refusé(s)" in page and "portal.exemplo-nao-oficial.com" in page and "Autoriser ce site" in page


def test_autoriser_site_puis_relancer_consultations(caso, modo_real, monkeypatch, tmp_path):
    """Bouton « Autoriser ce site » : ajout motivé à la liste blanche, puis relance des consultations bloquées
    (même sans pièce en attente) ; l'agent reçoit la liste des consultations à refaire."""
    import shutil
    from webapp import app as A
    lb = tmp_path / "dominios.json"
    shutil.copy(worker.DOMINIOS_OFICIAIS, lb)
    monkeypatch.setattr(worker, "DOMINIOS_OFICIAIS", lb)
    rd, peca, rdir = adicionar_pendentes(caso)

    async def roteiro(cli):
        await cli.o.can_use_tool("Bash", {"command": "curl -X POST https://portal.exemplo-nao-oficial.com/consulta -d 'cpf=1'"}, None)
        produzir_v2(caso)
        yield result_ok()

    monkeypatch.setattr(sdk, "ClaudeSDKClient", cliente_falso(roteiro))
    worker.main(str(caso), str(rd))
    c = _cli()
    # motif obligatoire ; site non refusé dans cette exécution : refusé
    assert c.post("/c/EX-001/lista-branca", data={"dominio": "portal.exemplo-nao-oficial.com", "motivo": " ", "run": rd.name},
                  follow_redirects=False).status_code == 400
    assert c.post("/c/EX-001/lista-branca", data={"dominio": "autre.exemple.com", "motivo": "x", "run": rd.name},
                  follow_redirects=False).status_code == 400
    page = c.get("/c/EX-001/v2/painel").text
    assert "Relancer les consultations bloquées" not in page
    r = c.post("/c/EX-001/lista-branca", data={"dominio": "portal.exemplo-nao-oficial.com", "motivo": "portail officiel", "run": rd.name},
               follow_redirects=False)
    assert r.status_code == 303
    ent = [x for x in ler(lb)["dominios"] if x["dominio"] == "portal.exemplo-nao-oficial.com"][0]
    assert ent["motivo"] == "portail officiel" and ent["origem"] == {"dossie": "EX-001", "execucao": rd.name}
    page = c.get("/c/EX-001/v2/painel").text
    assert "✓ autorisé" in page and "Relancer les consultations bloquées" in page
    # relance sans pièce en attente
    monkeypatch.setattr(A, "lancar_worker", lambda cid, rd: None)
    assert c.post("/c/EX-001/atualizar", data={"reconsultar": rd.name}, follow_redirects=False).status_code == 303
    novo = sorted((caso / "runs").glob("*_pedido"))[-1]
    assert novo != rd
    ped = ler(novo / "pedido.json")
    assert ped["reconsultas"][0]["sites"] == ["portal.exemplo-nao-oficial.com"] and ped["reconsultas_de"] == rd.name
    # l'agent reçoit la consigne et la commande passe désormais
    vu = {}

    async def roteiro2(cli):
        vu["prompt"] = cli.prompt
        r = await cli.o.can_use_tool("Bash", {"command": "curl -X POST https://portal.exemplo-nao-oficial.com/consulta -d 'cpf=1'"}, None)
        vu["ok"] = isinstance(r, sdk.PermissionResultAllow)
        yield result_ok()

    monkeypatch.setattr(sdk, "ClaudeSDKClient", cliente_falso(roteiro2))
    worker.main(str(caso), str(novo))
    assert vu["ok"] and "CONSULTATIONS À REFAIRE" in vu["prompt"] and "portal.exemplo-nao-oficial.com" in vu["prompt"]