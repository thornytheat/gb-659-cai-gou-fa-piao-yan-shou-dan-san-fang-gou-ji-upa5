"""同票多源补报的整票候选与裁决。

- 同一票（doc_type + doc_no）的每个来源整票留作候选，按内容摘要去重。
- 裁决只接受现存整票候选的摘要，禁止拼接各来源的行。
- 旧值重放只更新候选见闻，不翻案；新内容使争议重新待审并暂停勾稽。
- 裁决先记录后落账，中断后按同一裁决恢复，重试不重复采信。
"""
import json
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from hashlib import sha256

from app import store
from app.errors import DomainError
from .money import decimal_value, normalize_rate

try:
    from zoneinfo import ZoneInfo
    from app.config import TIMEZONE
    _TZ = ZoneInfo(TIMEZONE)
except Exception:  # 无 tzdata 时退回固定 +08:00
    _TZ = timezone(timedelta(hours=8))

LINE_FIELDS = ("sku", "qty", "amount", "tax_rate")

STATUS_CLEAR = "CLEAR"          # 单一候选，自动生效
STATUS_DISPUTED = "DISPUTED"    # 多候选未决，暂停勾稽
STATUS_DECIDED = "DECIDED"      # 已裁决落账

ADJUDICATION_REASONS = {
    "ORIGINAL_SUPPLIER": "按供应商原件",
    "ORIGINAL_BUYER": "按采购原件",
    "CORRECTED_RESEND": "按更正重发件",
    "FINANCE_VERIFIED": "按财务核实件",
}


def _now() -> str:
    return datetime.now(_TZ).isoformat(timespec="seconds")


def canonical_ticket(doc_type, doc_no, lines) -> dict:
    """整票规范化：字段保形、金额 Decimal 化、行按稳定键排序。"""
    kind = str(doc_type or "").strip().upper()
    number = str(doc_no or "").strip()
    if not kind or not number:
        raise DomainError("EMPTY_VALUE", "单据类型与单号不能为空")
    if not lines:
        raise DomainError("EMPTY_VALUE", "整票至少一行")
    norm = [_canonical_line(index, line) for index, line in enumerate(lines, 1)]
    norm.sort(key=lambda line: (line["sku"], line["tax_rate"], line["qty"], line["amount"]))
    return {"doc_type": kind, "doc_no": number, "lines": norm}


def _canonical_line(index: int, line) -> dict:
    line = line if isinstance(line, dict) else {}
    missing = [field for field in LINE_FIELDS if not str(line.get(field, "")).strip()]
    if missing:
        raise DomainError("EMPTY_VALUE", f"第 {index} 行缺少必填值: {','.join(missing)}")
    try:
        qty = decimal_value(str(line["qty"]))
        amount = decimal_value(str(line["amount"]))
        rate = normalize_rate(str(line["tax_rate"]))
    except (ValueError, InvalidOperation) as exc:
        raise DomainError("BAD_AMOUNT", f"第 {index} 行数量、金额或税率无法解析") from exc
    return {"sku": str(line["sku"]).strip(), "qty": _canonical_decimal(qty), "amount": _canonical_amount(amount), "tax_rate": _canonical_decimal(rate)}


def _canonical_amount(value: Decimal) -> str:
    """金额规范形：半入到分，保证同值不同写法摘要一致。"""
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _canonical_decimal(value: Decimal) -> str:
    """数量与税率规范形：去尾零、去指数，不舍入有效位。"""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def ticket_key(canonical: dict) -> str:
    return f"{canonical['doc_type']}:{canonical['doc_no']}"


def content_digest(canonical: dict) -> str:
    blob = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(blob.encode("utf-8")).hexdigest()


def total_amount(canonical: dict) -> str:
    return str(sum((Decimal(line["amount"]) for line in canonical["lines"]), Decimal("0")))


