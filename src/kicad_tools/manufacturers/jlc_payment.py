"""Opt-in, attended, exact-total-approved JLCPCB checkout leg (issue #8935).

This is the ONLY module in kicad-tools that can press a pay button. It is
reached from exactly one place, ``kct submit-jlcpcb --website --checkout``,
after the website driver (:mod:`.jlc_site_driver`) has saved the cart behind
its own gates (stock gate, tapeout release sign-off, factory match, fab-review
sign-off). Nothing else imports it; a test enforces that.

Authorization (operator rulings on #8935, 2026-11-16 and 2026-11-17): kct may
request the fab's ``/user-center/`` and cart host **only inside an attended
session in which the operator approves the exact total for that order**. No
monthly budget, no unattended, scheduled, background or batch checkout, and
nothing that can pay without an approval naming that order's exact total.

Safety properties (each covered by a test in ``tests/test_jlc_payment.py``):

1. **Exact-total approval.** The approval is a :class:`Money` (ISO currency
   code + decimal amount). It is compared, to the cent and currency, with the
   total read from the payment step *after* the payment method is chosen (so
   shipping, tax, fees and coupons are in it), and again immediately before the
   pay click. Any mismatch raises :class:`CheckoutAbort` naming both numbers;
   nothing is clicked and nothing is recorded.
2. **Attended only.** :func:`require_attended_checkout` refuses without an
   interactive foreground terminal on stdin/stdout/stderr, and refuses when any
   CI, scheduler, Loom, agent-harness or MCP marker is in the environment (see
   :data:`UNATTENDED_ENV_MARKERS`, :data:`UNATTENDED_ENV_PREFIXES`). The
   operator must also type ``checkout`` to start.
3. **Card data.** Optional; the saved site payment method is the default. A
   card file must be a regular file (not a symlink) owned by the user, mode
   exactly 600, and outside every git repository (:func:`check_card_file`). It
   is read only at the fill step (:func:`load_card`) into a :class:`CardSecret`
   that redacts itself under ``str``/``repr``/``format`` and refuses
   pickling/JSON. Values are typed only into the fab's own page, never into the
   environment, process arguments, logs, receipts, ledger, screenshots (none
   are taken once a card value is in the page) or exception messages.
4. **``ordered`` only from a confirmation page.** After the single pay click,
   the order is recorded ``ordered`` only if the page is on a confirmation
   path, shows the confirmation marker, a well-formed order id and a charged
   total equal to the approval. Anything else records ``payment-unconfirmed``,
   prints a check-your-account instruction and is never retried.
5. **Idempotence.** :class:`.jlc_ledger.OrderLedger` is checked first; a plan
   already ``ordered`` (or with an unresolved attempt) stops before any click.
   ``payment-attempted`` is fsynced before the click.
6. **Request scope.** UI clicks only, one at a time, at human pace
   (:data:`HUMAN_PACE_S` between actions), no navigation, no HTTP client, no
   retries, no polling loops. URLs are checked after every step against
   :data:`CHECKOUT_PATH_PREFIXES` on the allowed hosts; robots.txt-disallowed
   API and export paths still abort.

UNVERIFIED against the live site: every selector in :data:`CHECKOUT_SELECTORS`,
the checkout and confirmation paths, the order-id pattern and the money
formats. While :data:`CHECKOUT_SELECTORS_VERIFIED` is ``False`` the leg stops
at each step and asks the operator to confirm the page matches before it acts.
The documented first run is a rehearsal (``--checkout`` without ``--pay``),
which goes as far as the approved total and stops before the pay button.
"""

from __future__ import annotations

import contextlib
import os
import re
import stat
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from .jlc_ledger import LedgerError, OrderLedger, describe_block
from .jlc_site_driver import ALLOWED_HOSTS, DriverAbort, SitePage

CHECKOUT_VERSION = "jlc-checkout-1"

#: Flip to True only after an attended run has confirmed every selector, path
#: and format below against the live site (and recorded fixtures, #8478).
CHECKOUT_SELECTORS_VERIFIED = False

