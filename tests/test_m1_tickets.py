import pytest
from fastapi.testclient import TestClient

from app import store
from app.errors import DomainError
from app.main import app
from app.modules.m1_documents import tickets

OLD_LINES = [{"sku": "000142", "qty": "10", "amount": "1,000.00", "tax_rate": "13%"}]
NEW_LINES = [{"sku": "000142", "qty": "10", "amount": "880.00", "tax_rate": "13%"}]
THIRD_LINES = [{"sku": "000142", "qty": "9", "amount": "792.00", "tax_rate": "13%"}]
KEY = "IV:IV-8801"


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setattr("app.store.DATABASE_PATH", tmp_path / "tickets.sqlite3")
    store.init_db()
    yield


def import_old():
    return tickets.import_report("erp", "IV", "IV-8801", OLD_LINES)


def import_new():
    return tickets.import_report("supplier-portal", "IV", "IV-8801", NEW_LINES)


def digest_of(lines):
    return tickets.content_digest(tickets.canonical_ticket("IV", "IV-8801", lines))


def test_first_report_auto_effective(db):
    result = import_old()
    assert result["outcome"] == "AUTO_EFFECTIVE" and result["version"] == 1
    vouchers = tickets.list_vouchers()
    assert [v["ticket_key"] for v in vouchers["vouchers"]] == [KEY]
    assert vouchers["vouchers"][0]["total_amount"] == "1000.00"
    assert vouchers["suspended"] == []


def test_conflicting_resend_disputes_and_suspends(db):
    import_old()
    result = import_new()
    assert result["outcome"] == "DISPUTED" and result["version"] == 2
    vouchers = tickets.list_vouchers()
    assert vouchers["vouchers"] == [] and vouchers["suspended"] == [KEY]  # 争议未决暂停勾稽
    dispute = tickets.list_disputes()[0]
    assert dispute["version"] == 2 and len(dispute["candidates"]) == 2


def test_identical_resend_is_replay_without_state_change(db):
    import_old()
    again = tickets.import_report("erp", "IV", "IV-8801", [{"sku": "000142", "qty": "10.0", "amount": "1000.00", "tax_rate": "0.13"}])
    assert again["outcome"] == "REPLAYED" and again["seen_count"] == 2
    third = tickets.import_report("supplier-portal", "IV", "IV-8801", OLD_LINES)
    assert third["outcome"] == "REPLAYED"
    candidate = tickets.list_vouchers()["vouchers"][0]
    assert candidate["sources"] == ["erp", "supplier-portal"] and candidate["status"] == "CLEAR"


def test_adjudication_binds_whole_candidate_content(db):
    import_old(); import_new()
    result = tickets.adjudicate(KEY, digest_of(NEW_LINES), "CORRECTED_RESEND", 2, "adj-1", reason_text="供应商更正函")
    assert result["outcome"] == "POSTED" and result["version"] == 3
    voucher = tickets.list_vouchers()["vouchers"][0]
    assert voucher["digest"] == digest_of(NEW_LINES)
    assert voucher["lines"] == tickets.canonical_ticket("IV", "IV-8801", NEW_LINES)["lines"]
    assert voucher["adjudication"]["adjudication_id"] == "adj-1"
    assert voucher["adjudication"]["reason_code"] == "CORRECTED_RESEND"


def test_old_value_replay_does_not_overturn(db):
    import_old(); import_new()
    tickets.adjudicate(KEY, digest_of(NEW_LINES), "CORRECTED_RESEND", 2, "adj-1")
    replay = import_old()  # 原来源重发旧值
    assert replay["outcome"] == "REPLAYED"
    voucher = tickets.list_vouchers()["vouchers"][0]
    assert voucher["digest"] == digest_of(NEW_LINES) and voucher["status"] == "DECIDED"


