"""分装/合并出库的预留锁定守卫测试。

口径:分装父样品与合并各输入按「当前余量 - 活动预留的未核销锁定量」
校验可用量,已释放或已核销部分不再占用;不足时返回含样品、请求量与
可用量的结构化冲突,整次操作不写谱系与流水;批量提交与 dry-run 按顺序
使用同一口径,任一冲突整批不落库;既有操作号重放仍返回原结果。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db import connect, init_db
from app.main import app, get_conn


@pytest.fixture()
def client():
    mem = connect(":memory:")
    init_db(mem)

    def _override():
        yield mem

    app.dependency_overrides[get_conn] = _override
    with TestClient(app) as c:
        c.db = mem  # type: ignore[attr-defined]
        yield c
    app.dependency_overrides.clear()
    mem.close()


def _sample(client, op_no, sid, amount, unit="mL"):
    r = client.post("/api/v1/operations/sampling", json={
        "type": "sampling", "op_no": op_no, "sample_id": sid,
        "material": "血清", "amount": amount, "unit": unit,
    })
    assert r.status_code == 200, r.text


def _reserve(client, no, items):
    r = client.post("/api/v1/reservations", json={
        "reservation_no": no, "items": items,
    })
    assert r.status_code == 200, r.text


def _aliquot(client, op_no, parent, outputs):
    return client.post("/api/v1/operations/aliquot", json={
        "type": "aliquot", "op_no": op_no, "parent_id": parent,
        "outputs": [{"sample_id": s, "amount": a} for s, a in outputs],
    })


def _merge(client, op_no, inputs, output, **kw):
    return client.post("/api/v1/operations/merge", json={
        "type": "merge", "op_no": op_no,
        "inputs": [{"sample_id": s, "amount": a} for s, a in inputs],
        "output_id": output, **kw,
    })


def _remaining(client, sid):
    r = client.get(f"/api/v1/samples/{sid}/balance")
    assert r.status_code == 200, r.text
    return r.json()["samples"][0]["stored_remaining"]


def _write_counts(client):
    """谱系边 / 出入库流水 / 操作记录的行数(用于断言冲突未落库)。"""
    cur = client.db.execute("SELECT COUNT(*) FROM lineage_edges")
    edges = cur.fetchone()[0]
    cur = client.db.execute("SELECT COUNT(*) FROM ledger_entries")
    ledger = cur.fetchone()[0]
    cur = client.db.execute("SELECT COUNT(*) FROM operations")
    ops = cur.fetchone()[0]
    return edges, ledger, ops


# ---------------------------------------------------------------- 分装守卫

def test_aliquot_blocked_by_active_reservation(client):
    """余量 100、活动预留 80:可用量仅 20,分装 30 必须冲突且不写库。"""
    _sample(client, "OP-1", "S1", 100)
    _reserve(client, "RSV-1", [{"sample_id": "S1", "amount": 80}])
    before = _write_counts(client)

    r = _aliquot(client, "OP-2", "S1", [("A1", 30)])
    assert r.status_code == 409
    c = r.json()["conflicts"][0]
    assert c["code"] == "insufficient_balance"
    assert c["op_no"] == "OP-2"
    assert c["sample_id"] == "S1"
    assert c["requested"] == 30.0
    assert c["available"] == 20.0

    # 整次操作不落库:无操作记录、无谱系边、无流水,余量不变、子样品未建
    assert _write_counts(client) == before
    assert client.get("/api/v1/operations/OP-2").status_code == 404
    assert _remaining(client, "S1") == 100.0
    ids = {i["sample_id"] for i in client.get("/api/v1/samples").json()["items"]}
    assert "A1" not in ids


def test_aliquot_up_to_available_then_exhausted(client):
    """可用量 20 恰好可分装 20;之后可用量为 0,再分装 1 即冲突。"""
    _sample(client, "OP-1", "S1", 100)
    _reserve(client, "RSV-1", [{"sample_id": "S1", "amount": 80}])

    r = _aliquot(client, "OP-2", "S1", [("A1", 20)])
    assert r.status_code == 200, r.text
    assert r.json()["remaining"]["S1"] == 80.0

    r = _aliquot(client, "OP-3", "S1", [("A2", 1)])
    assert r.status_code == 409
    c = r.json()["conflicts"][0]
    assert c["code"] == "insufficient_balance"
    assert c["available"] == 0.0
    assert _remaining(client, "S1") == 80.0


def test_released_reservation_no_longer_locks(client):
    """预留整单释放后锁定归还,分装按完整余量放行。"""
    _sample(client, "OP-1", "S1", 100)
    _reserve(client, "RSV-1", [{"sample_id": "S1", "amount": 80}])
    r = client.delete("/api/v1/reservations/RSV-1")
    assert r.status_code == 200, r.text

    r = _aliquot(client, "OP-2", "S1", [("A1", 90)])
    assert r.status_code == 200, r.text
    assert r.json()["remaining"]["S1"] == 10.0


def test_consumed_portion_no_longer_locks(client):
    """核销部分不再占用:预留 80 核销 50 后,锁定 30、可用量 = 50 - 30。"""
    _sample(client, "OP-1", "S1", 100)
    _reserve(client, "RSV-1", [{"sample_id": "S1", "amount": 80}])
    r = client.post("/api/v1/consumptions", json={
        "consumption_no": "CV-1", "reservation_no": "RSV-1",
        "items": [{"sample_id": "S1", "amount": 50}],
    })
    assert r.status_code == 200, r.text
    # 余量 50、未核销锁定 30 -> 可用量 20
    r = _aliquot(client, "OP-2", "S1", [("A1", 30)])
    assert r.status_code == 409
    c = r.json()["conflicts"][0]
    assert c["available"] == 20.0
    assert c["requested"] == 30.0

    r = _aliquot(client, "OP-3", "S1", [("A1", 20)])
    assert r.status_code == 200, r.text
    assert r.json()["remaining"]["S1"] == 30.0


# ---------------------------------------------------------------- 合并守卫

def test_merge_inputs_blocked_by_reservation(client):
    """合并各输入分别按可用量校验,任一不足整次合并不写谱系与流水。"""
    _sample(client, "OP-1", "S1", 100)
    _sample(client, "OP-2", "S2", 50)
    _reserve(client, "RSV-1", [{"sample_id": "S1", "amount": 80}])
    before = _write_counts(client)

    # S1 可用 20 却取 30 -> 冲突;S2 未预留不受影响但整次不落库
    r = _merge(client, "OP-3", [("S1", 30), ("S2", 10)], "M1", material="混合")
    assert r.status_code == 409
    c = r.json()["conflicts"][0]
    assert c["code"] == "insufficient_balance"
    assert c["sample_id"] == "S1"
    assert c["requested"] == 30.0
    assert c["available"] == 20.0

    assert _write_counts(client) == before
    assert client.get("/api/v1/operations/OP-3").status_code == 404
    assert _remaining(client, "S1") == 100.0
    assert _remaining(client, "S2") == 50.0
    ids = {i["sample_id"] for i in client.get("/api/v1/samples").json()["items"]}
    assert "M1" not in ids

    # 各输入都在可用量之内则放行
    r = _merge(client, "OP-4", [("S1", 20), ("S2", 10)], "M1", material="混合")
    assert r.status_code == 200, r.text
    assert r.json()["remaining"] == {"S1": 80.0, "S2": 40.0, "M1": 30.0}


# ---------------------------------------------------------------- 批量与 dry-run

def test_batch_rolls_back_when_aliquot_exceeds_available(client):
    """批量按顺序使用同一口径:后面的分装超可用量,整批(含取样)不落库。"""
    _sample(client, "OP-1", "S1", 100)
    _reserve(client, "RSV-1", [{"sample_id": "S1", "amount": 80}])
    before = _write_counts(client)

    r = client.post("/api/v1/batch", json={"operations": [
        {"type": "sampling", "op_no": "OP-2", "sample_id": "T0",
         "material": "M", "amount": 10, "unit": "mL"},
        {"type": "aliquot", "op_no": "OP-3", "parent_id": "S1",
         "outputs": [{"sample_id": "A1", "amount": 30}]},
    ]})
    assert r.status_code == 409
    c = r.json()["conflicts"][0]
    assert c["code"] == "insufficient_balance"
    assert c["batch_index"] == 1
    assert c["available"] == 20.0

    assert _write_counts(client) == before
    ids = {i["sample_id"] for i in client.get("/api/v1/samples").json()["items"]}
    assert "T0" not in ids and "A1" not in ids


def test_batch_validate_sequential_same_rule(client):
    """dry-run 与正式提交同口径:批内前两笔分装各自看似可行,合计超可用量。"""
    _sample(client, "OP-1", "S1", 100)
    _reserve(client, "RSV-1", [{"sample_id": "S1", "amount": 80}])
    before = _write_counts(client)

    body = {"operations": [
        {"type": "aliquot", "op_no": "V1", "parent_id": "S1",
         "outputs": [{"sample_id": "A1", "amount": 15}]},
        {"type": "aliquot", "op_no": "V2", "parent_id": "S1",
         "outputs": [{"sample_id": "A2", "amount": 10}]},
    ]}
    r = client.post("/api/v1/batch/validate", json=body)
    assert r.status_code == 200
    data = r.json()
    assert data["valid"] is False
    c = data["conflicts"][0]
    assert c["code"] == "insufficient_balance"
    assert c["batch_index"] == 1
    assert c["requested"] == 10.0
    assert c["available"] == 5.0  # 第一笔预演后可用量 20 - 15 = 5

    # dry-run 不写主库;正式提交同一批同样整批拒绝
    assert _write_counts(client) == before
    r = client.post("/api/v1/batch", json=body)
    assert r.status_code == 409
    assert _write_counts(client) == before
    assert _remaining(client, "S1") == 100.0


def test_single_validate_uses_available(client):
    """单操作 dry-run 同样按可用量拦截,且不落库。"""
    _sample(client, "OP-1", "S1", 100)
    _reserve(client, "RSV-1", [{"sample_id": "S1", "amount": 80}])

    r = client.post("/api/v1/validate", json={
        "type": "merge", "op_no": "V1",
        "inputs": [{"sample_id": "S1", "amount": 25}],
        "output_id": "M1", "material": "混合",
    })
    assert r.status_code == 200
    data = r.json()
    assert data["valid"] is False
    assert data["conflicts"][0]["available"] == 20.0
    assert client.get("/api/v1/operations/V1").status_code == 404


# ---------------------------------------------------------------- 幂等重放

def test_replayed_op_returns_original_result_despite_reservation(client):
    """操作号重放不重新校验:预留建立后重放既有分装仍返回首次结果。"""
    _sample(client, "OP-1", "S1", 100)
    body = {
        "type": "aliquot", "op_no": "OP-2", "parent_id": "S1",
        "outputs": [{"sample_id": "A1", "amount": 60}],
    }
    r = client.post("/api/v1/operations/aliquot", json=body)
    assert r.status_code == 200, r.text
    first = r.json()

    # 对剩余 40 建预留 40(可用量 0);重放 OP-2 仍返回原结果、不重复扣减
    _reserve(client, "RSV-1", [{"sample_id": "S1", "amount": 40}])
    r = client.post("/api/v1/operations/aliquot", json=body)
    assert r.status_code == 200, r.text
    replay = r.json()
    assert replay["replayed"] is True
    assert replay == first | {"replayed": True}
    assert _remaining(client, "S1") == 40.0

    # 同号不同内容仍是幂等键冲突
    r = client.post("/api/v1/operations/aliquot", json={
        "type": "aliquot", "op_no": "OP-2", "parent_id": "S1",
        "outputs": [{"sample_id": "A1", "amount": 61}],
    })
    assert r.status_code == 409
    assert r.json()["conflicts"][0]["code"] == "op_no_conflict"