#: UNVERIFIED placeholders, in the style of ``jlc_site_driver.SELECTORS``.
CHECKOUT_SELECTORS: dict[str, str] = {
    "checkout_button": "[data-kct=checkout]",
    "cart_items": "[data-kct=cart-items]",
    "saved_payment_method": "[data-kct=saved-payment-method]",
    "card_name": "input[data-kct=card-name]",
    "card_number": "input[data-kct=card-number]",
    "card_expiry": "input[data-kct=card-expiry]",
    "card_cvc": "input[data-kct=card-cvc]",
    "order_total": "[data-kct=order-total]",
    "currency": "[data-kct=currency]",
    "pay_button": "[data-kct=pay]",
    "order_confirmed": "[data-kct=order-confirmed]",
    "order_number": "[data-kct=order-number]",
    "charged_total": "[data-kct=charged-total]",
}

#: Paths the checkout leg may be on (UNVERIFIED). ``/user-center/`` is allowed
#: here and only here, under the 2026-11-17 ruling.
CHECKOUT_PATH_PREFIXES = ("/cart", "/shoppingCart", "/user-center/")
#: Paths a completed-payment confirmation page may be on (UNVERIFIED).
CONFIRMATION_PATH_PREFIXES = ("/user-center/order", "/user-center/pay-success", "/pay/success")
#: robots.txt-disallowed prefixes that stay forbidden even during checkout.
STILL_DISALLOWED = (
    "/api",
    "/smtOrder/downloadNoMatchBomList",
    "/shoppingCart/smtGood/downloadNoMatchBomList",
)
#: A JLC order id as shown on the confirmation page (UNVERIFIED pattern).
ORDER_ID_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9-]{5,39}$")

#: Seconds between actions: a person reads the page before clicking.
HUMAN_PACE_S = 1.5

JLC_TERMS = (
    "JLCPCB terms: an order is charged when you pay; in-stock and global-sourcing parts "
    "cannot be cancelled once paid, and pre-order parts cannot be cancelled once quoted."
)

#: Any of these in the environment means "not an attended operator terminal".
UNATTENDED_ENV_MARKERS = (
    "CI",
    "CONTINUOUS_INTEGRATION",
    "BUILD_ID",
    "BUILD_NUMBER",
    "GITHUB_ACTIONS",
    "GITLAB_CI",
    "BUILDKITE",
    "CIRCLECI",
    "TRAVIS",
    "JENKINS_URL",
    "TF_BUILD",
    "TEAMCITY_VERSION",
    "APPVEYOR",
    "DRONE",
    "CODEBUILD_BUILD_ID",
    "BITBUCKET_BUILD_NUMBER",
    # schedulers / service managers
    "INVOCATION_ID",
    "JOURNAL_STREAM",
    # agent harnesses and MCP servers
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "MCP_SERVER",
    "KCT_MCP",
    "PYTEST_CURRENT_TEST",
)
UNATTENDED_ENV_PREFIXES = ("LOOM_", "CODEX_", "OPENCODE", "SQUAD_", "MCP_", "KCT_MCP")


class CheckoutAbort(DriverAbort):
    """Stopped before the pay click; nothing was paid and nothing recorded."""


class PaymentUnconfirmed(DriverAbort):
    """The pay click happened (or may have) but no confirmation page was recognised."""


class CheckoutPage(SitePage, Protocol):
    def fill(self, selector: str, value: str) -> None: ...


# --- attended --------------------------------------------------------------


def unattended_reasons(
    env: Mapping[str, str] | None = None,
    *,
    streams: Sequence[Any] | None = None,
    foreground: Callable[[], bool] | None = None,
) -> list[str]:
    """Every reason this process is not an attended operator terminal (empty = attended)."""
    env = os.environ if env is None else env
    reasons = []
    for name in UNATTENDED_ENV_MARKERS:
        if name in env:
            reasons.append(f"environment variable {name} is set")
    for name in sorted(env):
        if name.startswith(UNATTENDED_ENV_PREFIXES) and name not in UNATTENDED_ENV_MARKERS:
            reasons.append(f"environment variable {name} is set")
    for stream in streams if streams is not None else (sys.stdin, sys.stdout, sys.stderr):
        try:
            tty = bool(stream.isatty())
        except (AttributeError, ValueError, OSError):
            tty = False
        if not tty:
            reasons.append(f"{getattr(stream, 'name', 'a standard stream')} is not a terminal")
    if not reasons:
        check = foreground or _is_foreground
        if not check():
            reasons.append("the process is not in the terminal's foreground process group")
    return reasons


