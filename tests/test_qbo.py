import json
from datetime import timedelta
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from sqlalchemy import select

from bob.accounting.context import build_context
from bob.db import utcnow
from bob.models import AuditEvent, QboAccount, QboConnection, QboTaxCode
from bob.qbo import oauth
from bob.qbo.client import NotConnected, QBOClient, QBOError, WritesDisabled
from bob.qbo.connect import parse_redirect
from bob.qbo.sync import sync_reference_data

REALM = "9130355"


class FakeIntuit:
    """Records requests and answers like the QBO API and Intuit token endpoint."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.valid_token = "access-1"
        self.issued = 1
        self.fail_next: list[int] = []
        self.accounts = [
            {"Id": str(i), "Name": f"Account {i}", "AccountType": "Expense", "Active": True}
            for i in range(1, 1203)
        ]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url == httpx.URL(oauth.TOKEN_URL):
            form = parse_qs(request.content.decode())
            assert form["grant_type"] == ["refresh_token"]
            self.issued += 1
            self.valid_token = f"access-{self.issued}"
            return httpx.Response(
                200,
                json={
                    "access_token": self.valid_token,
                    "refresh_token": f"refresh-{self.issued}",
                    "expires_in": 3600,
                    "x_refresh_token_expires_in": 8726400,
                },
            )
        if self.fail_next:
            return httpx.Response(self.fail_next.pop(0), json={})
        if request.headers["Authorization"] != f"Bearer {self.valid_token}":
            return httpx.Response(401, json={"Fault": {"Error": [{"code": "3200"}]}})
        path = request.url.path.removeprefix(f"/v3/company/{REALM}/")
        if path == "query":
            return self._query(request.url.params["query"])
        if path == "preferences":
            return httpx.Response(
                200,
                json={
                    "Preferences": {
                        "AccountingInfoPrefs": {"BookCloseDate": "2026-09-30"},
                        "CurrencyPrefs": {"HomeCurrency": {"value": "CAD"}},
                    }
                },
            )
        if path == "bill" and request.method == "POST":
            body = json.loads(request.content)
            if not body.get("VendorRef"):
                return httpx.Response(
                    400,
                    json={
                        "Fault": {"Error": [{"code": "2020", "Message": "Required param missing"}]}
                    },
                )
            return httpx.Response(200, json={"Bill": {"Id": "501", **body}})
        return httpx.Response(404, json={})

    def _query(self, q: str) -> httpx.Response:
        words = q.split()
        entity = words[3]
        start = int(words[words.index("STARTPOSITION") + 1])
        size = int(words[words.index("MAXRESULTS") + 1])
        data = {
            "Account": self.accounts,
            "Vendor": [{"Id": "17", "DisplayName": "Northwind Software", "Active": True}],
            "TaxRate": [
                {"Id": "3", "Name": "GST", "RateValue": 5},
                {"Id": "7", "Name": "HST ON", "RateValue": 13},
            ],
            "Bill": [
                {
                    "Id": "900",
                    "DocNumber": "NW-10311",
                    "TxnDate": "2026-09-01",
                    "TotalAmt": 1260.0,
                    "VendorRef": {"value": "17", "name": "Northwind Software"},
                    "Line": [
                        {
                            "Amount": 1200.0,
                            "Description": "Subscription",
                            "DetailType": "AccountBasedExpenseLineDetail",
                            "AccountBasedExpenseLineDetail": {
                                "AccountRef": {"value": "7", "name": "Software"},
                                "TaxCodeRef": {"value": "4"},
                            },
                        }
                    ],
                }
            ],
            "VendorCredit": [],
            "Purchase": [],
            "TaxCode": [
                {
                    "Id": "4",
                    "Name": "GST",
                    "Active": True,
                    "PurchaseTaxRateList": {"TaxRateDetail": [{"TaxRateRef": {"value": "3"}}]},
                },
                {
                    "Id": "8",
                    "Name": "HST ON",
                    "Active": True,
                    "PurchaseTaxRateList": {"TaxRateDetail": [{"TaxRateRef": {"value": "7"}}]},
                },
                {"Id": "2", "Name": "Exempt", "Active": True, "PurchaseTaxRateList": {}},
            ],
        }[entity]
        page = data[start - 1 : start - 1 + size]
        return httpx.Response(200, json={"QueryResponse": {entity: page, "startPosition": start}})


@pytest.fixture
def intuit():
    return FakeIntuit()


@pytest.fixture
def qbo(factory, settings, intuit):
    settings = settings.model_copy(update={"qbo_client_id": "cid", "qbo_client_secret": "secret"})
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
    http = httpx.Client(transport=httpx.MockTransport(intuit.handler))
    return QBOClient(factory, settings, http=http, sleep=lambda _: None)


def test_query_follows_pages(qbo, intuit):
    rows = qbo.query("select * from Account")
    assert len(rows) == 1202
    queries = [r.url.params["query"] for r in intuit.requests]
    assert queries == [
        "select * from Account STARTPOSITION 1 MAXRESULTS 1000",
        "select * from Account STARTPOSITION 1001 MAXRESULTS 1000",
    ]
    assert intuit.requests[0].url.params["minorversion"] == "75"
    assert intuit.requests[0].url.host == "sandbox-quickbooks.api.intuit.com"


def test_expiring_token_is_refreshed_and_stored(qbo, intuit, factory):
    with factory() as s:
        s.scalars(select(QboConnection)).one().access_expires_at = utcnow() + timedelta(minutes=2)
        s.commit()

    qbo.preferences()

    with factory() as s:
        conn = s.scalars(select(QboConnection)).one()
        assert (conn.access_token, conn.refresh_token) == ("access-2", "refresh-2")


def test_rejected_token_is_refreshed_once(qbo, intuit):
    intuit.valid_token = "revoked-elsewhere"  # the stored token no longer works
    assert qbo.preferences()["AccountingInfoPrefs"]["BookCloseDate"] == "2026-09-30"
    token_calls = [r for r in intuit.requests if r.url == httpx.URL(oauth.TOKEN_URL)]
    assert len(token_calls) == 1


def test_persistent_401_is_not_retried_forever(qbo, intuit):
    intuit.fail_next = [401, 401, 401]
    with pytest.raises(QBOError) as err:
        qbo.preferences()
    assert err.value.status == 401
    token_calls = [r for r in intuit.requests if r.url == httpx.URL(oauth.TOKEN_URL)]
    assert len(token_calls) == 1


def test_transient_errors_are_retried(qbo, intuit):
    intuit.fail_next = [503, 429]
    assert qbo.preferences()["AccountingInfoPrefs"]["BookCloseDate"] == "2026-09-30"


def test_writes_are_refused_when_disabled(qbo, intuit):
    with pytest.raises(WritesDisabled):
        qbo.create("Bill", {"VendorRef": {"value": "17"}}, request_id="proposal-1")
    assert intuit.requests == []  # nothing was sent


def test_write_sends_request_id_when_enabled(qbo, intuit):
    qbo.settings = qbo.settings.model_copy(update={"qbo_writes_enabled": True})
    bill = qbo.create("Bill", {"VendorRef": {"value": "17"}}, request_id="proposal-1")
    assert bill["Id"] == "501"
    assert intuit.requests[-1].url.params["requestid"] == "proposal-1"


def test_fault_is_raised_with_details(qbo):
    qbo.settings = qbo.settings.model_copy(update={"qbo_writes_enabled": True})
    with pytest.raises(QBOError, match="2020: Required param missing"):
        qbo.create("Bill", {}, request_id="proposal-2")


def test_not_connected(factory, settings):
    client = QBOClient(
        factory, settings, http=httpx.Client(transport=httpx.MockTransport(lambda r: None))
    )
    with pytest.raises(NotConnected):
        client.preferences()


def test_sync_fills_cache_and_feeds_validator(qbo, factory):
    with factory() as s:
        summary = sync_reference_data(s, qbo)
        s.commit()
    assert (summary.accounts, summary.vendors, summary.tax_codes, summary.bills) == (1202, 1, 3, 1)

    with factory() as s:
        rates = {t.name: Decimal(t.purchase_rate) for t in s.scalars(select(QboTaxCode))}
        assert rates == {"GST": Decimal("0.05"), "HST ON": Decimal("0.13"), "Exempt": Decimal(0)}
        assert s.get(QboAccount, "7").account_type == "Expense"
        assert s.scalar(select(AuditEvent.action)) == "qbo.synced"

        ctx = build_context(s, utcnow().date(), Decimal(25000))
        assert ctx.closing_date.isoformat() == "2026-09-30"
        assert ctx.tax_codes["8"].purchase_rate == Decimal("0.13")
        assert len(ctx.accounts) == 1202


def test_sync_twice_updates_in_place(qbo, factory, intuit):
    with factory() as s:
        sync_reference_data(s, qbo)
        s.commit()
    intuit.accounts[0]["Active"] = False
    with factory() as s:
        sync_reference_data(s, qbo)
        s.commit()
        assert s.get(QboAccount, "1").active is False
        assert s.query(QboAccount).count() == 1202


def test_authorization_url():
    url = urlparse(oauth.authorization_url("cid", "http://localhost:8765/qbo/callback", "xyz"))
    params = parse_qs(url.query)
    assert url.netloc == "appcenter.intuit.com"
    assert params["scope"] == ["com.intuit.quickbooks.accounting"]
    assert params["state"] == ["xyz"]


def test_parse_redirect():
    url = "http://localhost:8765/qbo/callback?code=AB12&state=xyz&realmId=9130355"
    assert parse_redirect(url, "xyz") == ("AB12", "9130355")
    with pytest.raises(ValueError, match="State"):
        parse_redirect(url, "other")
    with pytest.raises(ValueError, match="access_denied"):
        parse_redirect("http://localhost/cb?error=access_denied&state=xyz", "xyz")
