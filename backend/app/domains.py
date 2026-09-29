"""Custom subdomain connection (brief §6).

State machine
    pending --(CNAME + TXT ok, CAA allows)--> verified --(cert requested)--> ssl_issuing --(valid cert)--> live
       \\______________________________ failed (specific reason) <__________/                          |
    live --(CNAME removed/changed, cert fails)--> failed  (alert client admins + platform admins)

The DNS resolver and TLS probe are injectable so the whole flow is testable offline.
"""

from __future__ import annotations

import secrets
import socket
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import dns.exception
import dns.resolver
import tldextract

from app.config import get_settings

_EXTRACT = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)   # bundled snapshot, no network


class DomainError(ValueError):
    pass


def normalize_hostname(raw: str) -> str:
    h = raw.strip().lower().rstrip(".")
    for prefix in ("https://", "http://"):
        if h.startswith(prefix):
            h = h[len(prefix):]
    h = h.split("/")[0].split(":")[0]
    if not h or len(h) > 253 or ".." in h:
        raise DomainError("Enter a subdomain like investors.yourcompany.com.")
    labels = h.split(".")
    for label in labels:
        if not label or len(label) > 63 or not all(c.isalnum() or c == "-" for c in label) or label.startswith("-") or label.endswith("-"):
            raise DomainError("That isn't a valid domain name.")
    ext = _EXTRACT(h)
    if not ext.suffix or not ext.domain:
        raise DomainError("That isn't a domain you can register.")
    if not ext.subdomain:
        raise DomainError(
            f"{h} is a root (apex) domain. Use a subdomain such as investors.{h} — a root domain can't point to "
            "our platform with a CNAME record, and would take over your main website.")
    cfg = get_settings()
    if h == cfg.platform_domain or h.endswith("." + cfg.platform_domain):
        raise DomainError("Use your own company's domain, not the platform's.")
    return h


def new_token() -> str:
    return secrets.token_hex(16)


def txt_name(hostname: str) -> str:
    return f"_{get_settings().verify_prefix}.{hostname}"


def txt_value(token: str) -> str:
    return f"{get_settings().verify_prefix}-verify={token}"


def records(hostname: str, token: str) -> list[dict]:
    cfg = get_settings()
    return [
        {"type": "CNAME", "name": hostname, "host_label": hostname.split(".")[0],
         "value": cfg.platform_target_hostname, "ttl": 3600},
        {"type": "TXT", "name": txt_name(hostname), "host_label": txt_name(hostname).rsplit(".", _depth(hostname))[0],
         "value": txt_value(token), "ttl": 3600},
    ]


def _depth(hostname: str) -> int:
    ext = _EXTRACT(hostname)
    return len(f"{ext.domain}.{ext.suffix}".split("."))


PROVIDER_NOTES = {
    "cloudflare": [
        "Open your domain in the Cloudflare dashboard → DNS → Records → Add record.",
        "Add the CNAME record. Set Proxy status to “DNS only” (grey cloud). If it is proxied (orange cloud), we can't issue your certificate and the site won't load.",
        "Add the TXT record with the name and value shown.",
    ],
    "godaddy": [
        "In GoDaddy, go to My Products → your domain → DNS → Add New Record.",
        "Add the CNAME: in “Name” enter only the host part (for example “investors”), and in “Value” the target shown.",
        "Add the TXT record: in “Name” enter the host part shown (GoDaddy adds your domain automatically).",
    ],
    "route53": [
        "In AWS Route 53, open Hosted zones → your domain → Create record.",
        "Create a CNAME record for the subdomain with the value shown (simple routing).",
        "Create a TXT record for the verification name. Route 53 needs the value wrapped in double quotes.",
    ],
    "squarespace": [
        "Google Domains moved to Squarespace. In Squarespace, open Domains → your domain → DNS → DNS Settings → Add record.",
        "Add the CNAME with host set to the subdomain part (for example “investors”) and data set to the target shown.",
        "Add the TXT record with the host part and value shown.",
    ],
    "other": [
        "Sign in to wherever your domain's DNS is managed (often your registrar or IT team).",
        "Add both records exactly as shown. Some providers want only the host part in the name field.",
        "Changes usually appear within minutes but can take up to 48 hours.",
    ],
}


def it_email(company: str, hostname: str, token: str) -> str:
    rs = records(hostname, token)
    lines = [f"Subject: DNS records needed for {hostname}", "",
             "Hello,", "",
             f"We're publishing our investor reports at {hostname}. Please add these two DNS records:", ""]
    for r in rs:
        lines.append(f"  Type: {r['type']}   Name: {r['name']}   Value: {r['value']}   TTL: {r['ttl']}")
    lines += ["",
              "Notes:",
              "- The CNAME must not be proxied (on Cloudflare set it to “DNS only”).",
              f"- If the domain has CAA records, they must allow certificates from one of: {get_settings().acme_ca_domains}.",
              "- Please don't add other records (A, AAAA, MX) for this same subdomain; a CNAME can't coexist with them.",
              "",
              "Thank you,", company]
    return "\n".join(lines)


# ------------------------------------------------------------------ checking


@dataclass
class DnsView:
    cname: str | None                   # target of the CNAME at hostname (None = no CNAME)
    cname_error: str | None             # 'nxdomain' | 'no_answer' | 'timeout' | None
    txt: list[str]
    caa: list[tuple[str, str]]          # (tag, value) of the closest CAA set, walking up the tree


Resolver = Callable[[str], DnsView]


def _q(name: str, rtype: str):
    r = dns.resolver.Resolver()
    r.lifetime = 5
    return r.resolve(name, rtype, raise_on_no_answer=False)