def test_new_content_reopens_dispute_and_suspends(db):
    import_old(); import_new()
    tickets.adjudicate(KEY, digest_of(NEW_LINES), "CORRECTED_RESEND", 2, "adj-1")
    reopened = tickets.import_report("erp", "IV", "IV-8801", THIRD_LINES)
    assert reopened["outcome"] == "DISPUTED" and reopened["version"] == 4
    vouchers = tickets.list_vouchers()
    assert vouchers["vouchers"] == [] and vouchers["suspended"] == [KEY]  # 新内容重新待审，暂停勾稽
    result = tickets.adjudicate(KEY, digest_of(THIRD_LINES), "FINANCE_VERIFIED", 4, "adj-2")
    assert result["outcome"] == "POSTED"
    assert tickets.list_vouchers()["vouchers"][0]["total_amount"] == "792.00"


def test_spliced_lines_are_not_adjudicatable(db):
    import_old(); import_new()
    spliced = tickets.canonical_ticket("IV", "IV-8801", OLD_LINES + [{"sku": "000143", "qty": "1", "amount": "1.00", "tax_rate": "13%"}])
    fake_digest = tickets.content_digest(spliced)
    with pytest.raises(DomainError) as exc:
        tickets.adjudicate(KEY, fake_digest, "CORRECTED_RESEND", 2, "adj-x")
    assert exc.value.code == "UNKNOWN_CANDIDATE"  # 禁止拼接各来源的行


def test_stale_base_version_rejected(db):
    import_old(); import_new()
    with pytest.raises(DomainError) as exc:
        tickets.adjudicate(KEY, digest_of(NEW_LINES), "CORRECTED_RESEND", 1, "adj-1")
    assert exc.value.code == "STALE_VERSION" and exc.value.status == 409


def test_concurrent_adjudication_second_rejected(db):
    import_old(); import_new()
    tickets.decide_adjudication(KEY, digest_of(NEW_LINES), "CORRECTED_RESEND", 2, "adj-1")
    with pytest.raises(DomainError) as exc:
        tickets.decide_adjudication(KEY, digest_of(OLD_LINES), "ORIGINAL_SUPPLIER", 2, "adj-2")
    assert exc.value.code == "ALREADY_DECIDED"  # 并发裁决拒绝过期候选


def test_retry_same_adjudication_id_replays_without_double_post(db):
    import_old(); import_new()
    first = tickets.adjudicate(KEY, digest_of(NEW_LINES), "CORRECTED_RESEND", 2, "adj-1")
    second = tickets.adjudicate(KEY, digest_of(NEW_LINES), "CORRECTED_RESEND", 2, "adj-1")
    assert first["replayed"] is False and second["replayed"] is True
    posted = [e for e in tickets.ledger_entries(KEY) if e["kind"] == "ADJUDICATED"]
    assert len(posted) == 1  # 重试不重复采信


def test_idempotency_key_conflict_rejected(db):
    import_old(); import_new()
    tickets.adjudicate(KEY, digest_of(NEW_LINES), "CORRECTED_RESEND", 2, "adj-1")
    with pytest.raises(DomainError) as exc:
        tickets.adjudicate(KEY, digest_of(OLD_LINES), "ORIGINAL_SUPPLIER", 2, "adj-1")
    assert exc.value.code == "IDEMPOTENCY_CONFLICT"


def test_interrupted_posting_resumes_same_adjudication(db):
    import_old(); import_new()
    tickets.decide_adjudication(KEY, digest_of(NEW_LINES), "CORRECTED_RESEND", 2, "adj-1")
    # 模拟落账前中断：票仍暂停勾稽，账上无裁决记录
    assert tickets.list_vouchers()["suspended"] == [KEY]
    assert [e for e in tickets.ledger_entries(KEY) if e["kind"] == "ADJUDICATED"] == []
    resumed = tickets.resume_interrupted()
    assert resumed == [{"adjudication_id": "adj-1", "resumed": True}]
    voucher = tickets.list_vouchers()["vouchers"][0]
    assert voucher["digest"] == digest_of(NEW_LINES) and voucher["adjudication"]["adjudication_id"] == "adj-1"
    again = tickets.adjudicate(KEY, digest_of(NEW_LINES), "CORRECTED_RESEND", 2, "adj-1")
    assert again["replayed"] is True
    assert len([e for e in tickets.ledger_entries(KEY) if e["kind"] == "ADJUDICATED"]) == 1


