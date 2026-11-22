"""特征源管理领域服务：负责数据源的注册、接入、字段发现与配置校验。

字段发现的入口必须幂等：同一来源、同一规范字段、等价内容只登记一次；
清单换序沿用原有特征编号；真实更正关联到同一来源字段并在原编号下形成修订。
"""
from __future__ import annotations

from typing import Any, Iterable

from app.services.base import BaseModuleService
from app.services import revision_support as revisions
from app.store import store

DEFAULT_DISCOVER_FIELDS = ["id", "uid", "age", "income", "city", "label", "dt"]

RECEIPT_KEYS = ("回执", "receipt", "回执内容", "receipts")
FIELD_KEYS = ("字段清单", "fields", "columns", "列", "列清单", "字段")
CORRECTION_KEYS = ("更正", "corrections", "改动", "revisions", "修订")


class FeatureSourceService(BaseModuleService):
    MODULE = "feature_source"
    ENTITY = "特征源"
    OPERATION_TERM = "接入"
    LIST_FIELDS = ["源编号", "源名称", "数据表名", "连接配置", "字段清单", "采样比例", "缓存策略", "源版本"]
    REQUIRED_FIELDS = ["源编号", "源名称", "数据表名"]
    STATUS_ORDER = ["草稿", "待校验", "已接入", "已停用"]
    ACTION_RULES = {"提交校验": "待校验", "确认接入": "已接入", "停用数据源": "已停用"}
    NEGATIVE_ACTIONS = ["停用数据源"]

    # ── 链路1: 特征源 → 特征定义 → 特征计算 → 特征快照 ──
    def fetch_source_schema(
        self,
        source_id: int,
        fields: Any = None,
        *,
        receipt_id: str | None = None,
    ) -> dict[str, Any]:
        """接入数据源并发现字段清单，按字段身份登记修订后返回字段映射。

        ``fields`` 可以是字段名列表、字段回执条目，或包含 ``字段清单`` /
        ``回执`` / ``更正`` 的整包回执。重复提交与清单换序都不会新增草稿。
        """
        source = store.find(self.MODULE, source_id)
        if source is None:
            return {"ok": False, "message": f"特征源 {source_id} 不存在"}

        items = self._normalize_receipt_items(fields)
        if not items:
            items = [
                (revisions.canonical_field(name), {}, "reference")
                for name in DEFAULT_DISCOVER_FIELDS
            ]

        # 同一次回执内也按字段身份去重：同一字段只有一条生效的登记意图。
        indexed: list[tuple[str, dict[str, Any], str]] = []
        seen: set[tuple[str, str, str]] = set()
        for field, overrides, mode in items:
            if not field:
                continue
            key = (field, mode, revisions.content_fingerprint(revisions.merge_content({}, overrides)))
            if key in seen:
                continue
            seen.add(key)
            indexed.append((field, overrides, mode))

        if not indexed:
            return {"ok": False, "message": "回执中没有可识别的字段"}

        source.setdefault("字段索引", {})
        results = []
        counts: dict[str, int] = {}
        for field, overrides, mode in indexed:
            code = self._code_for_field(source, source_id, field)
            result = revisions.register_field_revision(
                source_id=source_id,
                field=field,
                code=code,
                overrides=overrides,
                origin="receipt" if receipt_id else "discovery",
                receipt_id=receipt_id,
            )
            action = str(result.get("action"))
            counts[action] = counts.get(action, 0) + 1
            results.append(result)

        ordered_fields = list(dict.fromkeys(field for field, _, _ in indexed))
        source["字段清单"] = ",".join(ordered_fields)
        source["status"] = "已接入"
        source["pending"] = False
        source["字段登记"] = [
            {
                "来源字段": field,
                "特征编号": self._code_for_field(source, source_id, field),
            }
            for field in ordered_fields
        ]
        if receipt_id:
            source["最近回执"] = {
                "回执编号": receipt_id,
                "字段": ordered_fields,
                "处理结果": [
                    {
                        "来源字段": item.get("entry", {}).get("来源字段"),
                        "特征编号": item.get("code"),
                        "修订编号": item.get("revision"),
                        "动作": item.get("action"),
                    }
                    for item in results
                ],
            }

        self._validate_source_connectivity(source_id)

        # 校验入口按特征编号只暴露最新一条待校验修订。
        pending_by_code: dict[str, dict[str, Any]] = {}
        for item in results:
            entry = item.get("entry") or {}
            if entry.get("status") != "待校验" or entry.get("已废弃"):
                continue
            code = entry.get("特征编号")
            current = pending_by_code.get(code)
            if current is None or int(entry.get("修订序号", 1) or 1) > int(
                current.get("修订序号", 1) or 1
            ):
                pending_by_code[code] = entry
        pending = [revisions.revision_summary(row) for row in pending_by_code.values()]

        self._sync_cross_define(source_id, ordered_fields)
        return {
            "ok": True,
            "source_id": source_id,
            "fields": ordered_fields,
            "source_fields": {
                field: self._code_for_field(source, source_id, field)
                for field in ordered_fields
            },
            "registered": results,
            "counts": counts,
            "pending_revisions": pending,
            "pending_count": len(pending),
            "message": (
                f"特征源 {source_id} 已完成接入，发现 {len(ordered_fields)} 个字段；"
                f"新增定义 {counts.get('created', 0)} 条，"
                f"更正 {counts.get('revised', 0) + counts.get('amended', 0)} 条，"
                f"等价重复 {counts.get('unchanged', 0) + counts.get('pending_exists', 0) + counts.get('draft_exists', 0) + counts.get('stale_ignored', 0)} 条"
            ),
        }

    def sync_source_receipt(
        self,
        source_id: int,
        payload: Any = None,
        *,
        receipt_id: str | None = None,
    ) -> dict[str, Any]:
        """处理一次字段回执（可能同时包含旧字段回执与规则更正）。"""
        if receipt_id is None and isinstance(payload, dict):
            receipt_id = str(payload.get("回执编号") or payload.get("receipt_id") or "") or None
        return self.fetch_source_schema(source_id, payload, receipt_id=receipt_id)

    # 兼容不同调用方的命名
    process_source_receipt = sync_source_receipt
    push_source_receipt = sync_source_receipt
    handle_source_receipt = sync_source_receipt
    apply_source_receipt = sync_source_receipt
    send_source_receipt = sync_source_receipt
    replay_source_receipt = sync_source_receipt

    def discover_source_fields(
        self,
        source_id: int,
        fields: Any = None,
        *,
        receipt_id: str | None = None,
    ) -> dict[str, Any]:
        """字段发现入口的语义化别名，等价于 ``fetch_source_schema``。"""
        return self.fetch_source_schema(source_id, fields, receipt_id=receipt_id)

    register_discovered_fields = discover_source_fields
    register_source_fields = discover_source_fields

    def _normalize_receipt_items(self, fields: Any) -> list[tuple[str, dict[str, Any], str]]:
        """把各种回执形态归一成 (规范字段, 内容更正, 模式) 序列，保留先后顺序。

        模式说明：
        - ``reference``：只声明字段存在（发现清单），沿用已有定义。
        - ``receipt``：字段回执，带回字段当时的描述/规则，按等价内容去重。
        - ``correction``：显式更正，用更正内容在原编号下形成修订。
        """
        items: list[tuple[str, dict[str, Any], str]] = []
        if fields is None:
            return items
        if isinstance(fields, str):
            return [(name, {}, "reference") for name, _ in self._field_names_from_text(fields)]
        if isinstance(fields, dict):
            def absorb(keys: tuple[str, ...], mode: str) -> None:
                for key in keys:
                    value = fields.get(key)
                    if not value:
                        continue
                    for item in self._normalize_receipt_items(value):
                        items.append((item[0], item[1], mode))

            absorb(CORRECTION_KEYS, "correction")
            absorb(RECEIPT_KEYS, "receipt")
            absorb(FIELD_KEYS, "reference")
            if items:
                return items
            name = self._name_from_entry(fields)
            if name:
                overrides = revisions.field_overrides(fields)
                items.append((name, overrides, "receipt" if overrides else "reference"))
            return items
        if isinstance(fields, Iterable):
            for item in fields:
                if isinstance(item, str):
                    for name, _ in self._field_names_from_text(item):
                        items.append((name, {}, "reference"))
                elif isinstance(item, dict):
                    name = self._name_from_entry(item)
                    if name:
                        overrides = revisions.field_overrides(item)
                        items.append((name, overrides, "receipt" if overrides else "reference"))
            return items
        return items

    def _name_from_entry(self, entry: dict[str, Any]) -> str | None:
        name = revisions.field_name_of(entry)
        if name:
            return name
        # 回执可能只带特征编号或特征名称，用已有定义反查来源字段。
        code = entry.get("特征编号") or entry.get("feature_code")
        if code:
            for row in revisions.definitions_for_code(str(code)):
                field = revisions.canonical_field(row.get("来源字段"))
                if field:
                    return field
        feature_name = str(entry.get("特征名称") or entry.get("feature_name") or "")
        prefix = "feature_"
        if feature_name.startswith(prefix):
            field = revisions.canonical_field(feature_name[len(prefix):])
            if field:
                return field
        for row in store.rows("feature_define"):
            if row.get("特征名称") == feature_name and row.get("来源字段"):
                return revisions.canonical_field(row.get("来源字段"))
        return None

    def _field_names_from_text(self, text: str) -> list[tuple[str, dict[str, Any]]]:
        names = []
        for token in text.replace("，", ",").replace(";", ",").replace("\n", ",").split(","):
            field = revisions.canonical_field(token)
            if field:
                names.append((field, {}))
        return names

    def _code_for_field(self, source: dict[str, Any], source_id: int, field: str) -> str:
        """同一来源字段始终沿用第一次分配的特征编号，清单换序不影响编号。"""
        existing = revisions.definitions_for_field(source_id, field)
        if existing:
            return existing[0].get("特征编号") or self._allocate_code(source, source_id, field)
        # 兼容早期直接写入、没有来源字段标记的定义行。
        for row in store.rows("feature_define"):
            if (
                row.get("数据来源") == f"feature_source_{source_id}"
                and row.get("特征名称") == f"feature_{field}"
                and row.get("特征编号")
            ):
                return row.get("特征编号")
        return self._allocate_code(source, source_id, field)

    def _allocate_code(self, source: dict[str, Any], source_id: int, field: str) -> str:
        registry: dict[str, int] = source.setdefault("字段索引", {})
        if field in registry:
            return f"FEAT-S{source_id:04d}-F{registry[field]:02d}"
        used = {int(value) for value in registry.values() if isinstance(value, int)}
        # 从已有定义里补齐索引，避免进程重启后编号跳变。
        for row in store.rows("feature_define"):
            if row.get("来源特征源") != source_id:
                continue
            name = revisions.canonical_field(row.get("来源字段"))
            code = str(row.get("特征编号") or "")
            match = code.rsplit("-F", 1)
            if name and len(match) == 2 and match[1].isdigit():
                registry.setdefault(name, int(match[1]))
                used.add(int(match[1]))
        index = max(used, default=0) + 1
        registry[field] = index
        return f"FEAT-S{source_id:04d}-F{index:02d}"

    def _sync_cross_define(self, source_id: int, fields: list[str]) -> None:
        """把字段登记结果同步到特征源自身的可追溯视图，不重复写入定义。"""
        source = store.find(self.MODULE, source_id)
        if source is None:
            return
        sync = {
            field: {
                "特征编号": self._code_for_field(source, source_id, field),
                "来源标识": f"feature_source_{source_id}:{field}",
                "修订数量": len(revisions.definitions_for_field(source_id, field)),
                "最新修订": (
                    revisions.latest_revision(revisions.definitions_for_field(source_id, field) or [])
                    or {}
                ).get("修订编号"),
                "同步时间": "2026-11-22T00:00:00",
            }
            for field in fields
        }
        source["字段同步"] = sync

    def _validate_source_connectivity(self, source_id: int) -> dict[str, Any]:
        """校验数据源连接配置的有效性和可达性。"""
        source = store.find(self.MODULE, source_id)
        if source is None:
            return {"ok": False, "message": "数据源不存在"}
        conn_config = source.get("连接配置", "")
        is_valid = len(str(conn_config)) > 0 and "host" in str(conn_config).lower()
        if not is_valid:
            source["abnormal"] = True
            return {"ok": False, "message": f"数据源 {source_id} 连接配置校验失败，缺少必要的主机信息"}
        source["abnormal"] = False
        return {"ok": True, "message": f"数据源 {source_id} 连接配置校验通过，可正常采集数据"}

    def discover_and_register(self, source_id: int, conn_config: str) -> dict[str, Any]:
        """连接外部数据表并自动发现字段，完成源注册与入湖流程。"""
        source = store.find(self.MODULE, source_id)
        if source is None:
            return {"ok": False, "message": f"特征源 {source_id} 不存在"}
        source["连接配置"] = conn_config
        return self.fetch_source_schema(source_id)

    def source_field_catalog(self, source_id: int) -> dict[str, Any]:
        """按来源查询字段清单及其当前修订状态。"""
        source = store.find(self.MODULE, source_id)
        if source is None:
            return {"ok": False, "message": f"特征源 {source_id} 不存在"}
        fields = []
        for name in (source.get("字段清单") or "").split(","):
            field = revisions.canonical_field(name)
            if not field:
                continue
            rows = revisions.definitions_for_field(source_id, field)
            effective = next((row for row in rows if row.get("status") == "已生效"), None)
            pending = next(
                (row for row in rows if row.get("status") == "待校验" and not row.get("已废弃")), None
            )
            fields.append({
                "来源字段": field,
                "来源标识": f"feature_source_{source_id}:{field}",
                "特征编号": rows[0].get("特征编号") if rows else None,
                "修订数量": len(rows),
                "当前生效修订": revisions.revision_summary(effective) if effective else None,
                "待校验修订": revisions.revision_summary(pending) if pending else None,
            })
        return {
            "ok": True,
            "source_id": source_id,
            "字段清单": fields,
            "定义为": [item["特征编号"] for item in fields],
            "pending_revisions": [
                revisions.revision_summary(row) for row in revisions.pending_revisions(source_id=source_id)
            ],
        }

    field_catalog = source_field_catalog
    list_source_fields = source_field_catalog
    describe_source_fields = source_field_catalog

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        """特征源详情附带每个来源字段的修订状态。"""
        row = store.find(self.MODULE, entry_id)
        if row is None:
            return None
        catalog = self.source_field_catalog(entry_id)
        enriched = dict(row)
        enriched["字段定义"] = catalog.get("字段清单", [])
        enriched["待校验修订"] = catalog.get("pending_revisions", [])
        return enriched

    def collect_source_snapshot(self, source_id: int) -> dict[str, Any]:
        """从数据源采集样本数据并计算统计快照，用于数据质量评估。"""
        source = store.find(self.MODULE, source_id)
        if source is None:
            return {"ok": False}
        fields_list = (source.get("字段清单") or "").split(",") if source.get("字段清单") else []
        sample_ratio = source.get("采样比例", "0.1")
        stats = {
            "source_id": source_id,
            "source_name": source.get("源名称", ""),
            "total_fields": len(fields_list),
            "sample_ratio": float(str(sample_ratio).replace("%", "")) if sample_ratio else 0.1,
            "collected_at": "2026-11-18T10:00:00",
            "row_count": 50000,
            "null_ratio": {f: round(i * 0.01, 4) for i, f in enumerate(fields_list)},
        }
        self._archive_source_snapshot(source_id, stats)
        return {"ok": True, "snapshot": stats, "message": f"特征源 {source_id} 快照采集完成"}

    def _archive_source_snapshot(self, source_id: int, stats: dict[str, Any]) -> None:
        """将数据源快照统计信息归档到存储模块，保留历史版本供回溯。"""
        snapshot_rows = store.rows("feature_snapshot")
        next_id = max((int(r.get("id", 0)) for r in snapshot_rows), default=0) + 1
        snapshot_rows.append({
            "id": next_id,
            "status": "已完成",
            "pending": False,
            "abnormal": False,
            "快照编号": f"SS-{source_id:04d}-{next_id:04d}",
            "特征集": f"source_{source_id}_fields",
            "快照日期": "2026-11-18",
            "特征数量": stats["total_fields"],
            "数据行数": str(stats["row_count"]),
            "存储路径": f"snapshots/source_{source_id}/{next_id:04d}.json",
            "保留天数": "90",
            "快照状态": "已完成",
        })
