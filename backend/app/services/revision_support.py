"""特征字段修订支撑。

这一层把「字段身份」「内容等价」「修订状态」集中起来：
- 字段身份：同一特征源下的规范字段名（大小写、全角、空白不影响身份）。
- 内容等价：同一修订内容经规范化后的指纹，用来识别重复回执和重复更正。
- 修订状态：一个特征编号下只有一条「已生效」修订，旧版被取代后标记为「已废弃」。

特征源服务、特征定义服务和特征计算服务都通过这里读写修订，避免各自维护一套
去重逻辑后出现「清单里有草稿、详情里又查不到」的偏差。
"""
from __future__ import annotations

import ast
import hashlib
import json
import re
from typing import Any

from app.store import store

# 参与内容等价比对的字段。名称、类型、规则、来源、填充、取值范围、描述任一变化
# 都视为一次真实更正；只有完全等价的内容才被判定为重复提交。
CONTENT_FIELDS = [
    "特征名称",
    "特征类型",
    "计算逻辑",
    "数据来源",
    "缺失填充",
    "取值范围",
    "特征描述",
]

_CN_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}

_FULLWIDTH = str.maketrans(
    "０１２３４５６７８９（）＋－＊／×÷，．",
    "0123456789()+-*/x/,.",
)

_OP_NAMES = {
    "+": "add",
    "-": "sub",
    "*": "mul",
    "×": "mul",
    "x": "mul",
    "X": "mul",
    "/": "div",
    "÷": "div",
}

_OP_LABELS = {"add": "加", "sub": "减", "mul": "乘", "div": "除"}


def normalize_text(value: Any) -> str:
    """统一大小写、全角字符和连续空白，供字段身份与内容指纹使用。"""
    text = "" if value is None else str(value)
    text = text.translate(_FULLWIDTH)
    text = re.sub(r"\s+", " ", text.strip())
    return text.casefold()


def canonical_field(value: Any) -> str:
    """规范字段名：同一来源下 ``Age``、`` age ``、``ＡＧＥ`` 都归为 ``age``。"""
    text = "" if value is None else str(value)
    text = text.translate(_FULLWIDTH)
    text = text.strip().strip("`\"'").casefold()
    text = re.sub(r"\s+", "_", text)
    return text


def field_name_of(item: Any) -> str | None:
    """从字符串或字段描述对象中取出规范字段名。"""
    if isinstance(item, str):
        name = canonical_field(item)
        return name or None
    if isinstance(item, dict):
        for key in ("字段", "字段名", "field", "column", "列名", "name"):
            value = item.get(key)
            if value:
                name = canonical_field(value)
                if name:
                    return name
    return None


def field_overrides(item: Any) -> dict[str, Any]:
    """把回执/发现条目里的描述、规则等转换成定义内容字段。"""
    if not isinstance(item, dict):
        return {}
    mapping = {
        "特征名称": ("特征名称", "feature_name"),
        "特征类型": ("特征类型", "数据类型", "type", "dtype"),
        "计算逻辑": ("计算逻辑", "计算规则", "规则", "rule", "logic", "表达式", "expression", "compute_rule"),
        "数据来源": ("数据来源", "data_source"),
        "缺失填充": ("缺失填充", "fill", "missing_fill"),
        "取值范围": ("取值范围", "range", "value_range"),
        "特征描述": ("特征描述", "描述", "说明", "description", "desc"),
    }
    overrides: dict[str, Any] = {}
    for target, aliases in mapping.items():
        for alias in aliases:
            if alias in item and item[alias] is not None:
                value = item[alias]
                if isinstance(value, str) and not value.strip():
                    continue
                overrides[target] = value
                break
    return overrides


def _chinese_number(text: str) -> float | int | None:
    """转换常见的「一」「十二」「二十」等中文数字。"""
    text = text.strip()
    if not text:
        return None
    if "_" in text:
        return None
    if any(ch.isdigit() for ch in text):
        try:
            number = float(text)
            return int(number) if number.is_integer() else number
        except ValueError:
            return None
    if "十" not in text:
        if len(text) == 1 and text in _CN_DIGITS:
            return _CN_DIGITS[text]
        return None
    left, _, right = text.partition("十")
    tens = _CN_DIGITS.get(left, 1) if left else 1
    ones = _CN_DIGITS.get(right, 0) if right else 0
    return tens * 10 + ones


