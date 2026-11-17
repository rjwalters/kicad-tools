"""Opt-in attended checkout leg (issue #8935): the six safety properties.

Every page here is SYNTHETIC (a fake in this file), not a recorded JLC page:
these tests prove the approval, attended, card, ledger and confirmation logic,
not the live selectors. No test opens a network connection, attaches to a
browser, or can spend money: the real attended check refuses under pytest
(``PYTEST_CURRENT_TEST``), and every run below injects a fake page.
"""

from __future__ import annotations

import ast
import copy
import json
import logging
import os
import traceback
from decimal import Decimal
from pathlib import Path

import pytest

from kicad_tools.manufacturers import jlc_payment as pay
from kicad_tools.manufacturers.jlc_ledger import OrderLedger

S = pay.CHECKOUT_SELECTORS
PLAN = "p" * 64
CARD_NUMBER = "4111111111111111"
CARD_NAME = "SENTINEL-8935 CARDHOLDER"
CARD_CVC = "987"
CART = "https://cart.jlcpcb.com/shoppingCart"
CHECKOUT = "https://jlcpcb.com/user-center/checkout"
CONFIRMED = "https://jlcpcb.com/user-center/order/success"

# --- fakes -----------------------------------------------------------------


def _state(total="$123.45", charged="$123.45"):
    return {
        "url": CART,
        "elements": {
            S["checkout_button"]: {},
            S["cart_items"]: {"options": ["PCB+PCBA gerbers.zip x10"]},
            S["saved_payment_method"]: {"value": "Visa ending 0000"},
            S["card_name"]: {},
            S["card_number"]: {},
            S["card_expiry"]: {},
            S["card_cvc"]: {},
            S["order_total"]: {"text": total},
            S["currency"]: {"text": "USD"},
            S["pay_button"]: {},
            S["order_confirmed"]: {},
            S["order_number"]: {"text": "Y2-1234567A"},
            S["charged_total"]: {"text": charged},
        },
    }


class FakeCheckoutPage:
    """Synthetic page. ``screenshot`` dumps the full state, filled values included,
    so a screenshot taken with card data in the page is caught by the leak scan."""

    def __init__(
        self,
        state,
        *,
        after_pay="confirm",
        total_after_quote=None,
        fill_error=False,
        checkout_lands_on=CHECKOUT,
        click_error="",
    ):
        self.checkout_lands_on = checkout_lands_on
        self.click_error = click_error
        self.state = copy.deepcopy(state)
        self.url = state["url"]
        self.log: list[tuple] = []
        self.filled: dict[str, str] = {}
        self.after_pay = after_pay
        self.total_after_quote = total_after_quote
        self.fill_error = fill_error
        self.total_reads = 0

    def exists(self, sel):
        if sel == S["order_confirmed"] and self.after_pay == "no-marker":
            return False
        return sel in self.state["elements"]

    def options(self, sel):
        return self.state["elements"][sel]["options"]

    def select(self, sel, value):  # pragma: no cover - not used by checkout
        raise AssertionError("checkout never selects")

    def value(self, sel):
        return self.state["elements"][sel].get("value", "")

    def text(self, sel):
        if sel == S["order_total"]:
            self.total_reads += 1
            if self.total_after_quote and self.total_reads > 1:
                return self.total_after_quote
        return self.state["elements"][sel]["text"]

    def click(self, sel):
        self.log.append(("click", sel))
        if self.click_error and sel == S["checkout_button"]:
            raise RuntimeError(self.click_error)
        if sel == S["checkout_button"]:
            self.url = self.checkout_lands_on
        elif sel == S["pay_button"]:
            if self.after_pay == "raise":
                raise TimeoutError("navigation timeout")
            if self.after_pay == "raise-after-navigate":
                self.url = CONFIRMED  # the payment went through, then the wait timed out
                raise TimeoutError("navigation timeout")
            if self.after_pay == "redirect":
                self.url = "https://jlcpcb.com/quote"
            elif self.after_pay == "stay":
                pass
            else:
                self.url = CONFIRMED

    def fill(self, sel, value):
        if self.fill_error:
            raise RuntimeError(f"element not editable while typing {value!r}")
        self.log.append(("fill", sel))
        self.filled[sel] = value

    def set_file(self, sel, path):  # pragma: no cover
        raise AssertionError("checkout never uploads")

    def wait_settled(self):
        self.log.append(("settle",))

    def screenshot(self, path):
        path.write_text(json.dumps({"url": self.url, "filled": self.filled}))

    @property
    def clicks(self):
        return [entry[1] for entry in self.log if entry[0] == "click"]


class Answers:
    """Scripted operator: answers 'y' to unverified-step prompts by default."""

    def __init__(
        self, typed_total=None, start="checkout", unverified="y", cvc="done", on_step=None
    ):
        self.on_step = on_step or {}
        self.prompts: list[str] = []
        self.typed_total = typed_total
        self.start = start
        self.unverified = unverified
        self.cvc = cvc

    def __call__(self, prompt):
        self.prompts.append(prompt)
        if prompt.startswith("Attended checkout"):
            return self.start
        if prompt.startswith("[unverified step"):
            step = prompt.split("]")[0].removeprefix("[unverified step: ")
            if step in self.on_step:
                return self.on_step[step]()
            return self.unverified
        if prompt.startswith("To approve"):
            return self.typed_total or ""
        if prompt.startswith("Type the security code"):
            return self.cvc
        raise AssertionError(f"unexpected prompt {prompt!r}")