def _is_foreground() -> bool:
    try:
        return os.tcgetpgrp(sys.stdin.fileno()) == os.getpgrp()
    except (OSError, ValueError, AttributeError):
        return False


def require_attended_checkout(**kwargs: Any) -> None:
    reasons = unattended_reasons(**kwargs)
    if reasons:
        raise CheckoutAbort(
            "attended",
            "the checkout leg runs only in your own interactive terminal: " + "; ".join(reasons),
        )


# --- money -----------------------------------------------------------------

_SYMBOLS = {"US$": "USD", "€": "EUR", "£": "GBP"}
_DOLLAR_CURRENCIES = ("USD", "CAD", "AUD", "NZD", "SGD", "HKD")
_ISO = re.compile(r"\b([A-Z]{3})\b")
_AMOUNT = re.compile(r"(?<![\d.,])(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?(?![\d.,])")


@dataclass(frozen=True)
class Money:
    currency: str
    amount: Decimal

    def __str__(self) -> str:
        return f"{self.currency} {self.amount:.2f}"


class MoneyError(ValueError):
    pass


def _amount(text: str) -> Decimal:
    found = _AMOUNT.findall(text)
    if len(found) != 1:
        raise MoneyError(f"expected exactly one amount in {text!r}")
    whole, frac = found[0]
    try:
        value = Decimal(whole.replace(",", "") + (f".{frac}" if frac else ""))
    except InvalidOperation:
        raise MoneyError(f"unreadable amount in {text!r}") from None
    if frac and len(frac) > 2:
        raise MoneyError(f"more than two decimals in {text!r}")
    return value


def parse_approval(text: str) -> Money:
    """An operator approval: an ISO currency code and an amount, e.g. ``USD 123.45``."""
    text = text.strip()
    codes = _ISO.findall(text.upper())
    if len(codes) != 1 or codes[0] not in text:
        raise MoneyError(
            "an approval must name the currency code and the exact amount, e.g. 'USD 123.45'"
        )
    return Money(codes[0], _amount(text))


def parse_page_money(text: str, page_currency: str = "") -> Money:
    """A total as the page shows it. A bare ``$`` needs the page's ISO currency indicator."""
    text = text.strip()
    codes = set(_ISO.findall(text))
    indicator = page_currency.strip().upper()
    if indicator and not re.fullmatch(r"[A-Z]{3}", indicator):
        raise MoneyError(f"unreadable currency indicator {page_currency!r}")
    for symbol, code in _SYMBOLS.items():
        if symbol in text:
            codes.add(code)
    if not codes and "$" in text:
        if indicator not in _DOLLAR_CURRENCIES:
            raise MoneyError(f"'$' total {text!r} with no dollar currency indicator")
        codes.add(indicator)
    if indicator:
        codes.add(indicator)
    if len(codes) != 1:
        raise MoneyError(f"cannot tell the currency of {text!r} (indicator {indicator!r})")
    return Money(codes.pop(), _amount(text))


# --- card file -------------------------------------------------------------

CARD_KEYS = ("KCT_CARD_NAME", "KCT_CARD_NUMBER", "KCT_CARD_EXPIRY", "KCT_CARD_CVC")
_REQUIRED_CARD_KEYS = CARD_KEYS[:3]


class CardFileError(CheckoutAbort):
    def __init__(self, reason: str):
        super().__init__("card-file", reason)


def _inside_git_repo(path: Path) -> Path | None:
    for directory in (path.parent, *path.parent.parents):
        if (directory / ".git").exists():
            return directory
    return None