def _number(text: str) -> float | int | None:
    number = _chinese_number(text)
    return number


def parse_rule(rule: Any) -> tuple[str, float | int] | None:
    """把「加一」「*2」「age + 1」等规则解析为（操作, 操作数）。

    解析失败时返回 ``None``，调用方会把规则当作普通文本处理。
    """
    if rule is None:
        return None
    raw = str(rule).translate(_FULLWIDTH)
    compact = re.sub(r"\s+", "", raw).casefold()
    if not compact:
        return None

    # 中英文操作词
    word_patterns = [
        ("add", r"(?:增加|加上|加)([零〇一二两三四五六七八九十0-9.]+)"),
        ("add", r"(?:add(?:ed|s)?|plus)(?:by|with)?([0-9.]+)"),
        ("sub", r"(?:减去|减掉|减)([零〇一二两三四五六七八九十0-9.]+)"),
        ("sub", r"(?:subtract(?:ed|s)?|minus)(?:by|with)?([0-9.]+)"),
        ("mul", r"(?:乘以|乘上|乘)([零〇一二两三四五六七八九十0-9.]+)"),
        ("mul", r"(?:multipl(?:y|ies|ied)|times)(?:by|with)?([0-9.]+)"),
        ("div", r"(?:除以|除)([零〇一二两三四五六七八九十0-9.]+)"),
        ("div", r"divide(?:d)?(?:by)?([0-9.]+)"),
    ]
    for op, pattern in word_patterns:
        match = re.fullmatch(pattern, compact)
        if match:
            number = _number(match.group(1))
            if number is not None:
                return op, number

    # 前缀运算符：+1 / -1 / *2 / ×2 / x2 / ÷2
    match = re.fullmatch(r"([+\-*/×x÷])([0-9.]+)", compact)
    if match:
        op_key, number_text = match.group(1), match.group(2)
        number = _number(number_text)
        if number is not None:
            return _OP_NAMES[op_key], number

    # 变量在前：age+1 / age*2 / x*2
    match = re.fullmatch(r"(?:[a-z_][a-z0-9_]*)([+\-*/×x÷])([0-9.]+)", compact)
    if match:
        op_key, number_text = match.group(1), match.group(2)
        number = _number(number_text)
        if number is not None:
            return _OP_NAMES[op_key], number

    return None


def canonical_rule(rule: Any) -> str:
    """规则指纹：``+1``、``加一``、``age + 1`` 被视为等价内容。"""
    parsed = parse_rule(rule)
    if parsed is not None:
        op, operand = parsed
        return f"{op}:{operand:g}"
    return normalize_text(rule)


def apply_rule(rule: Any, value: Any) -> float | int | None:
    """按规则计算输入值；规则无法识别时返回原值，避免静默算出错误结果。"""
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return value
    parsed = parse_rule(rule)
    if parsed is not None:
        op, operand = parsed
        if op == "add":
            return _clean_number(numeric + operand)
        if op == "sub":
            return _clean_number(numeric - operand)
        if op == "mul":
            return _clean_number(numeric * operand)
        if op == "div":
            if operand == 0:
                return None
            return _clean_number(numeric / operand)
        return _clean_number(numeric)

    expression = _rule_expression(rule, numeric)
    if expression is not None:
        return _clean_number(expression)
    return value


def _clean_number(number: float) -> float | int:
    if float(number).is_integer():
        return int(number)
    return round(float(number), 6)


def _rule_expression(rule: Any, value: float) -> float | None:
    """支持 ``(age + 1) * 2`` 这类带变量的四则表达式。"""
    text = str(rule).translate(_FULLWIDTH)
    text = text.replace("×", "*").replace("÷", "/").replace("−", "-")
    text = re.sub(r"`", "", text).strip()
    if not text:
        return None
    # 只保留算式和变量名；变量统一替换成输入值。
    text = re.sub(r"([A-Za-z_][A-Za-z0-9_]*)", lambda _m: repr(value), text)
    if not re.fullmatch(r"[0-9eE+\-*/%.()\s]*", text):
        return None
    try:
        node = ast.parse(text, mode="eval")
    except SyntaxError:
        return None
    allowed = (
        ast.Expression,
        ast.BinOp,
        ast.UnaryOp,
        ast.Constant,
        ast.Add,
        ast.Sub,
        ast.Mult,
        ast.Div,
        ast.Mod,
        ast.Pow,
        ast.USub,
        ast.UAdd,
        ast.FloorDiv,
    )
    for child in ast.walk(node):
        if not isinstance(child, allowed):
            return None
    try:
        return float(eval(compile(node, "<rule>", "eval"), {"__builtins__": {}}))
    except (ArithmeticError, ValueError, TypeError):
        return None