def _run(tmp_path, page, *, approval="USD 123.45", pay_=True, answers=None, **kw):
    said: list[str] = []
    ledger = kw.pop("ledger", None) or OrderLedger(tmp_path / "ledger")
    result = pay.run_checkout(
        page,
        plan_sha256=PLAN,
        approval=pay.Approval(pay.parse_approval(approval) if approval else None),
        ledger=ledger,
        ask=answers or Answers(),
        say=said.append,
        shots_dir=tmp_path / "out" / "site-screenshots",
        pay=pay_,
        sleep=lambda s: None,
        **kw,
    )
    return result, ledger, said


def _write_card(directory: Path, mode=0o600, cvc=CARD_CVC) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "card.env"
    path.write_text(
        f"KCT_CARD_NAME={CARD_NAME}\nKCT_CARD_NUMBER={CARD_NUMBER}\n"
        f"KCT_CARD_EXPIRY=12/30\nKCT_CARD_CVC={cvc}\n"
    )
    os.chmod(path, mode)
    return path


#: Tests that exercise the REAL attended check (everything else replaces it).
_REAL_ATTENDED = {
    "test_real_attended_check_refuses_under_the_test_suite",
    "test_run_checkout_checks_attended_before_touching_anything",
    "test_run_checkout_has_no_parameter_to_skip_the_attended_check",
}


@pytest.fixture(autouse=True)
def _attended_seam(request, monkeypatch):
    """Test-only seam: the module-level check is replaced; run_checkout has no skip parameter."""
    if request.node.originalname not in _REAL_ATTENDED:
        monkeypatch.setattr(pay, "require_attended_checkout", lambda: None)


@pytest.fixture
def card_dir(tmp_path):
    if pay._inside_git_repo(tmp_path / "x") is not None:
        pytest.skip("tmp_path is inside a git repository on this host")
    return tmp_path / "secrets"


# --- 1. exact-total approval -----------------------------------------------


def test_matching_approval_pays_once_and_records_ordered(tmp_path):
    page = FakeCheckoutPage(_state())
    result, ledger, _ = _run(tmp_path, page)
    assert result.state == "ordered"
    assert result.order_id == "Y2-1234567A" and result.charged_total == "USD 123.45"
    assert page.clicks.count(S["pay_button"]) == 1
    assert ledger.plan_status(PLAN).state == "ordered"


@pytest.mark.parametrize(
    "approval", ["USD 123.44", "USD 123.46", "EUR 123.45", "USD 12.345", "123.45"]
)
def test_mismatched_or_malformed_approval_never_pays(tmp_path, approval):
    page = FakeCheckoutPage(_state())
    ledger = OrderLedger(tmp_path / "ledger")
    with pytest.raises((pay.CheckoutAbort, pay.MoneyError)) as exc:
        _run(tmp_path, page, approval=approval, ledger=ledger)
    assert S["pay_button"] not in page.clicks
    assert not ledger.path.exists()  # nothing recorded
    if isinstance(exc.value, pay.CheckoutAbort):
        assert exc.value.step == "approval"
        assert "USD 123.45" in exc.value.reason and approval in exc.value.reason


def test_auto_requires_typing_the_exact_total(tmp_path):
    page = FakeCheckoutPage(_state())
    with pytest.raises(pay.CheckoutAbort, match="no approval given"):
        _run(tmp_path, page, approval=None, answers=Answers(typed_total="y"))
    assert S["pay_button"] not in page.clicks
    page = FakeCheckoutPage(_state())
    with pytest.raises(pay.CheckoutAbort, match="does not match"):
        _run(tmp_path, page, approval=None, answers=Answers(typed_total="USD 123.40"))
    assert S["pay_button"] not in page.clicks
    page = FakeCheckoutPage(_state())
    answers = Answers(typed_total="USD 123.45")
    result, _, said = _run(tmp_path, page, approval=None, answers=answers)
    assert result.state == "ordered"
    assert any("cannot be cancelled" in line for line in said)  # JLC terms shown


def test_total_changed_between_quote_and_payment_aborts(tmp_path):
    page = FakeCheckoutPage(_state(), total_after_quote="$130.00")
    ledger = OrderLedger(tmp_path / "ledger")
    with pytest.raises(pay.CheckoutAbort) as exc:
        _run(tmp_path, page, ledger=ledger)
    assert exc.value.step == "re-check-total"
    assert "USD 130.00" in exc.value.reason and "USD 123.45" in exc.value.reason
    assert S["pay_button"] not in page.clicks
    assert not ledger.path.exists()


