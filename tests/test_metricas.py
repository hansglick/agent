"""Statistiques par tentative : une tentative sans version reste visible et comptée dans le cumul."""
import json

from kyc_agent.metricas import resumo


def _j(p, d):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d), encoding="utf-8")


def test_tentative_echouee_comptee_et_rattachee_a_la_version_reprise(tmp_path):
    uso_f = {"in": 10, "out": 100, "cw5": 0, "cw1h": 0, "cr": 1000}
    uso_v = {"in": 20, "out": 200, "cw5": 0, "cw1h": 0, "cr": 2000}
    _j(tmp_path / "runs/A_pedido/status.json", {"estado": "erro", "inicio": "2026-11-12T17:30:58+00:00", "uso": uso_f, "custo_usd": 8.0})
    _j(tmp_path / "runs/B_pedido/status.json", {"estado": "concluido", "inicio": "2026-11-12T17:56:56+00:00", "uso": uso_v, "custo_usd": 19.0})
    _j(tmp_path / "runs/C_pedido/status.json", {"estado": "em_curso", "inicio": "2026-11-12T19:00:00+00:00", "uso": uso_v})
    _j(tmp_path / "versions/v1/metricas.json", {"medido": True, "modelo": "claude-opus-5-5", "uso": uso_v, "custo_usd": 19.0,
                                                 "inicio": "2026-11-12T17:56:56+00:00", "execucao": "B_pedido", "retomada_de": "A_pedido"})
    m = resumo(tmp_path, [1])
    assert [(a["versao"], a["falha"], a["custo"]) for a in m["tentativas"]] == [(1, True, 8.0), (1, False, 19.0)]
    assert m["custo"] == 27.0 and m["falhas"] == 1
    assert m["uso"]["out"] == 300  # l'exécution en cours n'est pas comptée ici (ajoutée à part par le tableau de bord)


def test_tentative_echouee_sans_reprise_rattachee_a_la_version_suivante(tmp_path):
    uso = {"in": 1, "out": 1, "cw5": 0, "cw1h": 0, "cr": 0}
    _j(tmp_path / "runs/A_pedido/status.json", {"estado": "erro", "inicio": "2026-11-12T10:00:00+00:00", "uso": uso})
    _j(tmp_path / "versions/v1/metricas.json", {"medido": True, "modelo": "claude-opus-5-5", "uso": uso, "custo_usd": 1.0,
                                                 "inicio": "2026-11-12T11:00:00+00:00", "execucao": "B_pedido"})
    _j(tmp_path / "runs/D_pedido/status.json", {"estado": "erro", "inicio": "2026-11-12T12:00:00+00:00", "uso": uso})
    assert [(a["versao"], a["falha"]) for a in resumo(tmp_path, [1])["tentativas"]] == [(1, True), (1, False), (2, True)]


def test_duree_de_chaque_tentative(tmp_path):
    _j(tmp_path / "runs/A_pedido/status.json", {"estado": "erro", "inicio": "2026-11-12T10:00:00+00:00",
                                                "fim": "2026-11-12T10:00:03+00:00", "uso": {}})  # arrêt immédiat, rien consommé
    _j(tmp_path / "runs/B_pedido/status.json", {"estado": "erro", "inicio": "2026-11-12T10:10:00+00:00",
                                                "fim": "2026-11-12T10:40:00+00:00", "duracao_execucao_s": 1700, "uso": {"out": 5}})
    _j(tmp_path / "versions/v1/metricas.json", {"medido": True, "modelo": "claude-opus-5-5", "uso": {"out": 9}, "custo_usd": 1.0,
                                                 "duracao_s": 2900, "inicio": "2026-11-12T11:00:00+00:00",
                                                 "fim": "2026-11-12T11:20:00+00:00", "execucao": "C_pedido", "retomada_de": "B_pedido"})
    t = resumo(tmp_path, [1])["tentativas"]
    assert [a["duracao_s"] for a in t] == [3, 1700, 1200]  # la version : sa propre durée, pas le cumul avec la reprise


def test_fin_d_une_execution_disparue_au_dernier_signe_de_vie(tmp_path):
    import os
    import time
    from kyc_agent.worker import fim_trabalho
    (tmp_path / "log.jsonl").write_text("{}\n", encoding="utf-8")
    vieux = time.time() - 600
    os.utime(tmp_path / "log.jsonl", (vieux, vieux))
    st = {"atualizado_em": "2000-01-01T00:00:00+00:00"}
    assert abs(__import__("datetime").datetime.fromisoformat(fim_trabalho(tmp_path, st)).timestamp() - vieux) < 2
    os.utime(tmp_path / "log.jsonl", None)  # processus vivant : maintenant
    assert abs(__import__("datetime").datetime.fromisoformat(fim_trabalho(tmp_path, st)).timestamp() - time.time()) < 5


def test_fin_tient_compte_du_battement(tmp_path):
    """Agent vivant mais silencieux (attente des sous-agents) : le battement récent compte, la fin est maintenant."""
    import time
    from datetime import datetime, timezone
    from kyc_agent.worker import fim_trabalho
    st = {"atualizado_em": "2000-01-01T00:00:00+00:00", "vivo_em": datetime.now(timezone.utc).isoformat()}
    assert abs(datetime.fromisoformat(fim_trabalho(tmp_path, st)).timestamp() - time.time()) < 5


def test_plan_de_l_agent_chef_donne_la_progression(tmp_path):
    from kyc_agent.worker import Registro
    reg = Registro(tmp_path, {})
    reg.plano([{"content": "Lire", "status": "completed"}, {"content": "Screening", "status": "in_progress", "activeForm": "Screening OFAC"},
               {"content": "Livrables", "status": "pending"}, {"content": "Traduction", "status": "pending"}])
    assert reg.st["plano"] == {**reg.st["plano"], "total": 4, "feitas": 1, "pct": 25, "em_curso": "Screening OFAC", "revisoes": 0}
    reg.plano([{"content": x, "status": "completed" if i < 2 else "pending"} for i, x in enumerate("abcde")])  # +1 tâche
    assert (reg.st["plano"]["pct"], reg.st["plano"]["revisoes"]) == (40, 1)
    st = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
    assert st["plano"]["total"] == 5
