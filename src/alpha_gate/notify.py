"""Single alert sender for the trading bot — posts to the AIQSO /notify router.

WHY
---
Six scripts each built their own Telegram sendMessage call, with six formats and
a hardcoded bot token. That meant no dedup, no topic routing, no delivery
receipt, and a token in source control. This module replaces all of them.

THE ROUTER CONTRACT IS MEASURED — DO NOT RE-DERIVE IT
    - The envelope keys off `domain`, NOT a thread id. Posting {"topic": 169}
      is not the contract; the router maps (domain, class) -> thread itself.
    - `X-Notify-Token` is required and comes from a FILE, deliberately, so the
      call still resolves under cron's empty environment:
          /etc/alertmanager/notify_token.secret
          else WEBHOOK_AUTH_TOKEN in /etc/n8n/n8n.env
      A wrong token returns 401 {"error":"unauthorized"}.
    - Success is 200 {"routed":true,...}
    - A 10-minute dedup window returns 200 {"routed":false,"deduped":true} and
      that IS a delivered outcome — the operator was already told inside the
      window. Treating it as failure makes an alerter report "not delivered"
      precisely when it demonstrably was. Accept routed OR deduped.

CLASSES — this is the noise control
    page   -> wake somebody up. Errors and failed orders only.
    ticket -> a state change worth reading now (order filled, threshold cross).
    digest -> routine/heartbeat. Queued and flushed hourly as ONE grouped
              message per domain, with duplicates collapsed to xN.
    Routine polling output belongs in `digest`, never `ticket`.

⚠️ DOMAIN "trading" IS NOT LIVE YET. The router's table currently knows only
infra|security|automation|social|clients|business, and an unmapped domain has no
documented fallback — it may be silently dropped. Do not switch the six scripts
over until the `trading` domain exists in workflow 9Q3SNpClEImc4MTe and the
Telegram topic is created. That workflow is under drift detection, so the change
must go re-export -> deploy-expected.sh -> drift-check.sh --snapshot on 232 AND 370.
"""

import json
import os
import urllib.error
import urllib.request

ROUTER_URL = os.environ.get("NOTIFY_ROUTER_URL", "http://localhost:5678/webhook/notify")
DOMAIN = os.environ.get("NOTIFY_DOMAIN", "trading")
SOURCE = os.environ.get("NOTIFY_SOURCE", "mexc-trading-bot")

TOKEN_FILES = (
    "/etc/alertmanager/notify_token.secret",
    "/etc/n8n/n8n.env",  # parsed for WEBHOOK_AUTH_TOKEN=
)


class NotifyError(RuntimeError):
    """Raised when an alert could not be delivered."""


def read_token():
    """Resolve the router token from disk. Returns the token, or raises."""
    path = TOKEN_FILES[0]
    if os.path.exists(path):
        with open(path) as fh:
            tok = fh.read().strip()
            if tok:
                return tok
    envfile = TOKEN_FILES[1]
    if os.path.exists(envfile):
        with open(envfile) as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("WEBHOOK_AUTH_TOKEN="):
                    tok = line.split("=", 1)[1].strip().strip("'\"")
                    if tok:
                        return tok
    raise NotifyError(
        "no router token — expected /etc/alertmanager/notify_token.secret or "
        "WEBHOOK_AUTH_TOKEN in /etc/n8n/n8n.env"
    )


def build_envelope(title, body, cls="ticket", domain=None, source=None):
    """The measured envelope. Kept separate from the POST so it can be tested."""
    if cls not in ("page", "ticket", "digest", "log"):
        raise ValueError(f"class must be page|ticket|digest|log, got {cls!r}")
    return {
        "domain": domain or DOMAIN,
        "class": cls,
        "title": title,
        "body": body,
        "source": source or SOURCE,
    }


def interpret(status, payload):
    """Decide delivered/undelivered from the router's reply.

    Split out because this is the part that is easy to get wrong: a 200 with
    deduped:true is DELIVERED. Returns (delivered: bool, detail: str).
    """
    if status == 401:
        return False, "401 unauthorized — wrong X-Notify-Token"
    if status != 200:
        return False, f"HTTP {status}"
    if not isinstance(payload, dict):
        return False, f"unparseable router reply: {payload!r}"
    if payload.get("routed") is True:
        return True, f"routed to thread {payload.get('thread', '?')}"
    if payload.get("deduped") is True:
        return True, f"deduped inside {payload.get('window', '10m')} — already delivered"
    return False, f"router did not route: {payload}"


def send(title, body, cls="ticket", domain=None, source=None, timeout=10):
    """Post one alert. Returns a detail string, or raises NotifyError."""
    envelope = build_envelope(title, body, cls, domain, source)
    data = json.dumps(envelope).encode()
    req = urllib.request.Request(
        ROUTER_URL,
        data=data,
        headers={"Content-Type": "application/json", "X-Notify-Token": read_token()},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    except Exception as exc:
        raise NotifyError(f"router unreachable at {ROUTER_URL}: {exc}") from exc

    try:
        payload = json.loads(raw.decode() or "{}")
    except json.JSONDecodeError:
        payload = raw.decode()[:200]

    delivered, detail = interpret(status, payload)
    if not delivered:
        raise NotifyError(detail)
    return detail


def trade_alert(action, symbol, qty=None, price=None, pnl=None, cls="ticket", note=None):
    """The one message format all six scripts should use."""
    title = f"{action} {symbol}"
    parts = []
    if qty is not None:
        parts.append(f"qty={qty}")
    if price is not None:
        parts.append(f"price={price}")
    if pnl is not None:
        parts.append(f"P/L={pnl}")
    if note:
        parts.append(note)
    return send(title, " · ".join(parts) if parts else action, cls=cls)