# ── 定义内容与指纹 ────────────────────────────────────────────────────────

def default_content(source_id: int, field: str, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """为首次发现的字段生成一份完整定义内容。"""
    is_numeric = field in ("age", "income")
    content: dict[str, Any] = {
        "特征名称": f"feature_{field}",
        "特征类型": "numeric" if is_numeric else "category",
        "计算逻辑": f"从特征源 {source_id} 导入字段 {field} 并标准化缺失填充为均值",
        "数据来源": f"feature_source_{source_id}",
        "缺失填充": "mean_fill",
        "取值范围": "[0, 9999]" if is_numeric else "[]",
        "特征描述": f"特征源 {source_id} 的字段 {field}",
    }
    content.update(_clean_overrides(overrides or {}))
    return content


def receipt_content(source_id: int, field: str, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """字段回执本身不带定义时，用回执可推导的默认定义补齐整包内容。"""
    content = default_content(source_id, field)
    content.update(_clean_overrides(overrides or {}))
    return content


def content_of_row(row: dict[str, Any]) -> dict[str, Any]:
    """从定义行中提取参与等价比对的内容。"""
    return {field: row.get(field, "") for field in CONTENT_FIELDS}


def content_fingerprint(content: dict[str, Any]) -> str:
    canonical: dict[str, str] = {}
    for field in CONTENT_FIELDS:
        value = content.get(field)
        if field == "计算逻辑":
            canonical[field] = canonical_rule(value)
        else:
            canonical[field] = normalize_text(value)
    payload = json.dumps(canonical, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _clean_overrides(overrides: dict[str, Any]) -> dict[str, Any]:
    return {
        field: value
        for field, value in overrides.items()
        if field in CONTENT_FIELDS and value is not None
    }


def merge_content(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    content = {field: base.get(field, "") for field in CONTENT_FIELDS}
    content.update(_clean_overrides(overrides))
    return content


# ── 修订集合与状态 ────────────────────────────────────────────────────────

def definitions_for_code(code: str) -> list[dict[str, Any]]:
    return [row for row in store.rows("feature_define") if row.get("特征编号") == code]


def definitions_for_field(source_id: int, field: str) -> list[dict[str, Any]]:
    return [
        row
        for row in store.rows("feature_define")
        if int(row.get("来源特征源", -1) or -1) == int(source_id)
        and canonical_field(row.get("来源字段")) == field
    ]


def latest_revision(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not rows:
        return None
    return max(rows, key=lambda row: int(row.get("修订序号", 1) or 1))


def effective_revision(code: str) -> dict[str, Any] | None:
    rows = [
        row
        for row in definitions_for_code(code)
        if row.get("status") == "已生效" and not row.get("已废弃")
    ]
    return max(rows, key=lambda row: int(row.get("修订序号", 1) or 1)) if rows else None


def revision_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "特征编号": row.get("特征编号"),
        "修订编号": row.get("修订编号"),
        "特征版本": row.get("特征版本"),
        "修订序号": row.get("修订序号"),
        "status": row.get("status"),
        "状态": row.get("status"),
        "计算逻辑": row.get("计算逻辑"),
        "特征类型": row.get("特征类型"),
        "特征名称": row.get("特征名称"),
        "来源特征源": row.get("来源特征源"),
        "来源字段": row.get("来源字段"),
        "来源标识": row.get("来源标识"),
        "是否当前生效": row.get("status") == "已生效",
        "_内容指纹": row.get("_内容指纹"),
    }


def _match_existing(
    rows: list[dict[str, Any]],
    overrides: dict[str, Any],
    full_content: bool,
) -> dict[str, Any] | None:
    """按内容等价匹配已有修订。

    对每条已有修订分别叠加本次变更再比对，因此迟到的旧回执即使当前生效版已经
    变过，也能准确命中它当初对应的旧修订，不会被当成一次新更正。
    """
    # 已生效优先：没有实质变更的回执应落到当前版本，而不是被旧版本吸走。
    ordered = sorted(
        rows,
        key=lambda row: (
            0 if row.get("status") == "已生效" else 1 if row.get("status") == "待校验" else 2,
            -int(row.get("修订序号", 1) or 1),
        ),
    )
    for row in ordered:
        if full_content:
            candidate = merge_content(row, overrides)
        else:
            candidate = merge_content(content_of_row(row), overrides)
        if content_fingerprint(candidate) == row.get("_内容指纹"):
            return row
    return None


def _revision_code(source_id: int, field: str, index: int) -> str:
    return f"FEAT-S{source_id:04d}-F{index:02d}"


def _next_revision_index(rows: list[dict[str, Any]]) -> int:
    return max((int(row.get("修订序号", 1) or 1) for row in rows), default=0) + 1


def _new_definition_row(
    *,
    code: str,
    source_id: int | None,
    field: str | None,
    content: dict[str, Any],
    revision_index: int,
    status: str,
    supersedes: dict[str, Any] | None = None,
    origin: str = "discovery",
    receipt_id: str | None = None,
) -> dict[str, Any]:
    rows = store.rows("feature_define")
    entry_id = max((int(row.get("id", 0) or 0) for row in rows), default=0) + 1
    revision_no = f"{code}-R{revision_index:02d}"
    row: dict[str, Any] = {
        "id": entry_id,
        "status": status,
        "pending": status in ("草稿", "待校验"),
        "abnormal": False,
        "特征编号": code,
        "特征名称": content.get("特征名称"),
        "特征类型": content.get("特征类型"),
        "计算逻辑": content.get("计算逻辑"),
        "数据来源": content.get("数据来源"),
        "缺失填充": content.get("缺失填充"),
        "取值范围": content.get("取值范围"),
        "特征版本": f"v{revision_index}.0.0",
        "特征描述": content.get("特征描述"),
        "来源特征源": source_id,
        "来源字段": field,
        "来源标识": f"feature_source_{source_id}:{field}" if source_id is not None and field else None,
        "修订编号": revision_no,
        "修订序号": revision_index,
        "修订数量": 1,
        "取代修订": supersedes.get("修订编号") if supersedes else None,
        "_内容指纹": content_fingerprint(content),
        "修订记录": [
            {
                "序号": 1,
                "修订编号": revision_no,
                "变更字段": sorted(content),
                "内容指纹": content_fingerprint(content),
                "来源": origin,
                "回执编号": receipt_id,
                "记录时间": "2026-11-22T00:00:00",
            }
        ],
        "更新时间": "2026-11-22T00:00:00",
    }
    rows.append(row)
    return row


def append_revision_log(
    entry: dict[str, Any],
    *,
    origin: str,
    receipt_id: str | None,
    changed: list[str] | None = None,
) -> dict[str, Any]:
    """给同一条待校验修订追加一条可追溯记录，不额外生成草稿。"""
    log = entry.setdefault("修订记录", [])
    record = {
        "序号": len(log) + 1,
        "修订编号": entry.get("修订编号"),
        "变更字段": sorted(changed) if changed else [],
        "内容指纹": entry.get("_内容指纹"),
        "来源": origin,
        "回执编号": receipt_id,
        "记录时间": "2026-11-22T00:00:00",
    }
    log.append(record)
    entry["修订数量"] = len(log)
    return record


def _changed_fields(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    changed = []
    for field in CONTENT_FIELDS:
        if content_fingerprint({field: before.get(field)}) != content_fingerprint({field: after.get(field)}):
            changed.append(field)
    return changed


def _apply_content(row: dict[str, Any], content: dict[str, Any]) -> None:
    for field in CONTENT_FIELDS:
        row[field] = content.get(field, row.get(field))
    row["_内容指纹"] = content_fingerprint(content)
    row["更新时间"] = "2026-11-22T00:00:00"


def register_field_revision(
    *,
    source_id: int | None,
    field: str | None,
    code: str,
    overrides: dict[str, Any] | None = None,
    origin: str = "discovery",
    receipt_id: str | None = None,
    full_content: bool = False,
) -> dict[str, Any]:
    """登记一次字段内容，保证「同一来源 + 同一字段 + 等价内容」只登记一次。

    - 无历史修订：建立 v1 草稿。
    - 内容与任一历史修订等价：原样返回，不新增草稿、不改变当前生效版本。
    - 已有未生效的最新修订：就地更正，校验入口始终只有一个待校验修订。
    - 已有生效修订且内容真实变化：在同一特征编号下建立待校验修订，
      旧生效版保持生效，直到新修订校验通过。

    ``full_content=True`` 表示回执自带整包字段内容（例如 ``feature_age`` 的
    规则回执），此时回执字段就是该修订的全部内容，避免同一来源字段的
    描述残留在另一来源字段的规则上。
    """
    overrides = _clean_overrides(overrides or {})
    rows = definitions_for_code(code)
    if not rows and source_id is not None and field is not None:
        rows = definitions_for_field(source_id, field)
    if rows and not field:
        field = rows[0].get("来源字段")
    if rows and source_id is None:
        source_id = rows[0].get("来源特征源")

    matched = _match_existing(rows, overrides, full_content) if rows else None
    if matched is not None:
        status = matched.get("status")
        action = {
            "已生效": "unchanged",
            "待校验": "pending_exists",
            "草稿": "draft_exists",
            "已废弃": "stale_ignored",
        }.get(status, "unchanged")
        return {
            "action": action,
            "created": False,
            "entry": matched,
            "code": code,
            "revision": matched.get("修订编号"),
            "origin": origin,
        }

    if not rows:
        content = receipt_content(source_id or 0, field or code, overrides)
        row = _new_definition_row(
            code=code,
            source_id=source_id,
            field=field,
            content=content,
            revision_index=1,
            status="草稿",
            origin=origin,
            receipt_id=receipt_id,
        )
        return {
            "action": "created",
            "created": True,
            "entry": row,
            "code": code,
            "revision": row["修订编号"],
            "origin": origin,
        }

    latest = latest_revision(rows)
    if latest is not None and latest.get("status") in ("草稿", "待校验") and not latest.get("已废弃"):
        before = content_of_row(latest)
        content = merge_content(before, overrides)
        changed = _changed_fields(before, content)
        _apply_content(latest, content)
        append_revision_log(latest, origin=origin, receipt_id=receipt_id, changed=changed)
        return {
            "action": "amended",
            "created": False,
            "entry": latest,
            "code": code,
            "revision": latest.get("修订编号"),
            "origin": origin,
        }

    current = effective_revision(code) or latest
    content = merge_content(content_of_row(current), overrides)
    row = _new_definition_row(
        code=code,
        source_id=source_id if source_id is not None else current.get("来源特征源"),
        field=field or current.get("来源字段"),
        content=content,
        revision_index=_next_revision_index(rows),
        status="待校验",
        supersedes=current,
        origin=origin,
        receipt_id=receipt_id,
    )
    return {
        "action": "revised",
        "created": True,
        "entry": row,
        "code": code,
        "revision": row["修订编号"],
        "origin": origin,
    }


def activate_revision(row: dict[str, Any]) -> tuple[bool, str]:
    """让一条修订生效；同一特征编号下旧的生效版自动标记为已废弃。"""
    if row.get("status") == "已废弃" or row.get("已废弃"):
        return False, "已废弃修订不可重新生效"
    code = row.get("特征编号")
    for other in definitions_for_code(code):
        if other is row:
            continue
        if other.get("status") == "已生效":
            other["status"] = "已废弃"
            other["pending"] = False
            other["abnormal"] = False
            other["已废弃"] = True
            other["被取代于"] = row.get("修订编号")
            other["被取代时间"] = "2026-11-22T00:00:00"
    row["status"] = "已生效"
    row["pending"] = False
    row["abnormal"] = False
    row["已废弃"] = False
    row["生效时间"] = "2026-11-22T00:00:00"
    return True, "修订已生效"


def definition_detail(identifier: Any) -> dict[str, Any] | None:
    """按修订行 id 或特征编号返回来源、全部修订和当前生效版本。"""
    row = None
    if isinstance(identifier, dict):
        identifier = identifier.get("id") or identifier.get("特征编号")
    if isinstance(identifier, int) or (isinstance(identifier, str) and identifier.isdigit()):
        row = store.find("feature_define", int(identifier))
        if row is not None:
            code = row.get("特征编号")
        else:
            code = str(identifier)
    else:
        code = str(identifier)
    rows = definitions_for_code(code)
    if not rows:
        return None
    rows = sorted(rows, key=lambda item: int(item.get("修订序号", 1) or 1))
    current = effective_revision(code)
    pending = [item for item in rows if item.get("status") == "待校验"]
    latest = rows[-1]
    return {
        "ok": True,
        "特征编号": code,
        "特征名称": latest.get("特征名称"),
        "来源特征源": latest.get("来源特征源"),
        "来源字段": latest.get("来源字段"),
        "来源标识": latest.get("来源标识"),
        "修订数量": len(rows),
        "当前生效修订": revision_summary(current) if current else None,
        "待校验修订": [revision_summary(item) for item in pending],
        "修订": [revision_summary(item) for item in rows],
    }


def pending_revisions(
    *,
    source_id: int | None = None,
    field: str | None = None,
    include_drafts: bool = False,
) -> list[dict[str, Any]]:
    statuses = {"待校验", "草稿"} if include_drafts else {"待校验"}
    rows = [
        row
        for row in store.rows("feature_define")
        if row.get("status") in statuses and not row.get("已废弃")
    ]
    if source_id is not None:
        rows = [row for row in rows if int(row.get("来源特征源", -1) or -1) == int(source_id)]
    if field:
        canonical = canonical_field(field)
        rows = [row for row in rows if canonical_field(row.get("来源字段")) == canonical]
    # 同一特征编号只保留最新一条：历史草稿不能残留在校验入口。
    newest: dict[str, dict[str, Any]] = {}
    for row in rows:
        code = str(row.get("特征编号") or row.get("id"))
        current = newest.get(code)
        if current is None or int(row.get("修订序号", 1) or 1) > int(
            current.get("修订序号", 1) or 1
        ):
            newest[code] = row
    return sorted(newest.values(), key=lambda row: int(row.get("id", 0) or 0))


def definition_code_for_identifier(identifier: Any) -> str | None:
    if isinstance(identifier, dict):
        identifier = identifier.get("id") or identifier.get("特征编号")
    if isinstance(identifier, int) or (isinstance(identifier, str) and identifier.isdigit()):
        row = store.find("feature_define", int(identifier))
        return row.get("特征编号") if row else None
    return str(identifier)


def append_compute_history(
    task: dict[str, Any],
    *,
    action: str,
    input_value: Any = None,
    result: Any = None,
    replayed_from: int | None = None,
    replayed_to: int | None = None,
) -> dict[str, Any]:
    """记录一次计算下发/执行/重放，保留任务当时绑定的修订和来源字段。"""
    entries = store.rows("feature_compute_history")
    record = {
        "记录编号": f"HIST-{len(entries) + 1:04d}",
        "任务id": task.get("id"),
        "任务编号": task.get("任务编号"),
        "动作": action,
        "特征编号": task.get("特征编号"),
        "来源特征源": task.get("来源特征源"),
        "来源字段": task.get("来源字段"),
        "来源标识": task.get("来源标识"),
        "绑定修订": task.get("绑定修订"),
        "绑定特征版本": task.get("绑定特征版本"),
        "计算规则": task.get("计算规则"),
        "输入值": input_value if input_value is not None else task.get("输入值"),
        "计算结果": result if result is not None else task.get("计算结果"),
        "重放自": replayed_from if replayed_from is not None else task.get("重放自"),
        "重放目标任务": replayed_to if replayed_to is not None else task.get("重放目标任务"),
        "记录时间": "2026-11-22T00:00:00",
    }
    entries.append(record)
    return record


__all__ = [
    "CONTENT_FIELDS",
    "activate_revision",
    "append_compute_history",
    "apply_rule",
    "canonical_field",
    "canonical_rule",
    "content_fingerprint",
    "content_of_row",
    "default_content",
    "definition_code_for_identifier",
    "definition_detail",
    "definitions_for_code",
    "definitions_for_field",
    "effective_revision",
    "field_name_of",
    "field_overrides",
    "latest_revision",
    "merge_content",
    "normalize_text",
    "parse_rule",
    "pending_revisions",
    "register_field_revision",
    "revision_summary",
]
