"""场景演练：字段发现回执幂等、修订生效、历史任务重放。"""
from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import service_for
from app.store import store


def show(label, value):
    print(f"--- {label} ---")
    import json
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


source = service_for("feature_source")
define = service_for("feature_define")
compute = service_for("feature_compute")

# 1. 先发现 id / age 两列（age 第一版规则为加一）
r1 = source.fetch_source_schema(1, ["id", {"字段": "age", "计算逻辑": "加一"}])
show("discover id, age", {
    "fields": r1["fields"],
    "source_fields": r1["source_fields"],
    "counts": r1["counts"],
    "pending": r1["pending_revisions"],
})

age_code = r1["source_fields"]["age"]
id_code = r1["source_fields"]["id"]

# 2. age 第一版生效
pending = define.list_pending_revisions(source_id=1, field="age")
show("pending age", pending)
approve = define.approve_revision(age_code)
show("approve age v1", {"ok": approve["ok"], "effective": approve["effective_revision"]})

# 3. 用数值 20 下发旧任务，规则加一
push1 = define.push_to_compute_tasks(input_values={"age": 20})
show("push task with 20", push1)
task1 = push1["tasks"][0]["task_id"]
show("task1 detail", compute.query_task(task1))

# 4. 旧 age 回执 + age 规则改乘二的更正一起送入；清单换序再送
receipt = {
    "receipt_id": "R-OLD-AGE",
    "回执": [
        {
            "字段": "age",
            "特征名称": "feature_age",
            "特征类型": "numeric",
            "计算逻辑": "加一",
            "数据来源": "feature_source_1",
            "缺失填充": "mean_fill",
            "取值范围": "[0, 9999]",
            "特征描述": f"特征源 1 的字段 age",
        },
    ],
    "更正": [
        {"字段": "age", "计算逻辑": "乘二", "特征描述": "年龄字段（更正为乘二规则）"},
    ],
}
r4 = source.sync_source_receipt(1, receipt, receipt_id="R-OLD-AGE")
show("receipt old + correction", {
    "counts": r4["counts"],
    "pending": r4["pending_revisions"],
    "registered": [
        (item["action"], item["revision"], item["code"]) for item in r4["registered"]
    ],
})

reordered = source.fetch_source_schema(1, ["age", "id"])
show("reordered list", {"fields": reordered["fields"], "counts": reordered["counts"]})

# 5. 校验入口只能看到一个待校验 age 新修订；id 没有重复定义
entry = define.list_pending_revisions(source_id=1)
show("validation queue", entry)
defs = [row for row in store.rows("feature_define") if row.get("来源特征源") == 1]
show("definitions for source 1", [
    (row["id"], row["特征编号"], row["修订编号"], row["status"], row["计算逻辑"], row["来源字段"])
    for row in defs
])

# 6. 更正通过前另下发一项 age 计算，仍得 21
push2 = define.push_to_compute_tasks(input_values={"age": 20})
show("push before approve", push2)

# 7. 同一字段新修订与迟到的旧回执一起到达
r7 = source.sync_source_receipt(
    1,
    {
        "回执": [{"字段": "age", "计算逻辑": "加一"}],
        "更正": [{"字段": "age", "计算逻辑": "乘二"}],
    },
    receipt_id="R-MIXED",
)
show("mixed receipt", {
    "counts": r7["counts"],
    "pending": r7["pending_revisions"],
})

# 8. 新修订生效
new_pending = define.list_pending_revisions(source_id=1, field="age")
show("pending age after mixed", new_pending)
approve2 = define.approve_revision(new_pending["items"][0]["修订编号"])
show("approve age v2", {"ok": approve2["ok"], "effective": approve2["effective_revision"]})

# 9. 重放旧任务 + 下发新任务，同一输入 20 应分别得 21 与 40
replay = compute.replay_task(task1, 20)
show("replay old task with 20", replay)
push3 = define.push_to_compute_tasks(input_values={"age": 20})
show("push new task with 20", push3)

# 10. 再送旧回执
r10 = source.sync_source_receipt(
    1,
    {"回执": [{"字段": "age", "计算逻辑": "加一"}]},
    receipt_id="R-LATE-OLD",
)
show("late old receipt", {"counts": r10["counts"], "pending": r10["pending_revisions"]})

# 11. 查询计算历史，核对来源标识、任务修订与当前生效版本
history = compute.compute_history()
show("compute history", {
    "total": history["total"],
    "items": history["items"],
    "tasks": history["tasks"],
})

detail = define.definition_detail(age_code)
show("definition detail by code", detail)