def check_card_file(path: Path, *, getuid: Callable[[], int] = os.getuid) -> Path:
    """Refuse a card file that could be shared, committed or swapped. Reads no content."""
    raw = Path(path).expanduser()
    if not raw.is_absolute():
        raise CardFileError("the card file path must be absolute")
    try:
        info = raw.lstat()
    except OSError:
        raise CardFileError(f"cannot stat card file {raw}") from None
    if stat.S_ISLNK(info.st_mode):
        raise CardFileError(f"card file {raw} is a symlink; point at the real file")
    if not stat.S_ISREG(info.st_mode):
        raise CardFileError(f"card file {raw} is not a regular file")
    if info.st_uid != getuid():
        raise CardFileError(f"card file {raw} is not owned by you")
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise CardFileError(
            f"card file {raw} has mode {stat.S_IMODE(info.st_mode):03o}; run chmod 600 on it"
        )
    repo = _inside_git_repo(raw.resolve())
    if repo is not None:
        raise CardFileError(
            f"card file {raw} is inside the git repository {repo}; move it outside every "
            "repository (for example ~/.kct/card.env)"
        )
    return raw


class CardSecret:
    """Card fields; never shown by ``str``/``repr``/``format``, never pickled or serialised."""

    __slots__ = ("_values",)
    _values: dict[str, str]

    def __init__(self, values: Mapping[str, str]):
        object.__setattr__(self, "_values", dict(values))

    def reveal(self, key: str) -> str:
        return str(self._values.get(key, ""))

    def has(self, key: str) -> bool:
        return bool(self._values.get(key))

    def wipe(self) -> None:
        self._values.clear()

    def __repr__(self) -> str:
        return "CardSecret(<redacted>)"

    __str__ = __repr__

    def __format__(self, spec: str) -> str:
        return repr(self)

    def __reduce__(self):  # pickling / copy
        raise TypeError("CardSecret cannot be serialised")

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("CardSecret is read-only")


def _luhn_ok(number: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(number)):
        digit = int(char)
        if index % 2:
            digit *= 2
            digit -= 9 if digit > 9 else 0
        total += digit
    return total % 10 == 0


def load_card(path: Path, *, getuid: Callable[[], int] = os.getuid) -> CardSecret:
    """Re-check and read the card file now (at the moment of use). Errors name keys only."""
    from ..parts.jlc_env import parse_env_text

    checked = check_card_file(path, getuid=getuid)
    try:
        text = checked.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise CardFileError(f"cannot read card file {checked}") from None
    parsed = parse_env_text(text)
    del text
    values = {key: parsed.get(key, "").strip() for key in CARD_KEYS}
    parsed.clear()
    missing = [key for key in _REQUIRED_CARD_KEYS if not values[key]]
    if missing:
        raise CardFileError(f"card file {checked} is missing {', '.join(missing)}")
    number = values["KCT_CARD_NUMBER"].replace(" ", "").replace("-", "")
    if not (number.isdigit() and 12 <= len(number) <= 19 and _luhn_ok(number)):
        raise CardFileError(f"KCT_CARD_NUMBER in {checked} is not a valid card number")
    values["KCT_CARD_NUMBER"] = number
    if not re.fullmatch(r"(0[1-9]|1[0-2])\s*/\s*(\d{2}|\d{4})", values["KCT_CARD_EXPIRY"]):
        raise CardFileError(f"KCT_CARD_EXPIRY in {checked} is not MM/YY")
    cvc = values["KCT_CARD_CVC"]
    if cvc and not re.fullmatch(r"\d{3,4}", cvc):
        raise CardFileError(f"KCT_CARD_CVC in {checked} is not 3 or 4 digits")
    return CardSecret(values)


# --- checkout run ----------------------------------------------------------


@dataclass(frozen=True)
class Approval:
    """``--approve-total`` (``total`` set) or ``--auto`` (``total`` None: type it at the prompt)."""

    total: Money | None = None

    @property
    def mode(self) -> str:
        return "approve-total" if self.total is not None else "auto"


