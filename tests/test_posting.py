import json
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from bob.accounting import posting
from bob.accounting.entry import ProposedEntry, ProposedLine
from bob.agent.coding import record_proposal
from bob.db import utcnow
from bob.models import AuditEvent, Document, Job, Proposal, QboConnection, QboVendor
from bob.qbo.client import QBOClient
from tests.test_coding import TODAY, make_document, seed_cache

REALM = "4620816"


class FakeQBO:
    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.created: dict[str, dict] = {}
        self.next_id = 700
        self.balance_paid = False
        self.fail_create_status: int | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix(f"/v3/company/{REALM}/")
        params = request.url.params
        if request.method == "POST" and path == "upload":
            assert b"file_metadata_01" in request.content and b"%PDF" in request.content
            return httpx.Response(200, json={"AttachableResponse": [{"Attachable": {"Id": "a1"}}]})
        if request.method == "POST" and params.get("operation") == "delete":
            body = json.loads(request.content)
            return httpx.Response(
                200, json={path.capitalize(): {"Id": body["Id"], "status": "Deleted"}}
            )
        if request.method == "POST":
            if self.fail_create_status and path != "vendor":
                return httpx.Response(
                    self.fail_create_status,
                    json={
                        "Fault": {
                            "Error": [{"code": "6000", "Message": "Business validation error"}]
                        }
                    },
                )
            rid = params["requestid"]
            if rid in self.created:  # QuickBooks idempotency: same request id, same object
                return httpx.Response(200, json=self.created[rid])
            self.next_id += 1
            entity = {
                "vendor": "Vendor",
                "bill": "Bill",
                "vendorcredit": "VendorCredit",
                "purchase": "Purchase",
            }[path]
            body = json.loads(request.content)
            obj = {entity: {"Id": str(self.next_id), "SyncToken": "0", **body}}
            self.created[rid] = obj
            return httpx.Response(200, json=obj)
        if request.method == "GET" and path.startswith("bill/"):
            return httpx.Response(
                200,
                json={
                    "Bill": {
                        "Id": path.split("/")[1],
                        "SyncToken": "3",
                        "TotalAmt": 1260.0,
                        "Balance": 0.0 if self.balance_paid else 1260.0,
                    }
                },
            )
        return httpx.Response(404, json={})

    def posted(self, path: str) -> list[dict]:
        return [
            json.loads(r.content)
            for r in self.requests
            if r.method == "POST"
            and r.url.path.endswith(f"/{path}")
            and "operation" not in r.url.params
        ]


@pytest.fixture
def fake():
    return FakeQBO()


@pytest.fixture
def env(factory, settings, storage, fake):
    settings = settings.model_copy(
        update={"qbo_client_id": "c", "qbo_client_secret": "s", "qbo_writes_enabled": True}
    )
    with factory() as s:
        seed_cache(s)
        s.add(
            QboConnection(
                realm_id=REALM,
                environment="sandbox",
                access_token="t",
                access_expires_at=utcnow() + timedelta(hours=1),
                refresh_token="r",
                refresh_expires_at=utcnow() + timedelta(days=90),
            )
        )
        s.commit()
    qbo = QBOClient(
        factory,
        settings,
        http=httpx.Client(transport=httpx.MockTransport(fake.handler)),
        sleep=lambda _: None,
    )
    return settings, qbo


def entry(**kw) -> ProposedEntry:
    base = dict(  # noqa: C408
        kind="bill",
        vendor_name="Northwind Cloud Software Inc.",
        vendor_id="17",
        invoice_number="NW-10388",
        invoice_date=TODAY - timedelta(days=5),
        currency="CAD",
        subtotal=Decimal("1200.00"),
        tax_total=Decimal("60.00"),
        total=Decimal("1260.00"),
        lines=(ProposedLine("60", Decimal("1200.00"), "4", Decimal("60.00"), "Subscription"),),
        supplier_tax_number="812345678 RT0001",
        due_date=TODAY + timedelta(days=25),
    )
    base.update(kw)
    return ProposedEntry(**base)


def propose(factory, storage, settings, e: ProposedEntry, **kw) -> int:
    with factory() as s:
        doc_id = make_document(s, storage, counterparty=f"{e.vendor_name} {e.invoice_number}")
        p = record_proposal(
            s, s.get(Document, doc_id), e, settings, rationale="test", today=TODAY, **kw
        )
        s.commit()
        return p.id