def live_resolver(hostname: str) -> DnsView:
    cname = cname_err = None
    try:
        ans = _q(hostname, "CNAME")
        if ans.rrset:
            cname = str(ans.rrset[0].target).rstrip(".").lower()
        else:
            cname_err = "no_answer"
    except dns.resolver.NXDOMAIN:
        cname_err = "nxdomain"
    except (dns.exception.Timeout, dns.resolver.NoNameservers):
        cname_err = "timeout"
    txt: list[str] = []
    try:
        ans = _q(txt_name(hostname), "TXT")
        for rr in ans.rrset or []:
            txt.append(b"".join(rr.strings).decode(errors="replace"))
    except (dns.resolver.NXDOMAIN, dns.exception.Timeout, dns.resolver.NoNameservers):
        pass
    caa: list[tuple[str, str]] = []
    labels = hostname.split(".")
    for i in range(len(labels) - 1):
        name = ".".join(labels[i:])
        try:
            ans = _q(name, "CAA")
            if ans.rrset:
                caa = [(rr.tag.decode(), rr.value.decode()) for rr in ans.rrset]
                break
        except (dns.resolver.NXDOMAIN, dns.exception.Timeout, dns.resolver.NoNameservers):
            continue
    return DnsView(cname, cname_err, txt, caa)


@dataclass
class CheckResult:
    status: str                  # next status
    failure_code: str | None = None
    failure_reason: str | None = None
    cert_expires_at: datetime | None = None


FAILURES = {
    "cname_missing": "The CNAME record for {h} wasn't found. Add it (it can take a few minutes to appear).",
    "cname_elsewhere": "{h} points to {target}, not to {expected}. Change the CNAME value.",
    "cname_proxied": "{h} seems to be proxied through a CDN (for example Cloudflare's orange cloud). Set it to “DNS only”.",
    "txt_missing": "The verification TXT record ({name}) wasn't found yet.",
    "txt_mismatch": "The TXT record at {name} has a different value. It must be exactly: {value}",
    "caa_blocks": "Your domain's CAA records don't allow our certificate issuer ({cas}). Add a CAA record allowing one of them.",
    "dns_timeout": "We couldn't reach your domain's DNS servers. We'll keep trying.",
    "cert_failed": "The SSL certificate couldn't be issued or has stopped working: {detail}",
    "cert_expiring": "The SSL certificate expires soon and hasn't renewed.",
}


def _fail(code: str, **kw) -> CheckResult:
    return CheckResult("failed", code, FAILURES[code].format(**kw))


TlsProbe = Callable[[str], tuple[bool, datetime | None, str | None]]


def live_tls_probe(hostname: str) -> tuple[bool, datetime | None, str | None]:
    """Connect to the edge with SNI=hostname and validate the certificate."""
    cfg = get_settings()
    addr = cfg.edge_probe_address or f"{cfg.platform_target_hostname}:443"
    host, _, port = addr.rpartition(":")
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection((host, int(port or 443)), timeout=8) as sock:
            with ctx.wrap_socket(sock, server_hostname=hostname) as tls:
                cert = tls.getpeercert()
        expires = datetime.fromtimestamp(ssl.cert_time_to_seconds(cert["notAfter"]), tz=timezone.utc)
        return True, expires, None
    except (ssl.SSLError, ssl.CertificateError, OSError) as e:
        return False, None, str(e)[:200]


def evaluate(hostname: str, token: str, current_status: str, view: DnsView,
             tls: TlsProbe | None = None, *, now: datetime | None = None) -> CheckResult:
    """Pure state transition from DNS (+ TLS) observations."""
    cfg = get_settings()
    now = now or datetime.now(timezone.utc)
    expected = cfg.platform_target_hostname.lower().rstrip(".")
    if view.cname_error == "timeout":
        return CheckResult(current_status if current_status != "pending" else "pending", "dns_timeout",
                           FAILURES["dns_timeout"])
    if view.cname is None:
        return _fail("cname_missing", h=hostname)
    if view.cname != expected:
        if any(k in view.cname for k in ("cloudflare", "cdn", "akamai", "fastly")):
            return _fail("cname_proxied", h=hostname)
        return _fail("cname_elsewhere", h=hostname, target=view.cname, expected=expected)
    want = txt_value(token)
    if not view.txt:
        return _fail("txt_missing", name=txt_name(hostname))
    if want not in [t.strip().strip('"') for t in view.txt]:
        return _fail("txt_mismatch", name=txt_name(hostname), value=want)
    cas = [c.strip() for c in cfg.acme_ca_domains.split(",") if c.strip()]
    issue = [v.split(";")[0].strip().lower() for tag, v in view.caa if tag in ("issue", "issuewild")]
    if view.caa and issue and not any(ca in issue for ca in cas):
        return _fail("caa_blocks", cas=", ".join(cas))

    # DNS verified. Certificate step.
    if cfg.tls_mode == "off":
        return CheckResult("live")                       # development only
    probe = tls or live_tls_probe
    ok, expires, err = probe(hostname)
    if ok:
        if expires and expires - now < timedelta(days=7):
            return _fail("cert_expiring")
        return CheckResult("live", cert_expires_at=expires)
    if current_status in ("pending", "failed", "verified"):
        return CheckResult("ssl_issuing")                 # first probe triggers on-demand issuance
    if current_status == "ssl_issuing":
        return CheckResult("ssl_issuing")                 # keep waiting; alert after a grace period (caller)
    return _fail("cert_failed", detail=err or "unknown error")


def check_interval(status: str, since: datetime | None, now: datetime) -> timedelta:
    """How often the scheduler re-checks a domain in each state."""
    if status in ("pending", "verified", "ssl_issuing", "failed"):
        young = since is None or now - since < timedelta(hours=1)
        return timedelta(minutes=1 if young else 10)
    return timedelta(minutes=30)                          # live: monitor for removal / cert problems