def import_report(source, doc_type, doc_no, lines, now=None) -> dict:
    """导入一个来源的整票补报，返回该票的处置结果。"""
    store.init_db()
    source = str(source or "").strip()
    if not source:
        raise DomainError("EMPTY_VALUE", "来源不能为空")
    canonical = canonical_ticket(doc_type, doc_no, lines)
    digest = content_digest(canonical)
    key = ticket_key(canonical)
    seen_at = now or _now()
    with store.immediate() as db:
        row = db.execute("SELECT * FROM ticket_candidates WHERE ticket_key=? AND digest=?", (key, digest)).fetchone()
        if row:
            # 旧值重放：只更新见闻，不触碰状态，不翻案。
            sources = json.loads(row["sources_json"])
            if source not in sources:
                sources.append(source)
            db.execute(
                "UPDATE ticket_candidates SET last_seen=?, seen_count=seen_count+1, sources_json=? WHERE ticket_key=? AND digest=?",
                (seen_at, json.dumps(sources, ensure_ascii=False), key, digest),
            )
            state = _state(db, key)
            return {"ticket_key": key, "digest": digest, "outcome": "REPLAYED", "status": state["status"], "version": state["version"], "seen_count": row["seen_count"] + 1}
        db.execute(
            "INSERT INTO ticket_candidates VALUES (?,?,?,?,?,?,1)",
            (key, digest, _dump(canonical), json.dumps([source], ensure_ascii=False), seen_at, seen_at),
        )
        state = _state(db, key)
        if state is None:
            db.execute("INSERT INTO ticket_state VALUES (?,?,?,?)", (key, 1, STATUS_CLEAR, digest))
            _post_ledger(db, key, digest, "AUTO", None, canonical, seen_at)
            return {"ticket_key": key, "digest": digest, "outcome": "AUTO_EFFECTIVE", "status": STATUS_CLEAR, "version": 1, "seen_count": 1}
        # 新内容：争议（重）开，暂停该票勾稽，等待裁决。
        version = state["version"] + 1
        db.execute("UPDATE ticket_state SET version=?, status=?, effective_digest=NULL WHERE ticket_key=?", (version, STATUS_DISPUTED, key))
        return {"ticket_key": key, "digest": digest, "outcome": "DISPUTED", "status": STATUS_DISPUTED, "version": version, "seen_count": 1}


def decide_adjudication(ticket_key_value, digest, reason_code, base_version, adjudication_id, reason_text="", now=None) -> dict:
    """裁决第一阶段：记录裁决意向。并发下拒绝过期候选与重复裁决。"""
    store.init_db()
    if reason_code not in ADJUDICATION_REASONS:
        raise DomainError("BAD_REASON", "裁决理由必须引用有效原件原因码")
    adjudication_id = str(adjudication_id or "").strip()
    if not adjudication_id:
        raise DomainError("EMPTY_VALUE", "裁决标识不能为空")
    decided_at = now or _now()
    with store.immediate() as db:
        existing = _adjudication(db, adjudication_id)
        if existing:
            _ensure_same_request(existing, ticket_key_value, digest, reason_code, base_version)
            return {**_adjudication_view(existing), "fresh": False}
        state = _state(db, ticket_key_value)
        if state is None:
            raise DomainError("UNKNOWN_TICKET", "票不存在", 404)
        if state["status"] == STATUS_DECIDED:
            raise DomainError("ALREADY_DECIDED", "该票已裁决，新内容才会重开争议", 409)
        if state["status"] != STATUS_DISPUTED:
            raise DomainError("NOT_DISPUTED", "该票无争议，无需裁决", 409)
        if state["version"] != base_version:
            raise DomainError("STALE_VERSION", "候选集已变化，基于过期候选的裁决被拒绝", 409)
        candidate = db.execute("SELECT 1 FROM ticket_candidates WHERE ticket_key=? AND digest=?", (ticket_key_value, digest)).fetchone()
        if candidate is None:
            raise DomainError("UNKNOWN_CANDIDATE", "候选不存在：裁决只接受整票候选，禁止拼接各来源的行", 409)
        prior = db.execute(
            "SELECT adjudication_id FROM adjudications WHERE ticket_key=? AND base_version=? AND outcome != 'VOIDED'",
            (ticket_key_value, base_version),
        ).fetchone()
        if prior:
            raise DomainError("ALREADY_DECIDED", "该候选版本已有裁决，重复裁决被拒绝", 409)
        db.execute(
            "INSERT INTO adjudications VALUES (?,?,?,?,?,?,?,?,NULL)",
            (adjudication_id, ticket_key_value, digest, reason_code, str(reason_text or ""), base_version, "PENDING_POST", decided_at),
        )
        return {**_adjudication_view(_adjudication(db, adjudication_id)), "fresh": True}