def post(factory, storage, settings, qbo, pid):
    with factory() as s:
        posting.post_proposal(s, pid, qbo, storage, settings, today=TODAY)
        s.commit()
    with factory() as s:
        return s.get(Proposal, pid)


def test_bill_posts_with_request_id_note_and_attachment(env, factory, storage, fake):
    settings, qbo = env
    pid = propose(factory, storage, settings, entry())
    p = post(factory, storage, settings, qbo, pid)

    assert (p.status, p.qbo_entity) == ("posted", "Bill")
    [bill] = fake.posted("bill")
    assert bill["VendorRef"] == {"value": "17"}
    assert bill["DocNumber"] == "NW-10388"
    assert bill["Line"][0]["AccountBasedExpenseLineDetail"] == {
        "AccountRef": {"value": "60"},
        "TaxCodeRef": {"value": "4"},
    }
    assert bill["TxnTaxDetail"] == {"TotalTax": 60.0}
    assert bill["PrivateNote"] == f"Posted by Bob · proposal {pid} v1"
    assert "CurrencyRef" not in bill
    create = next(r for r in fake.requests if r.url.path.endswith("/bill"))
    assert create.url.params["requestid"] == f"bob-{pid}"
    assert any(r.url.path.endswith("/upload") for r in fake.requests)
    with factory() as s:
        assert s.get(Document, p.document_id).status == "posted"
        actions = s.scalars(select(AuditEvent.action)).all()
        assert {"proposal.posted", "qbo.attached"} <= set(actions)


def test_retry_after_crash_does_not_duplicate(env, factory, storage, fake):
    settings, qbo = env
    pid = propose(factory, storage, settings, entry())
    post(factory, storage, settings, qbo, pid)
    with factory() as s:  # simulate a crash that lost the "posted" commit
        s.get(Proposal, pid).status = "posting"
        s.commit()
    p = post(factory, storage, settings, qbo, pid)
    assert p.status == "posted"
    assert len(fake.created) == 1  # the same request id returned the same bill


def test_new_vendor_is_created_first(env, factory, storage, fake):
    settings, qbo = env
    pid = propose(
        factory, storage, settings, entry(vendor_id=None, vendor_name="Deskworks Online Ltd.")
    )
    p = post(factory, storage, settings, qbo, pid)
    assert p.status == "posted"
    [vendor] = fake.posted("vendor")
    assert vendor == {"DisplayName": "Deskworks Online Ltd."}
    [bill] = fake.posted("bill")
    assert bill["VendorRef"]["value"] == p.entry["vendor_id"]
    with factory() as s:
        assert s.get(QboVendor, p.entry["vendor_id"]).display_name == "Deskworks Online Ltd."


def test_credit_note_posts_as_vendor_credit(env, factory, storage, fake):
    settings, qbo = env
    pid = propose(
        factory, storage, settings, entry(kind="credit", invoice_number="CN-1", due_date=None)
    )
    assert post(factory, storage, settings, qbo, pid).qbo_entity == "VendorCredit"
    assert "DueDate" not in fake.posted("vendorcredit")[0]


def test_receipt_needs_payment_account(env, factory, storage, fake):
    settings, qbo = env
    pid = propose(factory, storage, settings, entry(kind="expense", invoice_number="R-1"))
    p = post(factory, storage, settings, qbo, pid)
    assert p.status == "held"
    assert "Which account paid" in p.question

    settings = settings.model_copy(update={"expense_payment_account_id": "41"})
    with factory() as s:
        posting.approve(s, pid, "oliver@bridgewerk.ca")
        s.commit()
    p = post(factory, storage, settings, qbo, pid)
    assert p.status == "posted"
    purchase = fake.posted("purchase")[0]
    assert purchase["AccountRef"] == {"value": "41"} and purchase["PaymentType"] == "CreditCard"


def test_writes_disabled_leaves_it_approved(env, factory, storage, fake):
    settings, qbo = env
    qbo.settings = settings.model_copy(update={"qbo_writes_enabled": False})
    pid = propose(factory, storage, settings, entry())
    p = post(factory, storage, settings, qbo, pid)
    assert p.status == "approved"
    assert "writes are disabled" in p.last_error
    assert fake.requests == []


def test_kill_switch(env, factory, storage, fake):
    settings, qbo = env
    with factory() as s:
        posting.set_posting_paused(s, True, "cli:oliver")
        s.commit()
    pid = propose(factory, storage, settings, entry())
    assert post(factory, storage, settings, qbo, pid).status == "approved"
    assert fake.requests == []