def test_rehearsal_stops_before_the_pay_button(tmp_path):
    page = FakeCheckoutPage(_state())
    result, ledger, said = _run(tmp_path, page, pay_=False)
    assert result.state == "rehearsed" and result.order_id == ""
    assert S["pay_button"] not in page.clicks
    assert not ledger.path.exists()
    assert any("REHEARSAL" in line for line in said)


@pytest.mark.parametrize(
    "text, indicator, expected",
    [
        ("$1,234.50", "USD", ("USD", "1234.50")),
        ("US$ 99", "", ("USD", "99")),
        ("€12.30", "", ("EUR", "12.30")),
        ("Total: USD 5.00", "", ("USD", "5.00")),
    ],
)
def test_page_money_parsing(text, indicator, expected):
    money = pay.parse_page_money(text, indicator)
    assert (money.currency, money.amount) == (expected[0], Decimal(expected[1]))


@pytest.mark.parametrize(
    "text, indicator",
    [("$12.00", ""), ("$12.00", "EUR"), ("12.00", ""), ("$1 + $2", "USD"), ("€1", "USD")],
)
def test_ambiguous_page_money_is_refused(text, indicator):
    with pytest.raises(pay.MoneyError):
        pay.parse_page_money(text, indicator)


def test_unreadable_total_aborts_before_approval(tmp_path):
    state = _state(total="see cart")
    page = FakeCheckoutPage(state)
    with pytest.raises(pay.CheckoutAbort) as exc:
        _run(tmp_path, page)
    assert exc.value.step in ("read-total", "order_total")
    assert S["pay_button"] not in page.clicks


def test_extra_cart_line_aborts(tmp_path):
    state = _state()
    state["elements"][S["cart_items"]]["options"].append("Another board x5")
    page = FakeCheckoutPage(state)
    with pytest.raises(pay.CheckoutAbort, match="2 line"):
        _run(tmp_path, page)
    assert S["pay_button"] not in page.clicks


def test_operator_no_at_unverified_step_stops(tmp_path):
    page = FakeCheckoutPage(_state())
    with pytest.raises(pay.CheckoutAbort, match="unverified step"):
        _run(tmp_path, page, answers=Answers(unverified="n"))
    assert page.clicks == []


def test_every_step_is_confirmed_while_selectors_are_unverified(tmp_path):
    assert pay.CHECKOUT_SELECTORS_VERIFIED is False
    answers = Answers()
    _run(tmp_path, FakeCheckoutPage(_state()), answers=answers)
    asked = [p.split("]")[0] for p in answers.prompts if p.startswith("[unverified")]
    assert asked == [
        "[unverified step: open-checkout",
        "[unverified step: cart-lines",
        "[unverified step: payment-method",
        "[unverified step: read-total",
        "[unverified step: pay",
        "[unverified step: confirmation",
    ]


# --- 2. attended only ------------------------------------------------------


class _Tty:
    def __init__(self, tty=True, name="<stdin>"):
        self._tty, self.name = tty, name

    def isatty(self):
        return self._tty


def _reasons(env, tty=True, foreground=True):
    return pay.unattended_reasons(
        env, streams=[_Tty(tty), _Tty(), _Tty()], foreground=lambda: foreground
    )


def test_attended_terminal_has_no_reasons():
    assert _reasons({"HOME": "/h", "TERM": "xterm"}) == []


@pytest.mark.parametrize(
    "var",
    [
        "CI",
        "GITHUB_ACTIONS",
        "JENKINS_URL",
        "INVOCATION_ID",
        "CLAUDECODE",
        "LOOM_SWEEP_ID",
        "LOOM_AGENT_SESSION_PID",
        "CODEX_SANDBOX",
        "MCP_SERVER_NAME",
        "PYTEST_CURRENT_TEST",
    ],
)
def test_ci_scheduler_agent_or_mcp_environment_refuses(var):
    assert any(var in r for r in _reasons({var: "1"}))


def test_no_tty_or_background_refuses():
    assert _reasons({}, tty=False)
    assert _reasons({}, foreground=False)


def test_real_attended_check_refuses_under_the_test_suite():
    with pytest.raises(pay.CheckoutAbort) as exc:
        pay.require_attended_checkout()
    assert exc.value.step == "attended"


def test_run_checkout_checks_attended_before_touching_anything(tmp_path):
    page = FakeCheckoutPage(_state())
    with pytest.raises(pay.CheckoutAbort) as exc:
        pay.run_checkout(
            page,
            plan_sha256=PLAN,
            approval=pay.Approval(pay.parse_approval("USD 123.45")),
            ledger=OrderLedger(tmp_path / "ledger"),
            ask=Answers(),
            say=print,
            shots_dir=tmp_path / "shots",
            pay=True,
            sleep=lambda s: None,
        )  # the real check, which refuses under pytest
    assert exc.value.step == "attended"
    assert page.log == []


def test_operator_must_type_checkout_to_start(tmp_path):
    page = FakeCheckoutPage(_state())
    with pytest.raises(pay.CheckoutAbort, match="did not start"):
        _run(tmp_path, page, answers=Answers(start="y"))
    assert page.log == []