def test_new_candidate_between_decide_and_post_voids(db):
    import_old(); import_new()
    tickets.decide_adjudication(KEY, digest_of(NEW_LINES), "CORRECTED_RESEND", 2, "adj-1")
    tickets.import_report("erp", "IV", "IV-8801", THIRD_LINES)  # 候选集变化
    with pytest.raises(DomainError) as exc:
        tickets.post_adjudication("adj-1")
    assert exc.value.code == "STALE_VERSION"
    assert tickets.list_vouchers()["suspended"] == [KEY]
    result = tickets.adjudicate(KEY, digest_of(THIRD_LINES), "FINANCE_VERIFIED", 3, "adj-2")
    assert result["outcome"] == "POSTED"


def test_adjudicate_requires_dispute_and_reason(db):
    import_old()
    with pytest.raises(DomainError) as exc:
        tickets.adjudicate(KEY, digest_of(OLD_LINES), "CORRECTED_RESEND", 1, "adj-1")
    assert exc.value.code == "NOT_DISPUTED"
    import_new()
    with pytest.raises(DomainError) as exc:
        tickets.adjudicate(KEY, digest_of(NEW_LINES), "GUESS", 2, "adj-2")
    assert exc.value.code == "BAD_REASON"  # 必须凭原件理由裁决


def test_unknown_ticket_rejected(db):
    with pytest.raises(DomainError) as exc:
        tickets.adjudicate("IV:IV-0000", "0" * 64, "CORRECTED_RESEND", 1, "adj-1")
    assert exc.value.code == "UNKNOWN_TICKET" and exc.value.status == 404


def test_invalid_report_rejected(db):
    with pytest.raises(DomainError) as exc:
        tickets.import_report("erp", "IV", "IV-8801", [])
    assert exc.value.code == "EMPTY_VALUE"
    with pytest.raises(DomainError) as exc:
        tickets.import_report("erp", "IV", "IV-8801", [{"sku": "000142", "qty": "10", "amount": "abc", "tax_rate": "13%"}])
    assert exc.value.code == "BAD_AMOUNT"
    with pytest.raises(DomainError) as exc:
        tickets.import_report("", "IV", "IV-8801", OLD_LINES)
    assert exc.value.code == "EMPTY_VALUE"


def test_api_import_adjudicate_and_vouchers(db):
    client = TestClient(app)
    payload = {"reports": [
        {"source": "erp", "doc_type": "IV", "doc_no": "IV-8801", "lines": OLD_LINES},
        {"source": "supplier-portal", "doc_type": "IV", "doc_no": "IV-8801", "lines": NEW_LINES},
    ]}
    imported = client.post("/api/documents/reports", json=payload)
    assert imported.status_code == 200 and imported.json()["disputed"] == 1

    dispute = client.get("/api/documents/disputes").json()["disputes"][0]
    corrected = next(c for c in dispute["candidates"] if c["total_amount"] == "880.00")
    stale = client.post("/api/documents/adjudicate", json={
        "ticket_key": dispute["ticket_key"], "digest": corrected["digest"],
        "reason_code": "CORRECTED_RESEND", "base_version": 99, "adjudication_id": "adj-api-1"})
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "STALE_VERSION"

    decided = client.post("/api/documents/adjudicate", json={
        "ticket_key": dispute["ticket_key"], "digest": corrected["digest"],
        "reason_code": "CORRECTED_RESEND", "base_version": dispute["version"], "adjudication_id": "adj-api-1"})
    assert decided.status_code == 200 and decided.json()["outcome"] == "POSTED"

    vouchers = client.get("/api/documents/vouchers").json()
    assert vouchers["suspended"] == []
    assert vouchers["vouchers"][0]["total_amount"] == "880.00"
    assert vouchers["vouchers"][0]["adjudication"]["reason_code"] == "CORRECTED_RESEND"


def test_api_rejects_missing_fields(db):
    client = TestClient(app)
    response = client.post("/api/documents/adjudicate", json={"ticket_key": KEY})
    assert response.status_code == 422 and response.json()["detail"]["code"] == "EMPTY_VALUE"
    response = client.post("/api/documents/reports", json={})
    assert response.status_code == 422
