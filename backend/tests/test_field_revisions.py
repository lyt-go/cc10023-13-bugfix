"""字段修订链路回归测试：发现幂等、更正生效、历史任务重放。"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import service_for
from app.store import store
import app.services.revision_support as revisions


def _reset():
    """每个用例都用干净的示例库，避免用例之间的修订污染。"""
    from app.seed import SEED_ROWS

    store._tables = {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}


def _target_source_id():
    """选一条种子特征源承接演练数据，避免和已有样例编号冲突。"""
    return 1


def _register(source, fields, receipt_id=None):
    return source.fetch_source_schema(_target_source_id(), fields, receipt_id=receipt_id)


def test_field_identity_dedup_and_reorder():
    _reset()
    source = service_for("feature_source")
    define = service_for("feature_define")

    first = _register(source, ["id", "age"])
    second = _register(source, ["age", "id"])
    third = _register(source, ["ID", "Age"])

    assert first["counts"] == {"created": 2}
    assert second["counts"] == {"draft_exists": 2}
    assert third["counts"] == {"draft_exists": 2}

    defs = revisions.definitions_for_field(_target_source_id(), "age")
    ids = revisions.definitions_for_field(_target_source_id(), "id")
    assert len(defs) == 1, "同一来源同一规范字段只登记一次"
    assert len(ids) == 1, "未改变的 id 不应有重复定义"
    # 清单换序不改变特征编号
    assert first["source_fields"] == third["source_fields"]


def test_duplicate_receipt_does_not_add_draft():
    _reset()
    source = service_for("feature_source")
    _register(source, ["id", "age"])
    before = len(store.rows("feature_define"))

    receipt = {"回执": [{"字段": "age", "计算逻辑": "加一"}]}
    r1 = source.sync_source_receipt(_target_source_id(), receipt, receipt_id="R1")
    r2 = source.sync_source_receipt(_target_source_id(), receipt, receipt_id="R1-dup")
    r3 = source.sync_source_receipt(_target_source_id(), receipt, receipt_id="R1-dup2")

    assert len(store.rows("feature_define")) == before, "同一内容重复提交不应多出草稿"
    assert r1["counts"].get("amended") == 1
    assert r2["counts"].get("draft_exists") == 1
    assert r3["counts"].get("draft_exists") == 1


def test_correction_creates_pending_revision_and_keeps_old_effective():
    _reset()
    source = service_for("feature_source")
    define = service_for("feature_define")

    _register(source, ["id", {"字段": "age", "计算逻辑": "加一"}])
    code = "FEAT-S0001-F02"
    approve = define.approve_revision(code)
    assert approve["ok"] and approve["effective_revision"]["计算逻辑"] != "乘二"

    source.sync_source_receipt(
        _target_source_id(),
        {"更正": [{"字段": "age", "计算逻辑": "乘二", "特征描述": "年龄字段（更正为乘二）"}]},
        receipt_id="C1",
    )

    pending = define.list_pending_revisions(source_id=_target_source_id())
    assert len(pending["items"]) == 1, "校验入口只应有一个待校验修订"
    assert pending["items"][0]["特征编号"] == code
    assert pending["items"][0]["计算逻辑"] == "乘二"
    assert pending["items"][0]["来源字段"] == "age"

    # 校验通过前，新任务仍使用旧生效版
    push = define.push_to_compute_tasks(input_values={"age": 20})
    assert push["tasks"][0]["revision"].endswith("-R01")
    assert push["tasks"][0]["result"] == 21

    detail = define.definition_detail(code)
    assert detail["当前生效修订"]["修订编号"].endswith("-R01")
    assert len(detail["待校验修订"]) == 1
    assert len(detail["修订"]) == 2
    assert all(r["来源标识"] == "feature_source_1:age" for r in detail["修订"])


def test_late_old_receipt_after_new_revision_effective():
    _reset()
    source = service_for("feature_source")
    define = service_for("feature_define")

    _register(source, ["id", {"字段": "age", "计算逻辑": "加一"}])
    code = "FEAT-S0001-F02"
    assert define.approve_revision(code)["ok"]
    task = define.push_to_compute_tasks(input_values={"age": 20})["tasks"][0]["task_id"]
    assert store.find("feature_compute", task)["计算结果"] == 21

    source.sync_source_receipt(
        _target_source_id(), {"更正": [{"字段": "age", "计算逻辑": "乘二"}]}, receipt_id="C2")
    pending = define.list_pending_revisions(field="age")["items"]
    assert len(pending) == 1
    assert define.approve_revision(pending[0]["修订编号"])["ok"]

    # 迟到的旧回执不得新增修订，也不得把当前定义改回旧版
    late = source.sync_source_receipt(
        _target_source_id(), {"回执": [{"字段": "age", "计算逻辑": "加一"}]}, receipt_id="R-LATE")
    assert late["counts"].get("stale_ignored") == 1
    assert define.list_pending_revisions(field="age")["total"] == 0

    detail = define.definition_detail(code)
    assert len(detail["修订"]) == 2
    assert detail["当前生效修订"]["计算逻辑"] == "乘二"

    # 重放历史任务沿用旧修订，新任务采用新修订
    replay = service_for("feature_compute").replay_task(task, 20)
    assert replay["revision"].endswith("-R01") and replay["result"] == 21
    fresh = define.push_to_compute_tasks(input_values={"age": 20})["tasks"][0]
    assert fresh["revision"].endswith("-R02") and fresh["result"] == 40


def test_history_records_source_and_bound_revision():
    _reset()
    source = service_for("feature_source")
    define = service_for("feature_define")
    compute = service_for("feature_compute")

    _register(source, ["id", {"字段": "age", "计算逻辑": "加一"}])
    code = "FEAT-S0001-F02"
    define.approve_revision(code)
    task = define.push_to_compute_tasks(input_values={"age": 20})["tasks"][0]["task_id"]
    source.sync_source_receipt(
        _target_source_id(), {"更正": [{"字段": "age", "计算逻辑": "乘二"}]}, receipt_id="C3")
    pending = define.list_pending_revisions(field="age")["items"][0]
    define.approve_revision(pending["修订编号"])
    compute.replay_task(task, 20)
    define.push_to_compute_tasks(input_values={"age": 20})

    history = compute.compute_history()
    items = [item for item in history["items"] if item.get("任务id") in (task, history["tasks"][-1]["id"])]
    for item in items:
        assert item["来源标识"] == "feature_source_1:age"
        assert item["来源字段"] == "age"
        assert item["当前生效修订"].endswith("-R02")

    # 历史任务保留原版本取数规则
    original = compute.query_task(task)
    assert original["绑定修订"].endswith("-R01")
    assert original["计算结果"] == 21
    assert original["当前生效版本"] == "v2.0.0"
    assert original["task"]["计算规则"] == "加一"


def test_definition_detail_traceability():
    _reset()
    source = service_for("feature_source")
    define = service_for("feature_define")

    _register(source, ["id", {"字段": "age", "计算逻辑": "加一"}])
    code = "FEAT-S0001-F02"
    define.approve_revision(code)
    source.sync_source_receipt(
        _target_source_id(), {"更正": [{"字段": "age", "计算逻辑": "乘二"}]}, receipt_id="C4")

    detail = define.definition_detail(code)
    assert detail["来源特征源"] == 1
    assert detail["来源字段"] == "age"
    assert detail["来源标识"] == "feature_source_1:age"
    assert [r["status"] for r in detail["修订"]] == ["已生效", "待校验"]
    assert detail["当前生效修订"]["特征版本"] == "v1.0.0"

    # 按修订 id 同样可查
    by_id = define.definition_detail(detail["待校验修订"][0]["id"])
    assert by_id["特征编号"] == code
    assert by_id["修订数量"] == 2


def test_rule_equivalence():
    assert revisions.parse_rule("加一") == ("add", 1)
    assert revisions.parse_rule("age + 1") == ("add", 1)
    assert revisions.parse_rule("*2") == ("mul", 2)
    assert revisions.parse_rule("乘二") == ("mul", 2)
    assert revisions.canonical_rule("加一") == revisions.canonical_rule("+1")
    assert revisions.apply_rule("加一", 20) == 21
    assert revisions.apply_rule("乘二", 20) == 40


if __name__ == "__main__":
    tests = [
        test_field_identity_dedup_and_reorder,
        test_duplicate_receipt_does_not_add_draft,
        test_correction_creates_pending_revision_and_keeps_old_effective,
        test_late_old_receipt_after_new_revision_effective,
        test_history_records_source_and_bound_revision,
        test_definition_detail_traceability,
        test_rule_equivalence,
    ]
    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            print(f"[PASS] {test.__name__}")
            passed += 1
        except Exception as error:  # noqa: BLE001
            print(f"[FAIL] {test.__name__}: {error}")
            import traceback
            traceback.print_exc()
            failed += 1
    print(f"\n总测试: {len(tests)}, 通过: {passed}, 失败: {failed}")
    if failed:
        sys.exit(1)