@dataclass
class CheckoutResult:
    state: str  # rehearsed | ordered
    plan_sha256: str
    approved_total: str
    order_id: str = ""
    charged_total: str = ""
    payment_method: str = "saved"
    steps: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "checkout_version": CHECKOUT_VERSION,
            "selectors_verified": CHECKOUT_SELECTORS_VERIFIED,
            "state": self.state,
            "plan_sha256": self.plan_sha256,
            "approved_total": self.approved_total,
            "order_id": self.order_id,
            "charged_total": self.charged_total,
            "payment_method": self.payment_method,
            "steps": list(self.steps),
        }


class _Checkout:
    def __init__(
        self,
        page: CheckoutPage,
        shots: Path,
        ask: Callable[[str], str],
        say: Callable[[str], None],
        sleep: Callable[[float], None],
    ):
        self.page = page
        self.shots = shots
        self.ask = ask
        self.say = say
        self.sleep = sleep
        self.steps: list[dict[str, Any]] = []
        #: Once a card value is in the page, no more screenshots (they would capture it).
        self.secret_in_page = False
        shots.mkdir(parents=True, exist_ok=True)

    def shot(self, name: str) -> str:
        if self.secret_in_page:
            return ""
        path = self.shots / f"checkout-{len(self.steps) + 1:02d}-{name}.png"
        try:
            self.page.screenshot(path)
        except Exception:
            return ""
        return path.name

    def check_url(self, step: str, prefixes: Sequence[str] = CHECKOUT_PATH_PREFIXES) -> None:
        parsed = urlparse(self.page.url)
        if parsed.hostname not in ALLOWED_HOSTS:
            raise CheckoutAbort(step, f"page left the allowed hosts: {parsed.hostname}")
        if any(parsed.path.startswith(p) for p in STILL_DISALLOWED):
            raise CheckoutAbort(step, f"page is on a robots.txt-disallowed path: {parsed.path}")
        if not any(parsed.path.startswith(p) for p in prefixes):
            raise CheckoutAbort(step, f"page is not on an expected checkout path: {parsed.path}")

    def need(self, key: str) -> str:
        selector = CHECKOUT_SELECTORS[key]
        if not self.page.exists(selector):
            raise CheckoutAbort(key, f"selector missing: {selector}")
        return selector

    def confirm_unverified(self, step: str, expectation: str) -> None:
        if CHECKOUT_SELECTORS_VERIFIED:
            return
        answer = self.ask(
            f"[unverified step: {step}] {expectation}\n"
            "Does the browser show exactly that? Type 'y' to continue, anything else stops: "
        )
        if answer.strip().lower() != "y":
            raise CheckoutAbort(step, "stopped by the operator at an unverified step")

    def step(
        self, name: str, action: Callable[[], dict[str, Any] | None], *, secret: bool = False
    ) -> dict[str, Any]:
        self.sleep(HUMAN_PACE_S)
        try:
            self.check_url(name)
            detail = action() or {}
            self.page.wait_settled()
            self.check_url(name)
        except DriverAbort:
            raise
        except Exception as exc:
            reason = (
                "the page did not accept the step (details withheld: card data may be involved)"
                if secret or self.secret_in_page
                else f"{type(exc).__name__}: {exc}"
            )
            raise CheckoutAbort(name, reason, self.shot(f"abort-{name}")) from None
        record = {"name": name, "screenshot": self.shot(name), "detail": detail}
        self.steps.append(record)
        return detail

    def read_total(self, key: str = "order_total") -> Money:
        text = self.page.text(self.need(key))
        currency = ""
        if self.page.exists(CHECKOUT_SELECTORS["currency"]):
            currency = self.page.text(CHECKOUT_SELECTORS["currency"])
        try:
            return parse_page_money(text, currency)
        except MoneyError as exc:
            raise CheckoutAbort(key, str(exc)) from None