def post_adjudication(adjudication_id, now=None) -> dict:
    """裁决第二阶段：落账。可恢复、幂等，落账前再校验候选版本栅栏。"""
    store.init_db()
    posted_at = now or _now()
    with store.immediate() as db:
        adj = _adjudication(db, adjudication_id)
        if adj is None:
            raise DomainError("UNKNOWN_ADJUDICATION", "裁决不存在", 404)
        if adj["outcome"] == "POSTED":
            state = _state(db, adj["ticket_key"])
            return {"adjudication_id": adjudication_id, "outcome": "POSTED", "already_posted": True, "posted_at": adj["posted_at"], "version": state["version"]}
        if adj["outcome"] == "VOIDED":
            raise DomainError("STALE_VERSION", "裁决已因候选过期作废", 409)
        state = _state(db, adj["ticket_key"])
        if state["version"] != adj["base_version"]:
            db.execute("UPDATE adjudications SET outcome='VOIDED' WHERE adjudication_id=?", (adjudication_id,))
            raise DomainError("STALE_VERSION", "落账前候选集已变化，裁决作废", 409)
        candidate = db.execute("SELECT content_json FROM ticket_candidates WHERE ticket_key=? AND digest=?", (adj["ticket_key"], adj["digest"])).fetchone()
        canonical = json.loads(candidate["content_json"])
        _post_ledger(db, adj["ticket_key"], adj["digest"], "ADJUDICATED", adjudication_id, canonical, posted_at)
        db.execute("UPDATE ticket_state SET version=version+1, status=?, effective_digest=? WHERE ticket_key=?", (STATUS_DECIDED, adj["digest"], adj["ticket_key"]))
        db.execute("UPDATE adjudications SET outcome='POSTED', posted_at=? WHERE adjudication_id=?", (posted_at, adjudication_id))
        return {"adjudication_id": adjudication_id, "outcome": "POSTED", "already_posted": False, "posted_at": posted_at, "version": state["version"] + 1}


def adjudicate(ticket_key_value, digest, reason_code, base_version, adjudication_id, reason_text="", now=None) -> dict:
    """裁决并落账；同一 adjudication_id 重试返回同次结果，不重复采信。"""
    decision = decide_adjudication(ticket_key_value, digest, reason_code, base_version, adjudication_id, reason_text, now=now)
    posting = post_adjudication(adjudication_id, now=now)
    return {
        "adjudication_id": adjudication_id,
        "ticket_key": ticket_key_value,
        "digest": digest,
        "reason_code": reason_code,
        "outcome": "POSTED",
        "status": STATUS_DECIDED,
        "version": posting["version"],
        "posted_at": posting["posted_at"],
        "replayed": posting["already_posted"],
        "resumed": not decision["fresh"] and not posting["already_posted"],
    }


def resume_interrupted(now=None) -> list:
    """恢复所有已记录但未落账的裁决（启动时与手动恢复共用）。"""
    store.init_db()
    with store.reading() as db:
        ids = [row[0] for row in db.execute("SELECT adjudication_id FROM adjudications WHERE outcome='PENDING_POST' ORDER BY decided_at")]
    results = []
    for adjudication_id in ids:
        try:
            post_adjudication(adjudication_id, now=now)
            results.append({"adjudication_id": adjudication_id, "resumed": True})
        except DomainError as exc:
            results.append({"adjudication_id": adjudication_id, "resumed": False, "code": exc.code})
    return results


def list_vouchers() -> dict:
    """有效凭据清单；争议未决的票列入 suspended，暂停勾稽。"""
    store.init_db()
    vouchers, suspended = [], []
    with store.reading() as db:
        states = db.execute("SELECT * FROM ticket_state ORDER BY ticket_key").fetchall()
        for state in states:
            if state["status"] == STATUS_DISPUTED:
                suspended.append(state["ticket_key"])
                continue
            candidate = db.execute("SELECT * FROM ticket_candidates WHERE ticket_key=? AND digest=?", (state["ticket_key"], state["effective_digest"])).fetchone()
            vouchers.append(_voucher_view(db, state, candidate))
    return {"vouchers": vouchers, "suspended": suspended}


