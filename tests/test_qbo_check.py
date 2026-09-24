import json
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from bob.db import utcnow
from bob.models import QboConnection, QboVendor
from bob.qbo.check import CHECK_VENDOR, read_checks, write_test
from bob.qbo.client import QBOClient
from tests.test_qbo import REALM, FakeIntuit


class FakeCompany(FakeIntuit):
    """FakeIntuit plus company info and a transaction store that can be created, read,
    attached to and deleted. `recompute_tax` mimics QuickBooks ignoring the sent TotalTax and
    taxing each line itself."""

    def __init__(self, country: str = "CA", recompute_tax: bool = False):
        super().__init__()
        self.country = country
        self.recompute_tax = recompute_tax
        self.accounts.append(
            {"Id": "90", "Name": "Company Visa", "AccountType": "Credit Card", "Active": True}
        )
        self.store: dict[tuple[str, str], dict] = {}
        self.uploads: list[str] = []
        self.next_id = 700

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix(f"/v3/company/{REALM}/")
        parts = path.split("/")
        if path == f"companyinfo/{REALM}":
            self.requests.append(request)
            return httpx.Response(
                200, json={"CompanyInfo": {"CompanyName": "Sandbox Co", "Country": self.country}}
            )
        if path == "upload":
            self.requests.append(request)
            self.uploads.append(request.url.params["requestid"])
            return httpx.Response(200, json={"AttachableResponse": [{"Attachable": {"Id": "1"}}]})
        entities = {"bill": "Bill", "vendorcredit": "VendorCredit", "purchase": "Purchase"}
        if parts[0] in entities or parts[0] == "vendor":
            self.requests.append(request)
            entity = entities.get(parts[0], "Vendor")
            if request.method == "GET":
                obj = self.store.get((entity, parts[1]))
                if obj is None:
                    return httpx.Response(400, json={"Fault": {"Error": [{"code": "610"}]}})
                return httpx.Response(200, json={entity: obj})
            body = json.loads(request.content)
            if request.url.params.get("operation") == "delete":
                self.store.pop((entity, body["Id"]))
                return httpx.Response(200, json={entity: {"Id": body["Id"], "status": "Deleted"}})
            return httpx.Response(200, json={entity: self._create(entity, body)})
        return super().handler(request)

    def _create(self, entity: str, body: dict) -> dict:
        self.next_id += 1
        obj = {**body, "Id": str(self.next_id), "SyncToken": "0"}
        if entity != "Vendor":
            amounts = [Decimal(str(line["Amount"])) for line in body["Line"]]
            tax = Decimal(str(body["TxnTaxDetail"]["TotalTax"]))
            if self.recompute_tax:
                tax = sum(((a * Decimal("0.05")).quantize(Decimal("0.01")) for a in amounts))
            obj["TxnTaxDetail"] = {"TotalTax": float(tax)}
            obj["TotalAmt"] = float(sum(amounts) + tax)
        self.store[(entity, obj["Id"])] = obj
        return obj


def _client(factory, settings, company: FakeCompany, **overrides) -> QBOClient:
    settings = settings.model_copy(
        update={"qbo_client_id": "cid", "qbo_client_secret": "secret", **overrides}
    )
    http = httpx.Client(transport=httpx.MockTransport(company.handler))
    return QBOClient(factory, settings, http=http, sleep=lambda _: None)


@pytest.fixture
def connected(factory):
    with factory() as s:
        s.add(
            QboConnection(
                realm_id=REALM,
                environment="sandbox",
                access_token="access-1",
                access_expires_at=utcnow() + timedelta(hours=1),
                refresh_token="refresh-1",
                refresh_expires_at=utcnow() + timedelta(days=100),
            )
        )
        s.commit()
    return factory


def _statuses(checks) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {"ok": [], "warn": [], "fail": []}
    for c in checks:
        out[c.status].append(c.message)
    return out


def test_read_checks_on_a_canadian_company(connected, settings):
    company = FakeCompany()
    with connected() as s:
        result = _statuses(read_checks(s, _client(connected, settings, company)))
    assert result["fail"] == []
    assert any("GST purchase tax code: GST (id 4)" in m for m in result["ok"])
    assert any("Closing date is 2026-09-30" in m for m in result["ok"])
    # The payment account is not configured yet; the card account is offered as a candidate.
    assert result["warn"] == [
        "BOB_EXPENSE_PAYMENT_ACCOUNT_ID is not set, so receipts will be held. "
        "Candidates: Company Visa (id 90)"
    ]


def test_read_checks_fail_on_a_us_company(connected, settings):
    with connected() as s:
        result = _statuses(read_checks(s, _client(connected, settings, FakeCompany("US"))))
    assert any("set up for US" in m for m in result["fail"])


def test_read_checks_flag_a_wrong_payment_account(connected, settings):
    client = _client(connected, settings, FakeCompany(), expense_payment_account_id="7")
    with connected() as s:
        result = _statuses(read_checks(s, client))
    assert any("BOB_EXPENSE_PAYMENT_ACCOUNT_ID=7 is not" in m for m in result["fail"])


def test_write_test_round_trips_and_cleans_up(connected, settings):
    company = FakeCompany()
    client = _client(connected, settings, company, qbo_writes_enabled=True)
    with connected() as s:
        read_checks(s, client)
        result = _statuses(write_test(s, client))
        s.commit()

    assert result["fail"] == [] and result["warn"] == []
    assert any("Bill: created; QuickBooks kept tax 3.33, total 69.99" in m for m in result["ok"])
    assert sum("deleted again" in m for m in result["ok"]) == 3  # bill, credit, card purchase
    assert len(company.uploads) == 1
    # Only the reusable test vendor is left behind.
    assert list(company.store) == [("Vendor", "701")]
    purchase = next(
        json.loads(r.content)
        for r in company.requests
        if r.url.path.endswith("/purchase") and "operation" not in r.url.params
    )
    assert purchase["AccountRef"] == {"value": "90"}

    # A second run reuses the vendor instead of creating another.
    with connected() as s:
        assert s.scalars(select(QboVendor).where(QboVendor.display_name == CHECK_VENDOR)).one()
        write_test(s, client)
    assert [k for k in company.store if k[0] == "Vendor"] == [("Vendor", "701")]


def test_write_test_reports_recomputed_tax(connected, settings):
    company = FakeCompany(recompute_tax=True)
    client = _client(connected, settings, company, qbo_writes_enabled=True)
    with connected() as s:
        read_checks(s, client)
        result = _statuses(write_test(s, client))

    assert result["fail"] == [
        "Bill: sent tax 3.33 and total 69.99; QuickBooks stored tax 3.34 and total 70.00. "
        "Posting must send explicit tax lines so the books match the invoice."
    ]
    assert [k for k in company.store if k[0] != "Vendor"] == []  # still cleaned up


def test_write_test_refuses_production(connected, settings):
    client = _client(
        connected, settings, FakeCompany(), qbo_environment="production", qbo_writes_enabled=True
    )
    with connected() as s, pytest.raises(ValueError, match="sandbox"):
        write_test(s, client)