def run_checkout(
    page: CheckoutPage,
    *,
    plan_sha256: str,
    approval: Approval,
    ledger: OrderLedger,
    ask: Callable[[str], str],
    say: Callable[[str], None],
    shots_dir: Path,
    pay: bool = False,
    card_file: Path | None = None,
    expected_cart_lines: int = 1,
    attended: Callable[[], None] = require_attended_checkout,
    sleep: Callable[[float], None] = time.sleep,
) -> CheckoutResult:
    """From the saved cart to the approved total, then (only with ``pay``) one pay click.

    Raises :class:`CheckoutAbort` for any stop before the pay click and
    :class:`PaymentUnconfirmed` for a click without a recognised confirmation.
    """
    attended()
    try:
        status = ledger.plan_status(plan_sha256)
    except LedgerError as exc:
        raise CheckoutAbort("ledger", str(exc)) from None
    if status.state != "not-ordered":
        raise CheckoutAbort("ledger", describe_block(status, ledger))
    start = ask("Attended checkout for this plan. Type 'checkout' to start (anything else stops): ")
    if start.strip() != "checkout":
        raise CheckoutAbort("start", "the operator did not start the checkout")

    run = _Checkout(page, Path(shots_dir), ask, say, sleep)
    run.confirm_unverified("open-checkout", "The saved cart is shown with a checkout button.")
    run.step("open-checkout", lambda: page.click(run.need("checkout_button")))

    def check_lines() -> dict[str, Any]:
        lines = [str(x).strip() for x in page.options(run.need("cart_items"))]
        if len(lines) != expected_cart_lines:
            raise CheckoutAbort(
                "cart-lines",
                f"checkout holds {len(lines)} line(s), expected {expected_cart_lines}; "
                "remove anything that is not this order from the cart",
            )
        return {"lines": len(lines)}

    run.confirm_unverified("cart-lines", "Checkout lists only this board's PCB/PCBA order.")
    run.step("cart-lines", check_lines)

    method = "saved"
    if card_file is None:

        def saved() -> dict[str, Any]:
            selector = run.need("saved_payment_method")
            page.click(selector)
            if not page.value(selector).strip():
                raise CheckoutAbort("payment-method", "no saved payment method is selected")
            return {"method": "saved"}

        run.confirm_unverified("payment-method", "Your saved payment method is offered.")
        run.step("payment-method", saved)
    else:
        method = "card-file"
        check_card_file(card_file)  # fail before the page is touched, without reading it
        run.confirm_unverified("card-form", "The card form on jlcpcb.com is shown.")

        def fill_card() -> dict[str, Any]:
            card = load_card(card_file)
            try:
                for key, selector_key in (
                    ("KCT_CARD_NAME", "card_name"),
                    ("KCT_CARD_NUMBER", "card_number"),
                    ("KCT_CARD_EXPIRY", "card_expiry"),
                    ("KCT_CARD_CVC", "card_cvc"),
                ):
                    if key == "KCT_CARD_CVC" and not card.has(key):
                        continue
                    selector = run.need(selector_key)
                    run.secret_in_page = True
                    page.fill(selector, card.reveal(key))
                need_cvc = not card.has("KCT_CARD_CVC")
            finally:
                card.wipe()
                del card
            return {"method": "card-file", "cvc_typed_by_operator": need_cvc}

        detail = run.step("card-form", fill_card, secret=True)
        if detail.get("cvc_typed_by_operator"):
            answer = ask(
                "Type the security code into the browser's card form yourself, then type "
                "'done' here (anything else stops): "
            )
            if answer.strip() != "done":
                raise CheckoutAbort("card-cvc", "stopped by the operator")

    run.confirm_unverified(
        "read-total", "The final total (with shipping, tax, fees and coupons) is shown."
    )
    totals: list[Money] = []

    def read_quote() -> dict[str, Any]:
        totals.append(run.read_total())
        return {"total": str(totals[0])}

    run.step("read-total", read_quote)
    quoted = totals[0]
    say(f"Final total on the payment step: {quoted}")
    say(JLC_TERMS)

    if approval.total is None:
        typed = ask(
            f"To approve paying {quoted}, type that exact total (currency and amount); "
            "anything else stops: "
        )
        try:
            approved = parse_approval(typed)
        except MoneyError:
            raise CheckoutAbort("approval", f"no approval given for {quoted}") from None
    else:
        approved = approval.total
    if approved != quoted:
        raise CheckoutAbort(
            "approval", f"the page total {quoted} does not match the approval {approved}"
        )

    if not pay:
        run.need("pay_button")  # a rehearsal reaches the pay button, and stops there
        say(
            f"REHEARSAL: stopped before the pay button with {approved} approved. "
            "Nothing was paid. Re-run with --pay to pay."
        )
        return CheckoutResult("rehearsed", plan_sha256, str(approved), "", "", method, run.steps)

    run.confirm_unverified("pay", f"The pay button will charge {approved}.")
    # Re-read immediately before the click, after any operator pause.
    run.sleep(HUMAN_PACE_S)
    final = run.read_total()
    if final != approved:
        raise CheckoutAbort(
            "re-check-total",
            f"the total changed to {final} after {approved} was approved; nothing was paid",
        )
    pay_selector = run.need("pay_button")
    run.check_url("pay")

    try:
        ledger.append(
            "payment-attempted",
            plan_sha256,
            kind="pcb-pcba",
            approved_total=str(approved),
            driver_version=CHECKOUT_VERSION,
        )
    except LedgerError as exc:
        raise CheckoutAbort("ledger", f"{exc}; nothing was paid") from None

    def unconfirmed(reason: str) -> PaymentUnconfirmed:
        # "payment-attempted" is already on disk and blocks a re-run even if this fails.
        with contextlib.suppress(LedgerError):
            ledger.append(
                "payment-unconfirmed",
                plan_sha256,
                kind="pcb-pcba",
                approved_total=str(approved),
                step="confirmation",
                reason=reason,
            )
        return PaymentUnconfirmed(
            "confirmation",
            f"{reason}. The order is NOT recorded as ordered. Check your JLCPCB account "
            "by hand before doing anything else; this tool will not retry the payment",
            run.shot("unconfirmed"),
        )

    try:
        page.click(pay_selector)  # the one irreversible action; never repeated
        page.wait_settled()
    except Exception:
        raise unconfirmed("the pay click raised an error, so its effect is unknown") from None

    parsed = urlparse(page.url)
    if parsed.hostname not in ALLOWED_HOSTS or not any(
        parsed.path.startswith(p) for p in CONFIRMATION_PATH_PREFIXES
    ):
        raise unconfirmed(f"the page after paying is not a confirmation page ({parsed.path})")
    try:
        if not page.exists(CHECKOUT_SELECTORS["order_confirmed"]):
            raise unconfirmed("the confirmation marker is missing")
        order_id = page.text(CHECKOUT_SELECTORS["order_number"]).strip()
        charged = run.read_total("charged_total")
    except DriverAbort as exc:
        if isinstance(exc, PaymentUnconfirmed):
            raise
        raise unconfirmed(f"the confirmation page could not be read ({exc.reason})") from None
    except Exception:
        raise unconfirmed("the confirmation page could not be read") from None
    if not ORDER_ID_PATTERN.fullmatch(order_id):
        raise unconfirmed("no well-formed order number on the confirmation page")
    if charged != approved:
        raise unconfirmed(f"the confirmation shows {charged}, not the approved {approved}")
    try:
        ledger.append(
            "ordered",
            plan_sha256,
            kind="pcb-pcba",
            approved_total=str(approved),
            charged_total=str(charged),
            order_id=order_id,
            driver_version=CHECKOUT_VERSION,
        )
    except LedgerError as exc:
        raise PaymentUnconfirmed(
            "ledger",
            f"JLC confirmed order {order_id} charged {charged}, but the ledger could not "
            f"record it ({exc}). Write that down; the earlier attempt record still blocks a "
            "second payment for this plan",
        ) from None
    run.steps.append({"name": "confirmation", "screenshot": run.shot("confirmation"), "detail": {}})
    return CheckoutResult(
        "ordered", plan_sha256, str(approved), order_id, str(charged), method, run.steps
    )
