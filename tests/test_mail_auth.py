import pytest

from bob.mail.auth import auth_verdict, check_sender, lookalike_of, parse_auth_results


def ar(value: str) -> list[tuple[str, str]]:
    return [("Received", "from mail.example"), ("Authentication-Results", value)]


PASS = ar(
    "spf=pass smtp.mailfrom=vendor.com; dkim=pass header.d=vendor.com; dmarc=pass action=none header.from=vendor.com; compauth=pass reason=100"
)
FAIL = ar(
    "spf=fail smtp.mailfrom=bridgewerk.ca; dkim=none; dmarc=fail action=quarantine header.from=bridgewerk.ca; compauth=fail reason=000"
)


def test_parse_uses_first_header():
    headers = PASS + [("Authentication-Results", "dmarc=fail")]
    assert parse_auth_results(headers)["dmarc"] == "pass"


@pytest.mark.parametrize(
    ("results", "verdict"),
    [
        ({}, "none"),
        ({"dmarc": "pass"}, "pass"),
        ({"dmarc": "bestguesspass"}, "pass"),
        ({"dmarc": "none", "compauth": "pass"}, "pass"),
        ({"dmarc": "fail"}, "fail"),
        ({"spf": "softfail", "dkim": "none"}, "fail"),
        ({"spf": "fail", "dkim": "pass"}, "none"),
    ],
)
def test_verdict(results, verdict):
    assert auth_verdict(results) == verdict


@pytest.mark.parametrize(
    ("domain", "imitates"),
    [
        ("vendor.com", None),
        ("vend0r.com", "vendor.com"),
        ("vendorr.com", "vendor.com"),
        ("bridgewerk.co", "bridgewerk.ca"),
        ("bridgevverk.ca", "bridgewerk.ca"),
        ("bridgewerk-billing.com", "bridgewerk.ca"),
        ("stripe.com", None),
    ],
)
def test_lookalikes(domain, imitates):
    assert lookalike_of(domain, {"vendor.com", "bridgewerk.ca"}) == imitates


def test_internal_mail_without_results_is_internal(settings):
    check = check_sender("oliver@bridgewerk.ca", [], settings)
    assert (check.trust, check.auth) == ("internal", "none")


def test_spoofed_internal_address_is_suspicious(settings):
    check = check_sender("oliver@bridgewerk.ca", FAIL, settings)
    assert check.trust == "suspicious"
    assert "our own domain" in check.reason


def test_known_sender_passing_is_known(settings):
    assert check_sender("billing@vendor.com", PASS, settings).trust == "known"


def test_known_sender_failing_is_downgraded(settings):
    check = check_sender("billing@vendor.com", FAIL, settings)
    assert check.trust == "unknown"
    assert "failed authentication" in check.reason


def test_lookalike_of_known_vendor_is_suspicious(settings):
    check = check_sender("billing@vend0r.com", PASS, settings)
    assert check.trust == "suspicious"
    assert check.detail["lookalike_of"] == "vendor.com"


def test_stranger_is_unknown(settings):
    assert check_sender("hello@stripe.com", PASS, settings).trust == "unknown"