def test_only_the_cli_imports_the_payment_module():
    root = Path(pay.__file__).resolve().parents[1]
    importers = set()
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.ImportFrom):
                names = [node.module or ""] + [a.name for a in node.names]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            if any("jlc_payment" in n for n in names):
                importers.add(path.relative_to(root).as_posix())
    assert importers == {"cli/submit_jlcpcb_cmd.py"}


def test_no_mcp_tool_reaches_submit_jlcpcb():
    root = Path(pay.__file__).resolve().parents[1]
    for path in (root / "mcp").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "submit_jlcpcb" not in text and "jlc_payment" not in text, path


# --- 3. card data ----------------------------------------------------------


def test_card_file_mode_owner_symlink_and_repo_checks(tmp_path, card_dir):
    good = _write_card(card_dir)
    assert pay.check_card_file(good) == good
    for mode in (0o644, 0o640, 0o400, 0o700):
        os.chmod(good, mode)
        with pytest.raises(pay.CardFileError, match="mode"):
            pay.check_card_file(good)
    os.chmod(good, 0o600)
    with pytest.raises(pay.CardFileError, match="not owned"):
        pay.check_card_file(good, getuid=lambda: os.getuid() + 1)
    link = card_dir / "link.env"
    link.symlink_to(good)
    with pytest.raises(pay.CardFileError, match="symlink"):
        pay.check_card_file(link)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    inside = _write_card(repo / "deep")
    with pytest.raises(pay.CardFileError, match="git repository"):
        pay.check_card_file(inside)
    with pytest.raises(pay.CardFileError, match="absolute"):
        pay.check_card_file(Path("card.env"))


def test_card_errors_name_keys_never_values(card_dir):
    path = _write_card(card_dir)
    path.write_text(f"KCT_CARD_NAME={CARD_NAME}\nKCT_CARD_NUMBER=4111111111111112\n")
    os.chmod(path, 0o600)
    with pytest.raises(pay.CardFileError) as exc:
        pay.load_card(path)
    text = "".join(traceback.format_exception(exc.value))
    assert "KCT_CARD_EXPIRY" in text or "KCT_CARD_NUMBER" in text
    assert "4111111111111112" not in text and CARD_NAME not in text


def test_card_secret_redacts_itself(card_dir):
    card = pay.load_card(_write_card(card_dir))
    for rendered in (str(card), repr(card), f"{card}", f"{card!r}", format(card, "")):
        assert CARD_NUMBER not in rendered and CARD_NAME not in rendered
    with pytest.raises(TypeError):
        json.dumps(card)
    with pytest.raises(TypeError):
        copy.copy(card)
    with pytest.raises(AttributeError):
        card.__dict__  # noqa: B018
    assert card.reveal("KCT_CARD_NUMBER") == CARD_NUMBER


def test_card_is_read_only_at_the_fill_step(tmp_path, card_dir, monkeypatch):
    path = _write_card(card_dir)
    calls = []
    real = pay.load_card
    monkeypatch.setattr(pay, "load_card", lambda p, **k: calls.append(p) or real(p, **k))
    page = FakeCheckoutPage(_state())
    answers = Answers(unverified="n")  # stop at the first unverified step
    with pytest.raises(pay.CheckoutAbort):
        _run(tmp_path, page, answers=answers, card_file=path)
    assert calls == []
    _run(tmp_path, FakeCheckoutPage(_state()), card_file=path)
    assert calls == [path]


def _scan(root: Path, needles, exclude: Path):
    hits = []
    for p in root.rglob("*"):
        if p.is_file() and p != exclude:
            data = p.read_bytes()
            hits += [f"{p}: {n}" for n in needles if n.encode() in data]
    return hits


def test_card_data_never_reaches_any_artifact_log_or_output(
    tmp_path, card_dir, caplog, capsys, monkeypatch
):
    """Redaction scan: every file under tmp_path (run dir, screenshots, ledger),
    stdout/stderr and all log records, after a paid run AND after failures."""
    caplog.set_level(logging.DEBUG)
    path = _write_card(card_dir)
    needles = (CARD_NUMBER, "4111 1111", CARD_NAME, f"CVC={CARD_CVC}")
    monkeypatch.setenv("KCT_TEST_SENTINEL", "x")
    page = FakeCheckoutPage(_state())
    result, ledger, said = _run(tmp_path, page, card_file=path)
    assert result.state == "ordered" and result.payment_method == "card-file"
    assert page.filled[S["card_number"]] == CARD_NUMBER  # it did reach the fab's form
    (tmp_path / "out" / "checkout-receipt.json").write_text(json.dumps(result.as_dict()))
    failures = []
    for variant in ("redirect", "no-marker", "raise"):
        variant_ledger = OrderLedger(tmp_path / f"ledger-{variant}")
        with pytest.raises(pay.PaymentUnconfirmed) as exc:
            _run(
                tmp_path,
                FakeCheckoutPage(_state(), after_pay=variant),
                card_file=path,
                ledger=variant_ledger,
            )
        failures.append("".join(traceback.format_exception(exc.value)))
    with pytest.raises(pay.CheckoutAbort) as exc:
        _run(
            tmp_path,
            FakeCheckoutPage(_state(), fill_error=True),
            card_file=path,
            ledger=OrderLedger(tmp_path / "ledger-fill"),
        )
    failures.append("".join(traceback.format_exception(exc.value)))
    assert "details withheld" in failures[-1]
    assert exc.value.__context__ is None and exc.value.__cause__ is None
    cap = capsys.readouterr()
    text = cap.out + cap.err + "\n".join(said) + caplog.text + "\n".join(failures)
    assert [n for n in needles if n in text] == []
    assert _scan(tmp_path, needles, exclude=path) == []
    assert all(CARD_NUMBER not in v for k, v in os.environ.items())
    # no screenshot is taken once card data is in the page
    shots = sorted(p.name for p in (tmp_path / "out" / "site-screenshots").iterdir())
    assert not any("confirmation" in s or "unconfirmed" in s for s in shots)


