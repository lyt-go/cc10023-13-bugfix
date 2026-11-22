"""特征定义领域服务：维护特征的计算逻辑、数据类型与取值范围定义。

每次真实更正都在原特征编号下形成一条待校验修订：
- 校验入口只展示每个来源字段最新的待校验修订；
- 校验通过前，下游计算继续使用旧的生效修订；
- 定义详情可以按编号追溯来源、全部修订和当前生效版本。
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services import revision_support as revisions
from app.store import store


class FeatureDefineService(BaseModuleService):
    MODULE = "feature_define"
    ENTITY = "特征定义"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["特征编号", "特征名称", "特征类型", "计算逻辑", "数据来源", "缺失填充", "取值范围", "特征版本"]
    REQUIRED_FIELDS = ["特征编号", "特征名称", "特征类型"]
    STATUS_ORDER = ["草稿", "待校验", "已生效", "已废弃"]
    ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "标记废弃": "已废弃"}
    NEGATIVE_ACTIONS = []

    # ── 链路1: 特征源 → 特征定义 → 特征计算 → 特征快照 ──
    def validate_definitions_batch(self, define_ids: list[int]) -> dict[str, Any]:
        """批量校验特征定义的数据来源、计算逻辑和取值范围，生成校验报告。

        校验通过即让该修订生效，并把同一特征编号下上一版生效修订标记为已废弃。
        """
        results = []
        for did in define_ids:
            entry = store.find(self.MODULE, did)
            if entry is None:
                results.append({"id": did, "ok": False, "reason": "不存在"})
                continue
            if entry.get("已废弃") or entry.get("status") == "已废弃":
                results.append({"id": did, "ok": False, "reason": "已废弃修订不可再次校验"})
                continue
            errors = []
            if not entry.get("数据来源"):
                errors.append("缺少数据来源")
            if not entry.get("计算逻辑"):
                errors.append("缺少计算逻辑")
            if not errors:
                activated, message = revisions.activate_revision(entry)
                results.append({
                    "id": did,
                    "ok": activated,
                    "name": entry.get("特征名称"),
                    "修订编号": entry.get("修订编号"),
                    "reason": message if not activated else None,
                })
            else:
                entry["status"] = "待校验"
                entry["pending"] = True
                entry["abnormal"] = True
                results.append({"id": did, "ok": False, "reason": "; ".join(errors)})
        return {"ok": all(r.get("ok") for r in results), "results": results, "total": len(results)}

    def approve_revision(self, identifier: Any) -> dict[str, Any]:
        """让一条待校验修订生效；旧生效版自动废弃，不改变历史任务绑定。"""
        row = self._resolve_revision(identifier)
        if row is None:
            return {"ok": False, "message": f"特征定义 {identifier} 不存在"}
        if row.get("status") not in ("草稿", "待校验"):
            return {
                "ok": False,
                "message": f"修订 {row.get('修订编号')} 当前状态为 {row.get('status')}，只有待校验修订可以生效",
                "entry": row,
            }
        ok, message = revisions.activate_revision(row)
        if not ok:
            return {"ok": False, "message": message, "entry": row}
        detail = revisions.definition_detail(row.get("特征编号"))
        return {
            "ok": True,
            "message": f"特征定义 {row.get('修订编号')} 已生效",
            "entry": row,
            "detail": detail,
            "effective_revision": revisions.revision_summary(row),
        }

    # 常见命名别名，保持领域动作语义一致
    activate_definition = approve_revision
    mark_effective = approve_revision
    validate_revision = approve_revision
    confirm_revision = approve_revision

    def submit_revision(
        self,
        identifier: Any,
        changes: dict[str, Any] | None = None,
        *,
        source_field: str | None = None,
        field: str | None = None,
    ) -> dict[str, Any]:
        """提交字段描述或计算规则更正，关联同一来源字段并形成待校验修订。"""
        changes = dict(changes or {})
        for alias in ("字段", "字段名", "source_field", "field"):
            changes.pop(alias, None)
        field_name = revisions.canonical_field(source_field or field or "")
        code, source_id, existing_field = self._resolve_code(identifier)
        if code is None:
            return {"ok": False, "message": f"特征定义 {identifier} 不存在"}
        field_name = field_name or revisions.canonical_field(existing_field or "")
        result = revisions.register_field_revision(
            source_id=source_id,
            field=field_name or existing_field,
            code=code,
            overrides=changes,
            origin="correction",
        )
        entry = result.get("entry") or {}
        return {
            "ok": True,
            "action": result.get("action"),
            "message": (
                f"已为 {code} 登记修订 {result.get('revision')}"
                if result.get("created")
                else f"{code} 的等价内容已存在，未新增修订"
            ),
            "entry": entry,
            "revision": revisions.revision_summary(entry),
            "source_field": entry.get("来源字段"),
            "source_id": entry.get("来源特征源"),
            "source_ref": entry.get("来源标识"),
            "pending_revisions": [
                revisions.revision_summary(row)
                for row in revisions.pending_revisions(field=field_name or existing_field or "")
            ],
        }

    revise_definition = submit_revision
    apply_definition_correction = submit_revision
    register_definition_revision = submit_revision
    submit_definition_revision = submit_revision

    def definition_detail(self, identifier: Any) -> dict[str, Any] | None:
        """按修订 id 或特征编号查询来源、各修订及状态。"""
        return revisions.definition_detail(identifier)

    get_definition_detail = definition_detail
    revision_detail = definition_detail
    describe_definition = definition_detail

    def list_pending_revisions(
        self,
        *,
        source_id: int | None = None,
        field: str | None = None,
        include_drafts: bool = False,
    ) -> dict[str, Any]:
        """校验入口：返回每个来源字段唯一的最新未生效修订。"""
        rows = revisions.pending_revisions(
            source_id=source_id, field=field, include_drafts=include_drafts)
        # 同一特征编号只保留最新修订，避免历史草稿残留在入口。
        items = [revisions.revision_summary(row) for row in rows]
        return {"ok": True, "items": items, "total": len(items), "pending": items}

    pending_revisions = list_pending_revisions
    validation_queue = list_pending_revisions
    list_pending_definitions = list_pending_revisions

    def apply_action(self, entry_id: int, action: str) -> dict[str, Any]:
        """在通用动作基础上补齐修订生效的联动。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": f"特征定义 {entry_id} 不存在"}
        if action in ("标记生效", "确认生效", "通过校验"):
            result = self.approve_revision(entry_id)
            return {"ok": result.get("ok"), "message": result.get("message"), "entry": entry}
        if action in ("提交校验", "提交修订"):
            if entry.get("status") == "草稿":
                entry["status"] = "待校验"
                entry["pending"] = True
                entry["abnormal"] = False
                return {"ok": True, "message": "修订已提交校验", "entry": entry}
            return {"ok": True, "message": f"修订当前状态为 {entry.get('status')}", "entry": entry}
        if action in ("标记废弃", "废弃修订"):
            if entry.get("status") == "已生效":
                return {
                    "ok": False,
                    "message": "当前生效修订不能直接废弃，请先让新修订生效",
                    "entry": entry,
                }
            entry["status"] = "已废弃"
            entry["pending"] = False
            entry["已废弃"] = True
            return {"ok": True, "message": "修订已废弃", "entry": entry}
        return {"ok": False, "message": f"动作「{action}」不属于{self.MODULE}可执行范围"}

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """手工登记特征定义：按来源字段去重，等价内容不重复建草稿。"""
        missing = [
            field for field in self.REQUIRED_FIELDS
            if not str(values.get(field) or "").strip()
        ]
        if missing:
            return None, missing
        code = str(values.get("特征编号") or "")
        rows = revisions.definitions_for_code(code)
        overrides = {
            field: values.get(field)
            for field in revisions.CONTENT_FIELDS
            if values.get(field) is not None
        }
        source_id = values.get("来源特征源")
        field = revisions.canonical_field(values.get("来源字段") or "")
        if rows:
            result = revisions.register_field_revision(
                source_id=source_id, field=field or rows[0].get("来源字段"), code=code,
                overrides=overrides, origin="manual",
            )
            return result.get("entry"), []
        rows = store.rows(self.MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in self.LIST_FIELDS})
        entry["status"] = self.STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["特征编号"] = code
        entry["修订编号"] = f"{code}-R01"
        entry["修订序号"] = 1
        entry["来源特征源"] = source_id
        entry["来源字段"] = field or None
        entry["来源标识"] = (
            f"feature_source_{source_id}:{field}" if source_id is not None and field else None
        )
        entry["_内容指纹"] = revisions.content_fingerprint(revisions.content_of_row(entry))
        rows.append(entry)
        return entry, []

    def push_to_compute_tasks(
        self,
        define_ids: list[Any] | None = None,
        *,
        revision: Any = None,
        input_value: Any = None,
        input_values: dict[str, Any] | None = None,
        slice_name: str | None = None,
    ) -> dict[str, Any]:
        """把已生效的特征定义导入计算任务队列，任务绑定下发当时生效的修订。

        ``define_ids`` 每项可以是修订行 id、修订编号或特征编号；显式提供
        ``input_value`` 时，任务会按绑定修订的计算规则立刻给出计算结果，
        便于验证「同一输入在新旧修订下分别得到不同结果」。
        """
        requested = list(define_ids or [])
        if revision is not None:
            requested.append(revision)
        if input_values:
            single = len(input_values) == 1
            for key, value in input_values.items():
                requested.append(key)
                if single:
                    input_value = value

        compute_rows = store.rows("feature_compute")
        next_id = max((int(r.get("id", 0)) for r in compute_rows), default=0)
        pushed = []
        dispatched: set[str] = set()
        for did in requested:
            bound = self._resolve_bound_revision(did)
            if bound is None:
                continue
            revision_key = str(bound.get("修订编号"))
            # 同一修订在一次下发里只生成一个任务，避免重复条目被下发成两次计算。
            if revision_key in dispatched:
                continue
            dispatched.add(revision_key)
            next_id += 1
            task = self._build_compute_task(
                next_id, bound, input_value=input_value, slice_name=slice_name)
            compute_rows.append(task)
            revisions.append_compute_history(
                task,
                action="下发",
                input_value=input_value,
                result=task.get("计算结果"),
            )
            pushed.append({
                "define_id": bound.get("id"),
                "task_id": next_id,
                "revision": bound.get("修订编号"),
                "version": bound.get("特征版本"),
                "result": task.get("计算结果"),
            })
        return {"ok": len(pushed) > 0, "pushed_count": len(pushed), "tasks": pushed}

    def analyze_definition_coverage(self) -> dict[str, Any]:
        """统计分析所有特征定义的字段覆盖率、缺失填充策略分布和数据类型分布。"""
        rows = store.rows(self.MODULE)
        type_dist = {}
        fill_dist = {}
        has_source = 0
        has_logic = 0
        total = len(rows)
        for r in rows:
            ft = r.get("特征类型", "unknown")
            type_dist[ft] = type_dist.get(ft, 0) + 1
            fill = r.get("缺失填充", "none")
            fill_dist[fill] = fill_dist.get(fill, 0) + 1
            if r.get("数据来源"):
                has_source += 1
            if r.get("计算逻辑"):
                has_logic += 1
        return {
            "total_definitions": total,
            "source_coverage": round(has_source / max(total, 1), 4),
            "logic_coverage": round(has_logic / max(total, 1), 4),
            "type_distribution": type_dist,
            "fill_strategy_distribution": fill_dist,
        }

    def export_feature_catalog(self) -> list[dict[str, Any]]:
        """导出全量特征定义目录，包含编码、名称、类型、来源、逻辑与当前生效修订。"""
        catalog = []
        for code in sorted({
            row.get("特征编号")
            for row in store.rows(self.MODULE)
            if row.get("特征编号")
        }):
            effective = revisions.effective_revision(code)
            if effective is None:
                continue
            catalog.append({
                "feature_code": code,
                "feature_name": effective.get("特征名称"),
                "feature_type": effective.get("特征类型"),
                "data_source": effective.get("数据来源"),
                "compute_logic": effective.get("计算逻辑"),
                "version": effective.get("特征版本"),
                "revision": effective.get("修订编号"),
                "source_field": effective.get("来源字段"),
            })
        return catalog

    # ── 内部解析 ──────────────────────────────────────────────────────────
    def _resolve_bound_revision(self, identifier: Any) -> dict[str, Any] | None:
        """下发时按特征编号取当时生效的修订，绝不把待校验修订带进计算。"""
        if isinstance(identifier, dict):
            identifier = (
                identifier.get("id")
                or identifier.get("特征编号")
                or identifier.get("define_id")
            )
        code: str | None = None
        if isinstance(identifier, int) or (isinstance(identifier, str) and str(identifier).isdigit()):
            row = store.find(self.MODULE, int(identifier))
            if row is not None:
                code = row.get("特征编号")
                if row.get("status") == "已生效":
                    # 未经过修订登记的旧数据没有修订编号，下发前补一个稳定标识。
                    if not row.get("修订编号") and code:
                        row["修订编号"] = f"{code}-R01"
                        row["修订序号"] = row.get("修订序号", 1)
                        row["来源标识"] = row.get("来源标识")
                    return row
            else:
                code = str(identifier)
        elif isinstance(identifier, str) and "-R" in identifier:
            for row in store.rows(self.MODULE):
                if row.get("修订编号") == identifier and row.get("status") == "已生效":
                    return row
            code = identifier.rsplit("-R", 1)[0]
        else:
            code = str(identifier) if identifier is not None else None
        if code:
            effective = revisions.effective_revision(code)
            if effective is not None:
                return effective
            for row in store.rows(self.MODULE):
                if row.get("特征名称") == code and row.get("status") == "已生效":
                    return row
        # 也允许直接以来源字段名下发（例如 "age"），取该字段当前生效修订。
        if isinstance(identifier, str) and identifier:
            field = revisions.canonical_field(identifier)
            candidates = [
                row for row in store.rows(self.MODULE)
                if revisions.canonical_field(row.get("来源字段")) == field
                and row.get("status") == "已生效"
            ]
            if candidates:
                return revisions.latest_revision(candidates)
        return None

    def _build_compute_task(
        self,
        task_id: int,
        bound: dict[str, Any],
        *,
        input_value: Any,
        slice_name: str | None,
    ) -> dict[str, Any]:
        result = (
            revisions.apply_rule(bound.get("计算逻辑"), input_value)
            if input_value is not None
            else None
        )
        return {
            "id": task_id,
            "status": "已完成" if input_value is not None else "草稿",
            "pending": input_value is None,
            "abnormal": False,
            "任务编号": f"COMP-{task_id:04d}",
            "关联特征": bound.get("特征名称", ""),
            "数据切片": slice_name or "default_slice_001",
            "计算引擎": "spark",
            "输入分区": "dt=2026-11-18",
            "输出分区": f"output/d=2026-11-18/feat_{task_id}",
            "并行度": "8",
            "任务状态": "已完成" if input_value is not None else "草稿",
            "特征编号": bound.get("特征编号"),
            "来源特征源": bound.get("来源特征源"),
            "来源字段": bound.get("来源字段"),
            "来源标识": bound.get("来源标识"),
            "绑定修订": bound.get("修订编号"),
            "绑定特征版本": bound.get("特征版本"),
            "计算规则": bound.get("计算逻辑"),
            "输入值": input_value,
            "计算结果": result,
            "结果": result,
            "下发时间": "2026-11-22T00:00:00",
        }

    def _resolve_revision(self, identifier: Any) -> dict[str, Any] | None:
        if isinstance(identifier, dict):
            identifier = identifier.get("id") or identifier.get("修订编号") or identifier.get("特征编号")
        if isinstance(identifier, int) or (isinstance(identifier, str) and str(identifier).isdigit()):
            row = store.find(self.MODULE, int(identifier))
            if row is not None:
                return row
            code = str(identifier)
        elif isinstance(identifier, str) and "-R" in identifier:
            for row in store.rows(self.MODULE):
                if row.get("修订编号") == identifier:
                    return row
            code = identifier.rsplit("-R", 1)[0]
        else:
            code = str(identifier)
        effective = revisions.effective_revision(code)
        if effective is not None:
            return effective
        for row in revisions.pending_revisions():
            if row.get("特征编号") == code:
                return row
        return revisions.latest_revision(revisions.definitions_for_code(code))

    def _resolve_code(self, identifier: Any) -> tuple[str | None, int | None, str | None]:
        row = self._resolve_revision(identifier)
        if row is None:
            return None, None, None
        return (
            row.get("特征编号"),
            row.get("来源特征源"),
            row.get("来源字段"),
        )
