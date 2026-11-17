from app.errors import DomainError
from .parser import parse_documents
from .money import decimal_value
from . import tickets

SAMPLE = [{"doc_type": "PO", "doc_no": "PO-001", "sku": "000142", "qty": "10", "amount": "1,000.00", "tax_rate": "0.13"}]

def run(rows: list[dict] | None = None) -> dict:
    accepted, quarantined = parse_documents(rows or SAMPLE)
    total = sum((decimal_value(row["amount"]) for row in accepted), start=decimal_value("0"))
    return {"input": len(accepted) + len(quarantined), "accepted": len(accepted), "quarantined": len(quarantined), "amount": str(total), "evidence": quarantined}

def demo():
    return run()

def import_reports(payload: dict | None) -> dict:
    """导入多源整票补报：每来源整票留作候选，新内容触发争议待审。"""
    if not isinstance(payload, dict) or not payload.get("reports"):
        raise DomainError("EMPTY_VALUE", "reports 不能为空")
    results = []
    for index, item in enumerate(payload["reports"], 1):
        if not isinstance(item, dict):
            raise DomainError("BAD_FORMAT", f"第 {index} 条补报格式不正确")
        results.append(tickets.import_report(item.get("source"), item.get("doc_type"), item.get("doc_no"), item.get("lines")))
    return {
        "imported": len(results),
        "auto_effective": sum(1 for row in results if row["outcome"] == "AUTO_EFFECTIVE"),
        "disputed": sum(1 for row in results if row["outcome"] == "DISPUTED"),
        "replayed": sum(1 for row in results if row["outcome"] == "REPLAYED"),
        "results": results,
    }

def adjudicate_ticket(payload: dict | None) -> dict:
    """凭原件理由裁决整票：绑定候选内容，并发拒绝过期候选，重试不重复采信。"""
    if not isinstance(payload, dict):
        raise DomainError("EMPTY_VALUE", "请求体不能为空")
    required = ("ticket_key", "digest", "reason_code", "base_version", "adjudication_id")
    missing = [key for key in required if payload.get(key) in (None, "")]
    if missing:
        raise DomainError("EMPTY_VALUE", f"缺少必填字段: {','.join(missing)}")
    try:
        base_version = int(payload["base_version"])
    except (TypeError, ValueError):
        raise DomainError("BAD_FORMAT", "base_version 必须为整数") from None
    return tickets.adjudicate(
        payload["ticket_key"],
        payload["digest"],
        payload["reason_code"],
        base_version,
        payload["adjudication_id"],
        reason_text=payload.get("reason_text", ""),
    )

def vouchers() -> dict:
    return tickets.list_vouchers()

def disputes() -> dict:
    return {"disputes": tickets.list_disputes(), "reasons": tickets.ADJUDICATION_REASONS}