def test_empty_cvc_waits_for_the_operator(tmp_path, card_dir):
    path = _write_card(card_dir, cvc="")
    page = FakeCheckoutPage(_state())
    answers = Answers()
    result, _, _ = _run(tmp_path, page, card_file=path, answers=answers)
    assert S["card_cvc"] not in page.filled
    assert any(p.startswith("Type the security code") for p in answers.prompts)
    assert result.state == "ordered"
    page = FakeCheckoutPage(_state())
    with pytest.raises(pay.CheckoutAbort, match="card-cvc|stopped by the operator"):
        _run(
            tmp_path,
            page,
            card_file=path,
            answers=Answers(cvc="no"),
            ledger=OrderLedger(tmp_path / "ledger-cvc"),
        )
    assert S["pay_button"] not in page.clicks


def test_saved_method_is_the_default_and_types_nothing(tmp_path):
    page = FakeCheckoutPage(_state())
    result, _, _ = _run(tmp_path, page)
    assert result.payment_method == "saved" and page.filled == {}


# --- 4. ordered only from a confirmation page ------------------------------


@pytest.mark.parametrize("variant", ["redirect", "no-marker", "raise", "raise-after-navigate"])
def test_no_confirmation_page_records_unconfirmed_not_ordered(tmp_path, variant):
    page = FakeCheckoutPage(_state(), after_pay=variant)
    ledger = OrderLedger(tmp_path / "ledger")
    with pytest.raises(pay.PaymentUnconfirmed) as exc:
        _run(tmp_path, page, ledger=ledger)
    assert "NOT recorded as ordered" in exc.value.reason
    assert "Check your JLCPCB account" in exc.value.reason
    assert page.clicks.count(S["pay_button"]) == 1  # never retried
    events = [e["event"] for e in ledger.events()]
    assert events == ["payment-attempted", "payment-unconfirmed"]
    assert ledger.plan_status(PLAN).state == "unresolved"


def test_charged_total_differing_from_approval_is_unconfirmed(tmp_path):
    page = FakeCheckoutPage(_state(charged="$150.00"))
    ledger = OrderLedger(tmp_path / "ledger")
    with pytest.raises(pay.PaymentUnconfirmed, match="USD 150.00"):
        _run(tmp_path, page, ledger=ledger)
    assert ledger.plan_status(PLAN).state == "unresolved"


def test_malformed_order_number_is_unconfirmed(tmp_path):
    state = _state()
    state["elements"][S["order_number"]]["text"] = "Thank you!"
    with pytest.raises(pay.PaymentUnconfirmed, match="order number"):
        _run(tmp_path, FakeCheckoutPage(state))


def test_attempt_is_on_disk_before_the_click(tmp_path):
    ledger = OrderLedger(tmp_path / "ledger")
    page = FakeCheckoutPage(_state())
    seen = []
    original = page.click

    def click(sel):
        if sel == S["pay_button"]:
            seen.append([e["event"] for e in ledger.events()])
        original(sel)

    page.click = click
    _run(tmp_path, page, ledger=ledger)
    assert seen == [["payment-attempted"]]


# --- 5. idempotence --------------------------------------------------------


def test_rerun_after_an_order_stops_before_any_click(tmp_path):
    ledger = OrderLedger(tmp_path / "ledger")
    _run(tmp_path, FakeCheckoutPage(_state()), ledger=ledger)
    page = FakeCheckoutPage(_state())
    with pytest.raises(pay.CheckoutAbort) as exc:
        _run(tmp_path, page, ledger=ledger)
    assert exc.value.step == "ledger" and "already ordered" in exc.value.reason
    assert "Y2-1234567A" in exc.value.reason
    assert page.log == []


def test_rerun_after_an_unconfirmed_attempt_stops(tmp_path):
    ledger = OrderLedger(tmp_path / "ledger")
    with pytest.raises(pay.PaymentUnconfirmed):
        _run(tmp_path, FakeCheckoutPage(_state(), after_pay="redirect"), ledger=ledger)
    page = FakeCheckoutPage(_state())
    with pytest.raises(pay.CheckoutAbort, match="never confirmed"):
        _run(tmp_path, page, ledger=ledger)
    assert page.log == []


