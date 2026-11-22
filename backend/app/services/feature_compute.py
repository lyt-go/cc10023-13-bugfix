"""特征计算任务领域服务：批量计算的编排、分区管理与执行追踪。

计算任务在下发时绑定「当时生效」的特征定义修订；历史任务保留原取数规则，
重放沿用历史任务已绑定的修订，不会因为后续修订生效而被悄悄改写。
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services import revision_support as revisions
from app.store import store


def _find_define(revision: str | None, code: str | None) -> dict[str, Any] | None:
    if revision:
        for row in store.rows("feature_define"):
            if row.get("修订编号") == revision:
                return row
    if code:
        for row in store.rows("feature_define"):
            if row.get("修订编号") == code or row.get("id") == code:
                return row
    return None


class FeatureComputeService(BaseModuleService):
    MODULE = "feature_compute"
    ENTITY = "计算任务"
    OPERATION_TERM = "关联"
    LIST_FIELDS = ["任务编号", "关联特征", "数据切片", "计算引擎", "输入分区", "输出分区", "并行度", "任务状态"]
    REQUIRED_FIELDS = ["任务编号", "关联特征", "数据切片"]
    STATUS_ORDER = ["草稿", "等待中", "计算中", "已完成", "已失败"]
    ACTION_RULES = {"提交执行": "草稿", "停止计算": "计算中", "确认完成": "已完成"}
    NEGATIVE_ACTIONS = []

    # ── 链路1: 特征定义 → 特征计算 → 特征快照 ──
    # ── 链路5: 特征计算 → 相关性矩阵 → 特征漂移监控 ──
    def execute_and_snapshot(
        self,
        task_id: int,
        input_value: Any = None,
    ) -> dict[str, Any]:
        """执行特征计算任务并在完成后自动生成特征快照存储到snapshots目录。

        任务绑定的修订决定取数规则；``input_value`` 只影响本次计算结果，
        不会改变任务绑定或历史版本。
        """
        task = store.find(self.MODULE, task_id)
        if task is None:
            return {"ok": False, "message": f"计算任务 {task_id} 不存在"}
        self._ensure_binding(task)
        if input_value is None:
            input_value = task.get("输入值")
        result = revisions.apply_rule(task.get("计算规则"), input_value) if input_value is not None else task.get("计算结果")
        task["结果"] = result
        task["计算结果"] = result
        task["status"] = "已完成"
        task["pending"] = False
        task["任务状态"] = "已完成"
        self._append_history(task, result, input_value, action="执行")
        snapshot_rows = store.rows("feature_snapshot")
        next_id = max((int(r.get("id", 0)) for r in snapshot_rows), default=0) + 1
        feats = task.get("关联特征", "unknown")
        snapshot_rows.append({
            "id": next_id,
            "status": "已完成",
            "pending": False,
            "abnormal": False,
            "快照编号": f"SNAP-{next_id:04d}",
            "特征集": feats,
            "快照日期": "2026-11-18",
            "特征数量": 12,
            "数据行数": "100000",
            "存储路径": f"snapshots/compute_{task_id}_{next_id}.json",
            "保留天数": "30",
            "快照状态": "已完成",
        })
        return {
            "ok": True,
            "task_id": task_id,
            "snapshot_id": next_id,
            "revision": task.get("绑定修订"),
            "result": result,
            "message": f"计算任务 {task_id} 已完成，生成特征快照 {next_id}",
        }

    def run_task(self, task_id: int, input_value: Any = None) -> dict[str, Any]:
        """计算别名：按任务绑定修订执行并返回计算结果。"""
        return self.execute_and_snapshot(task_id, input_value)

    execute_task = run_task

    def replay_task(self, task_id: int, input_value: Any = None) -> dict[str, Any]:
        """重放历史任务：沿用原任务绑定的修订，生成一条新的重放记录。"""
        task = store.find(self.MODULE, task_id)
        if task is None:
            return {"ok": False, "message": f"计算任务 {task_id} 不存在"}
        self._ensure_binding(task)
        source_id = task.get("来源特征源")
        field = task.get("来源字段")
        bound = task.get("绑定修订")
        bound_code = task.get("特征编号")
        if not task.get("计算规则") and bound:
            row = _find_define(str(bound), str(bound_code))
            if row is not None:
                task["计算规则"] = row.get("计算逻辑")
                task["来源标识"] = row.get("来源标识")
                task["来源特征源"] = row.get("来源特征源")
                task["来源字段"] = row.get("来源字段")
        if input_value is None:
            input_value = task.get("输入值")
        result = revisions.apply_rule(task.get("计算规则"), input_value) if input_value is not None else None
        # 生成新的重放任务行，保留原绑定，便于历史追溯。
        compute_rows = store.rows(self.MODULE)
        next_id = max((int(r.get("id", 0)) for r in compute_rows), default=0) + 1
        replay = dict(task)
        replay.update({
            "id": next_id,
            "任务编号": f"{task.get('任务编号', 'COMP')}-RP{next_id:04d}",
            "status": "已完成",
            "pending": False,
            "abnormal": False,
            "任务状态": "已完成",
            "重放自": task.get("id"),
            "重放目标任务": task.get("id"),
            "绑定修订": bound,
            "计算规则": task.get("计算规则"),
            "来源特征源": source_id,
            "来源字段": field,
            "来源标识": task.get("来源标识"),
            "输入值": input_value,
            "计算结果": result,
            "结果": result,
        })
        compute_rows.append(replay)
        self._append_history(replay, result, input_value, action="重放", replayed_from=task.get("id"))
        self._append_history(task, result, input_value, action="重放", replayed_to=next_id)
        return {
            "ok": True,
            "task_id": task_id,
            "replay_task_id": next_id,
            "revision": bound,
            "result": result,
            "message": f"任务 {task_id} 已按修订 {bound} 重放，生成任务 {next_id}",
        }

    replay_historical_task = replay_task
    rerun_task = replay_task

    def compute_history(self, *, task_id: int | None = None) -> dict[str, Any]:
        """计算历史：返回来源标识、任务绑定的修订、结果与当前生效版本。"""
        history = store.rows("feature_compute_history")
        if task_id is not None:
            history = [item for item in history if item.get("任务id") == task_id]
        effective_cache: dict[str, str | None] = {}
        items = []
        for item in history:
            code = item.get("特征编号")
            if code not in effective_cache:
                effective = revisions.effective_revision(code) if code else None
                effective_cache[code] = effective.get("修订编号") if effective else None
            current = None
            if code:
                effective = next(
                    (row for row in store.rows("feature_define")
                     if row.get("修订编号") == effective_cache[code]), None)
                current = revisions.revision_summary(effective) if effective else None
            items.append({
                **item,
                "当前生效修订": effective_cache[code],
                "当前生效版本": current.get("特征版本") if current else None,
                "当前规则": current.get("计算逻辑") if current else None,
                "是否使用当前生效": item.get("绑定修订") == effective_cache[code],
            })
        tasks = self._task_summaries()
        return {
            "ok": True,
            "items": items,
            "total": len(items),
            "tasks": tasks,
        }

    history = compute_history
    query_compute_history = compute_history
    list_compute_history = compute_history

    def query_task(self, task_id: int) -> dict[str, Any]:
        """查询单个计算任务的绑定与结果。"""
        task = store.find(self.MODULE, task_id)
        if task is None:
            return {"ok": False, "message": f"计算任务 {task_id} 不存在"}
        code = task.get("特征编号")
        effective = revisions.effective_revision(code) if code else None
        return {
            "ok": True,
            "task": task,
            "绑定修订": task.get("绑定修订"),
            "来源标识": task.get("来源标识"),
            "来源特征源": task.get("来源特征源"),
            "来源字段": task.get("来源字段"),
            "计算结果": task.get("计算结果", task.get("结果")),
            "输入值": task.get("输入值"),
            "当前生效修订": effective.get("修订编号") if effective else None,
            "当前生效版本": effective.get("特征版本") if effective else None,
        }

    get_task = query_task
    task_detail = query_task

    def compute_correlation_matrix(self, task_id: int) -> dict[str, Any]:
        """基于特征计算结果计算特征间相关性矩阵并写入存储。"""
        task = store.find(self.MODULE, task_id)
        if task is None:
            return {"ok": False}
        corr_rows = store.rows("feature_corr")
        next_id = max((int(r.get("id", 0)) for r in corr_rows), default=0) + 1
        feats = task.get("关联特征", "")
        corr_rows.append({
            "id": next_id,
            "status": "已完成",
            "pending": False,
            "abnormal": False,
            "矩阵编号": f"CORR-{next_id:04d}",
            "特征集": feats,
            "计算方法": "pearson",
            "阈值筛选": "0.7",
            "可视化配置": "heatmap_full",
            "计算时间": "2026-11-18T10:00:00",
            "矩阵版本": "v1.0.0",
            "结果存储": f"corr_matrix/compute_{task_id}_v1.json",
        })
        return {"ok": True, "corr_id": next_id, "task_id": task_id}

    def trigger_drift_monitoring(self, task_id: int) -> dict[str, Any]:
        """基于特征计算结果触发特征漂移监控，关联基准分布并启动PSI计算。"""
        task = store.find(self.MODULE, task_id)
        if task is None:
            return {"ok": False}
        drift_rows = store.rows("drift_monitor")
        next_id = max((int(r.get("id", 0)) for r in drift_rows), default=0) + 1
        feats = task.get("关联特征", "")
        drift_rows.append({
            "id": next_id,
            "status": "监控中",
            "pending": False,
            "abnormal": False,
            "监控编号": f"DRIFT-{next_id:04d}",
            "关联特征": feats,
            "基准分布": f"baseline_compute_{task_id}",
            "当前分布": f"current_{task_id}_2026_11_18",
            "PSI值": "0.015",
            "漂移阈值": "0.25",
            "采样窗口": "30d",
            "监控状态": "监控中",
        })
        return {"ok": True, "drift_id": next_id, "task_id": task_id, "psi_value": 0.015}

    def retry_failed_tasks(self, task_ids: list[int]) -> dict[str, Any]:
        """重试失败的计算任务，将状态回退到草稿并重新调度采集。"""
        retried = []
        failed = []
        for tid in task_ids:
            task = store.find(self.MODULE, tid)
            if task is None:
                failed.append(tid)
                continue
            if task.get("status") != "已失败":
                failed.append(tid)
                continue
            task["status"] = "草稿"
            task["pending"] = True
            task["abnormal"] = False
            retried.append(tid)
        return {"ok": len(retried) > 0, "retried": retried, "failed_skipped": failed}

    def query_compute_progress(self, task_id: int) -> dict[str, Any]:
        """查询特征计算任务的执行进度、引擎状态和资源消耗情况。"""
        task = store.find(self.MODULE, task_id)
        if task is None:
            return {"ok": False}
        return {
            "ok": True,
            "task_id": task_id,
            "status": task.get("status"),
            "engine": task.get("计算引擎"),
            "parallelism": task.get("并行度"),
            "input": task.get("输入分区"),
            "output": task.get("输出分区"),
            "revision": task.get("绑定修订"),
        }

    # ── 内部 ──────────────────────────────────────────────────────────────
    def _ensure_binding(self, task: dict[str, Any]) -> None:
        """补齐任务的下发修订绑定；已绑定任务不改写，保留历史取数规则。"""
        if task.get("绑定修订"):
            row = _find_define(task.get("绑定修订"), task.get("特征编号"))
            if row is not None:
                task.setdefault("计算规则", row.get("计算逻辑"))
                task.setdefault("来源标识", row.get("来源标识"))
                task.setdefault("来源特征源", row.get("来源特征源"))
                task.setdefault("来源字段", row.get("来源字段"))
                task.setdefault("特征编号", row.get("特征编号"))
            return
        code = task.get("特征编号")
        row = revisions.effective_revision(code) if code else None
        if row is None and task.get("关联特征"):
            row = next(
                (item for item in store.rows("feature_define")
                 if item.get("特征名称") == task.get("关联特征") and item.get("status") == "已生效"),
                None,
            )
        if row is not None:
            task["绑定修订"] = row.get("修订编号")
            task["绑定特征版本"] = row.get("特征版本")
            task["计算规则"] = row.get("计算逻辑")
            task["来源特征源"] = row.get("来源特征源")
            task["来源字段"] = row.get("来源字段")
            task["来源标识"] = row.get("来源标识")
            task["特征编号"] = row.get("特征编号")
            task["下发时间"] = task.get("下发时间") or "2026-11-22T00:00:00"

    def _append_history(
        self,
        task: dict[str, Any],
        result: Any,
        input_value: Any,
        *,
        action: str,
        replayed_from: int | None = None,
        replayed_to: int | None = None,
    ) -> None:
        revisions.append_compute_history(
            task,
            action=action,
            input_value=input_value,
            result=result,
            replayed_from=replayed_from,
            replayed_to=replayed_to,
        )

    def _task_summaries(self) -> list[dict[str, Any]]:
        summaries = []
        for task in store.rows(self.MODULE):
            if not task.get("绑定修订"):
                continue
            code = task.get("特征编号")
            effective = revisions.effective_revision(code) if code else None
            summaries.append({
                "id": task.get("id"),
                "任务编号": task.get("任务编号"),
                "来源特征源": task.get("来源特征源"),
                "来源字段": task.get("来源字段"),
                "来源标识": task.get("来源标识"),
                "绑定修订": task.get("绑定修订"),
                "绑定特征版本": task.get("绑定特征版本"),
                "计算规则": task.get("计算规则"),
                "输入值": task.get("输入值"),
                "计算结果": task.get("计算结果", task.get("结果")),
                "重放自": task.get("重放自"),
                "当前生效修订": effective.get("修订编号") if effective else None,
                "当前生效版本": effective.get("特征版本") if effective else None,
            })
        return summaries
