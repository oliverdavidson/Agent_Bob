"""Is the sender who they claim to be?

Exchange Online Protection stamps an Authentication-Results header (SPF, DKIM, DMARC and
Microsoft's composite "compauth") on every message arriving from outside the tenant. Mail sent
between BridgeWerk users never passes that inbound filter, so it carries no such header.
That gives three verdicts:

- pass: DMARC or compauth passed.
- fail: DMARC or compauth failed, or SPF failed with no passing DKIM.
- none: no authentication results (internal mail, or an unusual route).

Separately, a sender domain one or two characters away from a domain we trust is a lookalike,
the usual shape of an invoice-fraud email.
"""

import re
from dataclasses import dataclass

from bob.config import Settings

RESULT = re.compile(r"\b(spf|dkim|dmarc|compauth)\s*=\s*([a-z]+)", re.IGNORECASE)
# Characters commonly swapped to fake a domain.
HOMOGLYPHS = str.maketrans({"0": "o", "1": "l", "3": "e", "5": "s", "|": "l"})


@dataclass(frozen=True)
class SenderCheck:
    trust: str  # internal | known | unknown | suspicious
    auth: str  # pass | fail | none
    detail: dict

    @property
    def reason(self) -> str | None:
        return self.detail.get("reason")


def parse_auth_results(headers: list[tuple[str, str]]) -> dict[str, str]:
    """Results from the first (topmost, added by Microsoft) Authentication-Results header."""
    for name, value in headers:
        if name.lower() == "authentication-results":
            results: dict[str, str] = {}
            for key, verdict in RESULT.findall(value):
                results.setdefault(key.lower(), verdict.lower())
            return results
    return {}


def auth_verdict(results: dict[str, str]) -> str:
    if not results:
        return "none"
    dmarc, compauth = results.get("dmarc"), results.get("compauth")
    if dmarc in {"pass", "bestguesspass"} or compauth == "pass":
        return "pass"
    if dmarc == "fail" or compauth == "fail":
        return "fail"
    if results.get("spf") in {"fail", "softfail"} and results.get("dkim") != "pass":
        return "fail"
    return "none"


def _levenshtein(a: str, b: str) -> int:
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


def lookalike_of(domain: str, trusted: set[str]) -> str | None:
    """The trusted domain this one imitates, if any."""
    if domain in trusted:
        return None
    folded = domain.translate(HOMOGLYPHS).replace("rn", "m").replace("vv", "w")
    for good in trusted:
        good_folded = good.translate(HOMOGLYPHS).replace("rn", "m").replace("vv", "w")
        if folded == good_folded or _levenshtein(folded, good_folded) <= 2:
            return good
        # bridgewerk.ca.invoices.co, bridgewerk-ca.com and similar
        stem = good.rsplit(".", 1)[0]
        if len(stem) >= 5 and stem in domain:
            return good
    return None


def check_sender(address: str, headers: list[tuple[str, str]], settings: Settings) -> SenderCheck:
    address = address.lower()
    domain = address.rsplit("@", 1)[-1]
    internal = {d.lower() for d in settings.internal_domains}
    known_addresses = {a.lower() for a in settings.known_sender_addresses}
    trusted = internal | {a.rsplit("@", 1)[-1] for a in known_addresses}

    results = parse_auth_results(headers)
    auth = auth_verdict(results)
    detail: dict = {"results": results}

    if domain in internal:
        if auth == "fail":
            detail["reason"] = "Claims to be from our own domain but failed authentication."
            return SenderCheck("suspicious", auth, detail)
        if auth == "none" or auth == "pass":
            return SenderCheck("internal", auth, detail)

    if (imitated := lookalike_of(domain, trusted)) is not None:
        detail["reason"] = f"Sender domain {domain} looks like {imitated}."
        detail["lookalike_of"] = imitated
        return SenderCheck("suspicious", auth, detail)

    if address in known_addresses:
        if auth == "fail":
            detail["reason"] = f"{address} is a known sender, but this email failed authentication."
            return SenderCheck("unknown", auth, detail)
        return SenderCheck("known", auth, detail)

    return SenderCheck("unknown", auth, detail)