# --- 6. request scope ------------------------------------------------------


def test_actions_are_paced_and_nothing_is_repeated(tmp_path):
    page = FakeCheckoutPage(_state())
    sleeps = []
    pay.run_checkout(
        page,
        plan_sha256=PLAN,
        approval=pay.Approval(pay.parse_approval("USD 123.45")),
        ledger=OrderLedger(tmp_path / "ledger"),
        ask=Answers(),
        say=lambda s: None,
        shots_dir=tmp_path / "shots",
        pay=True,
        sleep=sleeps.append,
    )
    assert page.clicks == [S["checkout_button"], S["saved_payment_method"], S["pay_button"]]
    assert len(sleeps) >= 5 and all(s >= pay.HUMAN_PACE_S for s in sleeps)


@pytest.mark.parametrize(
    "url, why",
    [
        ("https://evil.example/checkout", "allowed hosts"),
        ("https://evil.example/user-center/checkout", "allowed hosts"),
        ("https://evil.example/shoppingCart", "allowed hosts"),
        ("https://jlcpcb.com/api/pay", "robots.txt"),
        ("https://jlcpcb.com/smtOrder/downloadNoMatchBomList", "robots.txt"),
        # inside the allowed /shoppingCart prefix: only STILL_DISALLOWED stops it
        ("https://cart.jlcpcb.com/shoppingCart/smtGood/downloadNoMatchBomList", "robots.txt"),
        ("https://jlcpcb.com/x", "expected checkout path"),
    ],
)
def test_unexpected_page_aborts_before_paying(tmp_path, url, why):
    page = FakeCheckoutPage(_state())
    page.url = url
    with pytest.raises(pay.CheckoutAbort) as exc:
        _run(tmp_path, page)
    assert why in exc.value.reason
    assert page.clicks == []


def test_url_is_checked_after_each_action(tmp_path):
    bad = "https://cart.jlcpcb.com/shoppingCart/smtGood/downloadNoMatchBomList"
    page = FakeCheckoutPage(_state(), checkout_lands_on=bad)
    with pytest.raises(pay.CheckoutAbort) as exc:
        _run(tmp_path, page)
    # caught by the post-action check of the step that navigated, not the next step
    assert exc.value.step == "open-checkout" and "robots.txt" in exc.value.reason
    assert S["pay_button"] not in page.clicks