def test_daily_cap_holds_rule_approvals_only(env, factory, storage, fake):
    settings, qbo = env
    settings = settings.model_copy(update={"daily_post_cap_count": 1})
    first = propose(factory, storage, settings, entry(invoice_number="A-1"))
    second = propose(factory, storage, settings, entry(invoice_number="A-2"))
    assert post(factory, storage, settings, qbo, first).status == "posted"
    p = post(factory, storage, settings, qbo, second)
    assert p.status == "held" and "daily posting limit" in p.question
    with factory() as s:
        posting.approve(s, second, "oliver@bridgewerk.ca")
        s.commit()
    assert post(factory, storage, settings, qbo, second).status == "posted"


def test_quickbooks_rejection_marks_failed(env, factory, storage, fake):
    settings, qbo = env
    fake.fail_create_status = 400
    pid = propose(factory, storage, settings, entry())
    p = post(factory, storage, settings, qbo, pid)
    assert p.status == "failed"
    assert "6000" in p.last_error


def test_duplicate_found_at_posting_time_is_not_posted(env, factory, storage, fake):
    settings, qbo = env
    first = propose(factory, storage, settings, entry())
    second = propose(factory, storage, settings, entry(), approved_by="oliver@bridgewerk.ca")
    # coding already rejected the second as a duplicate of the first (approved) proposal
    with factory() as s:
        assert s.get(Proposal, second).status == "needs_fix"
    assert post(factory, storage, settings, qbo, first).status == "posted"
    assert len(fake.posted("bill")) == 1


def test_approve_refuses_rejected_entries(env, factory, storage):
    settings, _ = env
    pid = propose(factory, storage, settings, entry(total=Decimal("9.99")))
    with factory() as s, pytest.raises(ValueError, match="needs_fix"):
        posting.approve(s, pid, "oliver@bridgewerk.ca")


def test_undo_deletes_and_records(env, factory, storage, fake):
    settings, qbo = env
    pid = propose(factory, storage, settings, entry())
    p = post(factory, storage, settings, qbo, pid)
    with factory() as s:
        result = posting.reverse(s, pid, qbo, "oliver@bridgewerk.ca", "wrong account")
        s.commit()
        assert result.done
        assert s.get(Proposal, pid).status == "reversed"
        assert s.get(Document, p.document_id).status == "reversed"
    delete = fake.requests[-1]
    assert delete.url.params["operation"] == "delete"
    assert json.loads(delete.content) == {"Id": p.qbo_id, "SyncToken": "3"}


def test_undo_refuses_paid_bill_and_closed_period(env, factory, storage, fake):
    settings, qbo = env
    pid = propose(factory, storage, settings, entry())
    post(factory, storage, settings, qbo, pid)
    fake.balance_paid = True
    with factory() as s, pytest.raises(posting.UndoRefused, match="payment"):
        posting.reverse(s, pid, qbo, "oliver", "x")

    fake.balance_paid = False
    with factory() as s:
        from bob.models import QboSetting

        s.merge(QboSetting(key="closing_date", value=TODAY.isoformat()))
        s.commit()
    with factory() as s, pytest.raises(posting.UndoRefused, match="closed period"):
        posting.reverse(s, pid, qbo, "oliver", "x")


def test_find_posted_for_bulk_undo(env, factory, storage, fake):
    settings, qbo = env
    a = propose(factory, storage, settings, entry(invoice_number="B-1"))
    b = propose(
        factory,
        storage,
        settings,
        entry(invoice_number="B-2", vendor_id=None, vendor_name="Bow River Office Supply Ltd."),
    )
    post(factory, storage, settings, qbo, a)
    post(factory, storage, settings, qbo, b)
    with factory() as s:
        assert [p.id for p in posting.find_posted(s, vendor="bow river office supply")] == [b]
        assert {p.id for p in posting.find_posted(s, ids=[a, b])} == {a, b}


def test_admin_cli_pause_resume_requeues(env, factory, storage, settings, monkeypatch):
    from bob import admin

    settings_, _ = env
    monkeypatch.setattr(admin, "get_settings", lambda: settings_)
    assert admin.main(["pause"]) == 0
    pid = propose(factory, storage, settings_, entry())
    assert admin.main(["resume"]) == 0
    with factory() as s:
        assert not posting.posting_paused(s)
        kinds = [j.payload for j in s.scalars(select(Job).where(Job.kind == "post_proposal"))]
        assert {"proposal_id": pid} in kinds