def list_disputes() -> list:
    """争议未决的票及其全部整票候选。"""
    store.init_db()
    disputes = []
    with store.reading() as db:
        states = db.execute("SELECT * FROM ticket_state WHERE status=? ORDER BY ticket_key", (STATUS_DISPUTED,)).fetchall()
        for state in states:
            candidates = db.execute("SELECT * FROM ticket_candidates WHERE ticket_key=? ORDER BY first_seen, digest", (state["ticket_key"],)).fetchall()
            pending = db.execute("SELECT COUNT(*) FROM adjudications WHERE ticket_key=? AND outcome='PENDING_POST'", (state["ticket_key"],)).fetchone()[0]
            disputes.append({
                "ticket_key": state["ticket_key"],
                "version": state["version"],
                "status": state["status"],
                "pending_posting": bool(pending),
                "candidates": [_candidate_view(row) for row in candidates],
            })
    return disputes


def ledger_entries(ticket_key_value=None) -> list:
    store.init_db()
    with store.reading() as db:
        if ticket_key_value:
            rows = db.execute("SELECT * FROM voucher_ledger WHERE ticket_key=? ORDER BY seq", (ticket_key_value,)).fetchall()
        else:
            rows = db.execute("SELECT * FROM voucher_ledger ORDER BY seq").fetchall()
    return [dict(row) for row in rows]


def _state(db, key):
    return db.execute("SELECT * FROM ticket_state WHERE ticket_key=?", (key,)).fetchone()


def _adjudication(db, adjudication_id):
    return db.execute("SELECT * FROM adjudications WHERE adjudication_id=?", (adjudication_id,)).fetchone()


def _adjudication_view(row) -> dict:
    return {
        "adjudication_id": row["adjudication_id"],
        "ticket_key": row["ticket_key"],
        "digest": row["digest"],
        "reason_code": row["reason_code"],
        "base_version": row["base_version"],
        "outcome": row["outcome"],
    }


def _ensure_same_request(existing, key, digest, reason_code, base_version):
    same = (
        existing["ticket_key"] == key
        and existing["digest"] == digest
        and existing["reason_code"] == reason_code
        and existing["base_version"] == base_version
    )
    if not same:
        raise DomainError("IDEMPOTENCY_CONFLICT", "相同裁决标识提交了不同内容", 409)


def _post_ledger(db, key, digest, kind, adjudication_id, canonical, posted_at):
    db.execute(
        "INSERT INTO voucher_ledger (ticket_key, digest, kind, adjudication_id, content_json, posted_at) VALUES (?,?,?,?,?,?)",
        (key, digest, kind, adjudication_id, _dump(canonical), posted_at),
    )


def _dump(canonical: dict) -> str:
    return json.dumps(canonical, ensure_ascii=False, sort_keys=True)


def _candidate_view(row) -> dict:
    canonical = json.loads(row["content_json"])
    return {
        "digest": row["digest"],
        "sources": json.loads(row["sources_json"]),
        "seen_count": row["seen_count"],
        "first_seen": row["first_seen"],
        "last_seen": row["last_seen"],
        "total_amount": total_amount(canonical),
        "lines": canonical["lines"],
    }


def _voucher_view(db, state, candidate) -> dict:
    view = {
        "ticket_key": state["ticket_key"],
        "status": state["status"],
        "version": state["version"],
        **_candidate_view(candidate),
    }
    view["adjudication"] = None
    if state["status"] == STATUS_DECIDED:
        adj = db.execute(
            "SELECT * FROM adjudications WHERE ticket_key=? AND digest=? AND outcome='POSTED' ORDER BY decided_at DESC LIMIT 1",
            (state["ticket_key"], candidate["digest"]),
        ).fetchone()
        if adj:
            view["adjudication"] = {
                "adjudication_id": adj["adjudication_id"],
                "reason_code": adj["reason_code"],
                "reason_text": adj["reason_text"],
                "posted_at": adj["posted_at"],
            }
    return view