def test_payment_module_has_no_http_client_or_navigation():
    tree = ast.parse(Path(pay.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not imported & {"requests", "httpx", "urllib3", "aiohttp", "http", "socket"}
    assert not any(isinstance(node, ast.While) for node in ast.walk(tree))  # no polling
    attrs = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert not attrs & {"goto", "request", "fetch", "urlopen", "evaluate", "reload"}


# --- Judge round 1 (PR #9101): guards that each need a failing test ----------


def test_total_is_reread_after_the_pay_prompt_immediately_before_the_click(tmp_path):
    """The operator may pause at the pay prompt; a change during that pause must stop."""
    page = FakeCheckoutPage(_state())

    def change_total():
        page.state["elements"][S["order_total"]]["text"] = "$130.00"
        return "y"

    ledger = OrderLedger(tmp_path / "ledger")
    with pytest.raises(pay.CheckoutAbort) as exc:
        _run(tmp_path, page, ledger=ledger, answers=Answers(on_step={"pay": change_total}))
    assert exc.value.step == "re-check-total" and "USD 130.00" in exc.value.reason
    assert S["pay_button"] not in page.clicks
    assert not ledger.path.exists()


def test_attempt_record_is_fsynced_to_the_ledger_before_the_click(tmp_path, monkeypatch):
    ledger = OrderLedger(tmp_path / "ledger")
    page = FakeCheckoutPage(_state())
    order: list[str] = []
    real_fsync = os.fsync

    def fsync(fd):
        if os.fstat(fd).st_ino == ledger.path.stat().st_ino:
            order.append("fsync-ledger")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    original = page.click

    def click(sel):
        if sel == S["pay_button"]:
            order.append("pay-click")
        original(sel)

    page.click = click
    _run(tmp_path, page, ledger=ledger)
    assert order[:2] == ["fsync-ledger", "pay-click"]


def test_ledger_is_rechecked_under_the_lock_right_before_paying(tmp_path):
    """A second run that ordered this plan mid-session (start check already passed)."""
    ledger = OrderLedger(tmp_path / "ledger")
    page = FakeCheckoutPage(_state())

    def other_run_orders():
        OrderLedger(tmp_path / "ledger").append("ordered", PLAN, order_id="Y2-9999999A")
        return "y"

    with pytest.raises(pay.CheckoutAbort) as exc:
        _run(tmp_path, page, ledger=ledger, answers=Answers(on_step={"pay": other_run_orders}))
    assert exc.value.step == "ledger" and "already ordered" in exc.value.reason
    assert S["pay_button"] not in page.clicks
    assert [e["event"] for e in ledger.events()] == ["ordered"]


def test_a_concurrent_run_holding_the_ledger_lock_stops_this_one(tmp_path):
    import fcntl

    ledger = OrderLedger(tmp_path / "ledger")
    ledger.directory.mkdir(parents=True)
    lock = ledger.directory / "jlc-orders.lock"
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        page = FakeCheckoutPage(_state())
        with pytest.raises(pay.CheckoutAbort) as exc:
            _run(tmp_path, page, ledger=ledger)
    finally:
        os.close(fd)
    assert "another kct checkout" in exc.value.reason and "nothing was paid" in exc.value.reason
    assert S["pay_button"] not in page.clicks
    assert not ledger.path.exists()


def test_run_checkout_has_no_parameter_to_skip_the_attended_check(tmp_path):
    import inspect

    assert "attended" not in inspect.signature(pay.run_checkout).parameters
    page = FakeCheckoutPage(_state())
    with pytest.raises(pay.CheckoutAbort) as exc:
        _run(tmp_path, page)  # the real module-level check, under pytest
    assert exc.value.step == "attended" and page.log == []


@pytest.mark.parametrize(
    "env, needle",
    [({"DEBUG": "pw:api"}, "DEBUG"), ({"DEBUG": "*"}, "DEBUG"), ({"PWDEBUG": "1"}, "PWDEBUG")],
)
def test_playwright_debug_logging_refuses(env, needle):
    assert any(needle in r for r in _reasons(env))


@pytest.mark.parametrize(
    "text",
    ["USD -123.45", "USD +123.45", "USD −123.45", "(USD 123.45)", "USD 0.00", "USD 0"],
)
def test_signed_bracketed_or_zero_approval_is_refused(text):
    with pytest.raises(pay.MoneyError):
        pay.parse_approval(text)


@pytest.mark.parametrize(
    "text, indicator",
    [
        ("$-5.00", "USD"),
        ("-$5.00", "USD"),
        ("($5.00)", "USD"),
        ("$0.00", "USD"),
        ("$١٢.00", "USD"),  # Arabic-Indic digits
        ("$１２.00", "USD"),  # fullwidth digits
    ],
)
def test_signed_zero_or_non_ascii_page_total_is_refused(text, indicator):
    with pytest.raises(pay.MoneyError):
        pay.parse_page_money(text, indicator)


@pytest.mark.parametrize("text", ["USD \u0661\u0662.00", "USD 1\uff12.00", "USD 12.0\u0663"])
def test_non_ascii_digits_are_refused_by_name(text):
    with pytest.raises(pay.MoneyError, match="non-ASCII digits"):
        pay.parse_approval(text)


def test_control_characters_in_page_text_never_reach_the_terminal(tmp_path, capsys):
    page = FakeCheckoutPage(_state(), click_error="boom \x1b[2J\x1b]0;owned\x07 " + "x" * 2000)
    with pytest.raises(pay.CheckoutAbort) as exc:
        _run(tmp_path, page)
    text = str(exc.value) + exc.value.reason
    assert "\x1b" not in text and "\x07" not in text
    assert len(exc.value.reason) <= 400


def test_operator_must_confirm_the_confirmation_page(tmp_path):
    page = FakeCheckoutPage(_state())
    ledger = OrderLedger(tmp_path / "ledger")
    answers = Answers(on_step={"confirmation": lambda: "n"})
    with pytest.raises(pay.PaymentUnconfirmed, match="did not confirm"):
        _run(tmp_path, page, ledger=ledger, answers=answers)
    assert ledger.plan_status(PLAN).state == "unresolved"
    assert page.clicks.count(S["pay_button"]) == 1


def test_unchanged_url_after_the_click_is_unconfirmed(tmp_path):
    ledger = OrderLedger(tmp_path / "ledger")
    with pytest.raises(pay.PaymentUnconfirmed, match="did not change"):
        _run(tmp_path, FakeCheckoutPage(_state(), after_pay="stay"), ledger=ledger)
    assert ledger.plan_status(PLAN).state == "unresolved"


@pytest.mark.parametrize("path", ["/user-center/order/list", "/user-center/orders", "/pay"])
def test_pre_payment_order_pages_are_not_confirmation_pages(path):
    assert not any(path.startswith(p) for p in pay.CONFIRMATION_PATH_PREFIXES)


def test_amount_pattern_matches_ascii_digits_only():
    import re

    assert pay._AMOUNT.flags & re.ASCII
    assert pay._AMOUNT.findall("١٢.00") == []


# --- Judge round 2 (PR #9101): the remaining unpinned guards -------------------

_COMPETING_RUN = r"""
import sys
from pathlib import Path
from kicad_tools.manufacturers.jlc_ledger import OrderLedger
ledger = OrderLedger(Path(sys.argv[1]))
with ledger.locked():
    if ledger.plan_status(sys.argv[2]).state == "not-ordered":
        ledger.append("payment-attempted", sys.argv[2], approved_total="USD 123.45")
        print("RECORDED")
"""


class _RacedLedger(OrderLedger):
    """A real second process completes its whole locked check-and-record just
    before this run takes the lock: the window a re-check outside the lock misses."""

    def locked(self):
        import subprocess
        import sys

        env = dict(os.environ, PYTHONPATH=os.pathsep.join(p for p in sys.path if p))
        out = subprocess.run(
            [sys.executable, "-c", _COMPETING_RUN, str(self.directory), PLAN],
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )
        assert out.stdout.strip() == "RECORDED", out.stderr
        return super().locked()


def test_recheck_inside_the_lock_stops_a_second_process_that_just_recorded(tmp_path):
    ledger = _RacedLedger(tmp_path / "ledger")
    page = FakeCheckoutPage(_state())
    with pytest.raises(pay.CheckoutAbort) as exc:
        _run(tmp_path, page, ledger=ledger)
    assert exc.value.step == "ledger" and "never confirmed" in exc.value.reason
    assert S["pay_button"] not in page.clicks
    attempts = [e for e in ledger.events() if e["event"] == "payment-attempted"]
    assert len(attempts) == 1  # only the other process's; this run recorded nothing


def test_recheck_and_attempt_record_both_happen_while_the_lock_is_held(tmp_path):
    held = {"now": False}
    calls: list[tuple[str, bool]] = []

    class Spy(OrderLedger):
        def locked(self):
            from contextlib import contextmanager

            parent = super().locked

            @contextmanager
            def cm():
                with parent():
                    held["now"] = True
                    try:
                        yield
                    finally:
                        held["now"] = False

            return cm()

        def plan_status(self, plan):
            calls.append(("status", held["now"]))
            return super().plan_status(plan)

        def append(self, event, plan, **kw):
            calls.append((event, held["now"]))
            return super().append(event, plan, **kw)

    _run(tmp_path, FakeCheckoutPage(_state()), ledger=Spy(tmp_path / "ledger"))
    i = calls.index(("payment-attempted", True))
    assert calls[i - 1] == ("status", True)  # the re-check immediately precedes it, locked
    assert calls[0] == ("status", False)  # the start check is the unlocked early stop


def test_confirmation_on_a_foreign_host_is_never_ordered(tmp_path):
    page = FakeCheckoutPage(_state())
    original = page.click

    def click(sel):
        original(sel)
        if sel == S["pay_button"]:
            page.url = "https://evil.example/user-center/order/success"

    page.click = click
    ledger = OrderLedger(tmp_path / "ledger")
    with pytest.raises(pay.PaymentUnconfirmed, match="not a confirmation page"):
        _run(tmp_path, page, ledger=ledger)
    assert ledger.plan_status(PLAN).state == "unresolved"


def test_ledger_write_failure_after_a_confirmed_order_names_the_order(tmp_path, monkeypatch):
    real_write = os.write

    def write(fd, data):
        if b'"event": "ordered"' in data:
            raise OSError(28, "No space left on device")
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", write)
    ledger = OrderLedger(tmp_path / "ledger")
    with pytest.raises(pay.PaymentUnconfirmed) as exc:
        _run(tmp_path, FakeCheckoutPage(_state()), ledger=ledger)
    assert exc.value.step == "ledger"
    assert "Y2-1234567A" in exc.value.reason and "Write that down" in exc.value.reason
    assert ledger.plan_status(PLAN).state == "unresolved"  # the attempt still blocks a re-run


@pytest.mark.parametrize(
    "text, indicator",
    [
        ("‐$5.00", "USD"),
        ("‑$5.00", "USD"),
        ("➖$5.00", "USD"),
        ("˗$5.00", "USD"),
        ("–$5.00", "USD"),
        ("HK$5.00", "USD"),
        ("C$5.00", "USD"),
        ("A$5.00", "USD"),
        ("$5.00 CR", "USD"),
        ("USD 123.45%", ""),
    ],
)
def test_money_residuals_are_refused(text, indicator):
    with pytest.raises(pay.MoneyError):
        pay.parse_page_money(text, indicator)


def test_us_dollar_prefix_is_still_accepted():
    assert str(pay.parse_page_money("US$5.00")) == "USD 5.00"


def test_lowercase_approval_code_is_refused():
    with pytest.raises(pay.MoneyError, match="currency code"):
        pay.parse_approval("usd 1.00")


def test_malformed_currency_indicator_is_refused_by_name():
    with pytest.raises(pay.MoneyError, match="unreadable currency indicator"):
        pay.parse_page_money("$1.00", "usd!")


def test_unconfirmed_reason_stored_in_the_ledger_is_terminal_safe(tmp_path):
    page = FakeCheckoutPage(_state())
    original = page.click

    def click(sel):
        original(sel)
        if sel == S["pay_button"]:
            page.url = "https://jlcpcb.com/quote/%1b" + "‮" + "x" * 3000

    page.click = click
    ledger = OrderLedger(tmp_path / "ledger")
    with pytest.raises(pay.PaymentUnconfirmed):
        _run(tmp_path, page, ledger=ledger)
    (record,) = [e for e in ledger.events() if e["event"] == "payment-unconfirmed"]
    assert "‮" not in record["reason"] and len(record["reason"]) <= 400
