"""Attended JLCPCB website driver: fill the PCBA order form, stop at the saved cart.

Issue #8468 (part 3 of #8465), under the operator ruling of 2026-11-05 on that
issue. This is the ONLY module that knows JLCPCB's page structure, so a site
change edits one file (:data:`SELECTORS`, bump :data:`DRIVER_VERSION`).

Binding constraints (each is enforced in code and covered by a test):

* **Attended only.** :func:`require_attended` refuses without an interactive TTY;
  the page comes from the operator's own signed-in Chrome over CDP
  (:func:`connect_attended_page`). There is no headless or CI entry point.
* **One driver per browser.** :func:`browser_lock` holds a per-CDP-port lock
  file (stale after :data:`LOCK_STALE_AFTER_S` or a dead holder) so a second
  agent cannot drive the same signed-in session mid-order (issue #8465).
* **Human pace.** One action at a time, :meth:`SitePage.wait_settled` after each,
  no parallel tabs, no polling loop, no retry.
* **UI only.** The :class:`SitePage` protocol has no navigation and no raw HTTP
  call, and this module imports no HTTP client. After every step the page URL
  is checked against :data:`DISALLOWED_PATHS` (robots.txt) and the allowlist
  :data:`ALLOWED_HOSTS`. The part-matching export is downloaded by the operator
  and handed in as bytes; the driver never fetches it.
* **No cart without the fab-view sign-off (issue #8818).** The driver uploads
  and checks the factory match, then refuses to click "save to cart" unless the
  tapeout manifest carries a ``fab_review_signoff`` (a human looked at the fab's
  gerber viewer / DFM result and ran ``kct tapeout-sign-off --stage
  fab-review``). Re-run after signing; the pause is the abort itself.
* **Stops at the saved cart.** The last action is "save to cart". Nothing here
  places an order, pays for anything or buys parts, and "ordered" is never set.
* **Kill switch.** Any failed assertion raises :class:`DriverAbort` (with a
  screenshot) and the caller falls back to :func:`guided_checklist`
  (``--guided``). The driver never retries and never submits after an abort.

Selectors in :data:`SELECTORS` are UNVERIFIED against the live site: they were
written without a recorded page, and a first live run (issue #8629) found the
page has no ``[data-kct]`` attributes and no ``<select>``: every option is a
chip/button, which :class:`_PlaywrightPage` models through the same
``options``/``select``/``value`` seam (read-back = the chip's selected state). Tests run against synthetic page-state
fixtures, which prove the abort/gate logic but not the live selectors; a live
attended run is tracked separately.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from ..export.factory_selection import (
    ExpectedPart,
    SelectionMapping,
    compare_factory_selection,
)

DRIVER_VERSION = "jlc-site-driver-1"
ALLOWED_HOSTS = ("jlcpcb.com", "www.jlcpcb.com", "cart.jlcpcb.com")
#: robots.txt-disallowed prefixes: if the page ever lands on one, abort.
DISALLOWED_PATHS = (
    "/api",
    "/user-center/",
    "/smtOrder/downloadNoMatchBomList",
    "/shoppingCart/smtGood/downloadNoMatchBomList",
)
#: Where the order form is expected to be (the operator navigates there).
ORDER_PAGE_PATH_PREFIXES = ("/quote", "/orderOnline", "/pcb-quote", "/cart", "/shoppingCart")

#: UNVERIFIED against the live site (see module docstring).
SELECTORS: dict[str, str] = {
    "gerber_input": "input[type=file][data-kct=gerber]",
    "layers": "select[data-kct=layers]",
    "thickness": "select[data-kct=thickness]",
    "outer_copper": "select[data-kct=outer-copper]",
    "inner_copper": "select[data-kct=inner-copper]",
    "quantity": "select[data-kct=quantity]",
    "pcba_toggle": "[data-kct=pcba-toggle]",
    "pcba_type": "select[data-kct=pcba-type]",
    "assembly_side": "select[data-kct=assembly-side]",
    "bom_input": "input[type=file][data-kct=bom]",
    "cpl_input": "input[type=file][data-kct=cpl]",
    "match_ready": "[data-kct=match-ready]",
    "save_to_cart": "[data-kct=save-to-cart]",
    "quote_total": "[data-kct=quote-total]",
    "lead_time": "[data-kct=lead-time]",
    "cart_url": "[data-kct=cart-link]",
}

_LAYER_ENUM = {1: "1", 2: "2", 4: "4", 6: "6"}
_THICKNESS_ENUM = {"0.8": "0.8", "1.0": "1.0", "1.2": "1.2", "1.6": "1.6", "2.0": "2.0"}
_COPPER_ENUM = {"1": "1 oz", "2": "2 oz"}
_SIDE_ENUM = {"top": "Top Side", "bottom": "Bottom Side", "both": "Both Sides"}
_PCBA_TYPE = {True: "Standard", False: "Economic"}


class DriverAbort(RuntimeError):
    """A step assertion failed; nothing further was attempted and nothing submitted."""

    def __init__(self, step: str, reason: str, screenshot: str | None = None):
        super().__init__(f"{step}: {reason}")
        self.step = step
        self.reason = reason
        self.screenshot = screenshot


class SitePage(Protocol):
    """Everything the driver may do to the page. Deliberately no navigation, no HTTP."""

    @property
    def url(self) -> str: ...
    def exists(self, selector: str) -> bool: ...
    def options(self, selector: str) -> Sequence[str]: ...
    def select(self, selector: str, value: str) -> None: ...
    def value(self, selector: str) -> str: ...
    def text(self, selector: str) -> str: ...
    def click(self, selector: str) -> None: ...
    def set_file(self, selector: str, path: Path) -> None: ...
    def wait_settled(self) -> None: ...
    def screenshot(self, path: Path) -> None: ...


@dataclass(frozen=True)
class SiteOrderSpec:
    """Order fields taken from the frozen plan plus operator-stated board facts.

    Thickness and copper are not in the plan, so they are required explicit
    inputs here; nothing is defaulted or guessed.
    """

    layers: int
    quantity: int
    thickness_mm: str
    outer_copper_oz: str
    inner_copper_oz: str | None
    assembly_side: str  # top | bottom | both
    has_tht: bool


@dataclass(frozen=True)
class SiteInputs:
    gerber: Path
    bom: Path
    cpl: Path
    #: sha256 each file must still have (from the frozen plan).
    sha256: Mapping[str, str]


@dataclass
class StepRecord:
    name: str
    screenshot: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class SiteResult:
    driver_version: str
    steps: list[StepRecord]
    uploaded_hashes: dict[str, str]
    match_report: dict[str, Any]
    cart_url: str
    quote_total: str
    lead_time: str
    state: str = "in-cart"  # never "ordered": only the human sets that
    remaining_human_steps: tuple[str, ...] = (
        "Confirm consignment / parts inventory choices",
        "Place and pay for the order yourself (this tool never does)",
    )

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "driver_version": self.driver_version,
            "cart_url": self.cart_url,
            "quote_total": self.quote_total,
            "lead_time": self.lead_time,
            "uploaded_hashes": dict(self.uploaded_hashes),
            "steps": [
                {"name": s.name, "screenshot": s.screenshot, "detail": s.detail} for s in self.steps
            ],
            "match_diff": _match_diff(self.match_report),
            "remaining_human_steps": list(self.remaining_human_steps),
        }


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _match_diff(report: Mapping[str, Any]) -> list[str]:
    return [str(i) for i in report.get("issues", ())]


def require_attended(isatty: Callable[[], bool] | None = None) -> None:
    """Refuse to run without an interactive terminal (no CI, no unattended runs)."""
    check = isatty or (lambda: sys.stdin.isatty() and sys.stdout.isatty())
    if not check():
        raise DriverAbort("attended", "refusing to run without an interactive TTY; use --guided")


def map_order_fields(spec: SiteOrderSpec) -> dict[str, str]:
    """Map the spec onto site enum values; raise ``DriverAbort`` on anything unmappable."""
    problems = []
    if spec.layers not in _LAYER_ENUM:
        problems.append(f"layer count {spec.layers}")
    if spec.thickness_mm not in _THICKNESS_ENUM:
        problems.append(f"thickness {spec.thickness_mm!r}")
    if spec.outer_copper_oz not in _COPPER_ENUM:
        problems.append(f"outer copper {spec.outer_copper_oz!r}")
    if spec.layers >= 4 and spec.inner_copper_oz not in _COPPER_ENUM:
        problems.append(f"inner copper {spec.inner_copper_oz!r}")
    if spec.layers < 4 and spec.inner_copper_oz is not None:
        problems.append("inner copper given for a board without inner layers")
    if spec.assembly_side not in _SIDE_ENUM:
        problems.append(f"assembly side {spec.assembly_side!r}")
    if not isinstance(spec.quantity, int) or isinstance(spec.quantity, bool) or spec.quantity < 1:
        problems.append(f"quantity {spec.quantity!r}")
    if problems:
        raise DriverAbort("map-fields", "cannot map exactly: " + "; ".join(problems))
    fields = {
        "layers": _LAYER_ENUM[spec.layers],
        "thickness": _THICKNESS_ENUM[spec.thickness_mm],
        "outer_copper": _COPPER_ENUM[spec.outer_copper_oz],
        "quantity": str(spec.quantity),
        "pcba_type": _PCBA_TYPE[spec.has_tht],
        "assembly_side": _SIDE_ENUM[spec.assembly_side],
    }
    if spec.layers >= 4:
        assert spec.inner_copper_oz is not None
        fields["inner_copper"] = _COPPER_ENUM[spec.inner_copper_oz]
    return fields


class _Run:
    def __init__(self, page: SitePage, shots: Path):
        self.page = page
        self.shots = shots
        self.records: list[StepRecord] = []
        shots.mkdir(parents=True, exist_ok=True)

    def _shot(self, name: str) -> str:
        path = self.shots / f"{len(self.records) + 1:02d}-{name}.png"
        try:
            self.page.screenshot(path)
        except Exception:  # a failed screenshot must not mask the real abort
            return ""
        return path.name

    def check_url(self, step: str) -> None:
        parsed = urlparse(self.page.url)
        if parsed.hostname not in ALLOWED_HOSTS:
            raise DriverAbort(step, f"page left the allowed hosts: {parsed.hostname}")
        if any(parsed.path.startswith(p) for p in DISALLOWED_PATHS):
            raise DriverAbort(step, f"page is on a robots.txt-disallowed path: {parsed.path}")

    def step(self, name: str, action: Callable[[], dict[str, Any] | None]) -> StepRecord:
        try:
            self.check_url(name)
            detail = action() or {}
            self.page.wait_settled()
            self.check_url(name)
        except DriverAbort as exc:
            exc.screenshot = exc.screenshot or self._shot(f"abort-{name}")
            raise
        except Exception as exc:  # missing selector, driver error: abort, never retry
            raise DriverAbort(
                name, f"{type(exc).__name__}: {exc}", self._shot(f"abort-{name}")
            ) from exc
        record = StepRecord(name, self._shot(name), detail)
        self.records.append(record)
        return record

    def need(self, key: str) -> str:
        selector = SELECTORS[key]
        if not self.page.exists(selector):
            raise DriverAbort(key, f"selector missing: {selector}")
        return selector

    def set_enum(self, key: str, wanted: str) -> dict[str, Any]:
        selector = self.need(key)
        if wanted not in list(self.page.options(selector)):
            raise DriverAbort(key, f"option {wanted!r} not offered; refusing to guess")
        self.page.select(selector, wanted)
        if self.page.value(selector) != wanted:
            raise DriverAbort(key, f"read-back {self.page.value(selector)!r} != {wanted!r}")
        return {"value": wanted}

    def upload(self, key: str, path: Path, expected_sha: str, hashes: dict[str, str]) -> None:
        digest = _sha(path)
        if digest != expected_sha:
            raise DriverAbort(key, f"{path.name} changed since the plan was frozen")
        self.page.set_file(self.need(key), path)
        hashes[path.name] = digest


def run_driver(
    page: SitePage,
    spec: SiteOrderSpec,
    inputs: SiteInputs,
    expected: Sequence[ExpectedPart],
    mapping: SelectionMapping,
    get_match_csv: Callable[[], bytes],
    shots_dir: Path,
    *,
    attended: bool = True,
    fab_review_signed: bool = False,
) -> SiteResult:
    """Drive the order form to a saved cart. Raises :class:`DriverAbort` on any failure.

    ``fab_review_signed`` says the manifest carries a valid fab-review sign-off
    (issue #8818); without it the run stops after the factory match, before
    "save to cart".
    """
    if not attended:
        raise DriverAbort("attended", "the website driver only runs attended")
    fields = map_order_fields(spec)
    run = _Run(page, Path(shots_dir))
    hashes: dict[str, str] = {}

    run.step("check-page", lambda: {"url_ok": True})
    run.step(
        "upload-gerber",
        lambda: run.upload("gerber_input", inputs.gerber, inputs.sha256["gerber"], hashes),
    )
    for key in ("layers", "thickness", "outer_copper", "inner_copper", "quantity"):
        if key in fields:
            run.step(f"set-{key}", partial(run.set_enum, key, fields[key]))
    run.step("enable-pcba", lambda: run.page.click(run.need("pcba_toggle")))
    run.step(
        "set-pcba-type",
        lambda: run.set_enum("pcba_type", fields["pcba_type"]),
    )
    run.step(
        "set-assembly-side",
        lambda: run.set_enum("assembly_side", fields["assembly_side"]),
    )
    run.step(
        "upload-bom", lambda: run.upload("bom_input", inputs.bom, inputs.sha256["bom"], hashes)
    )
    run.step(
        "upload-cpl", lambda: run.upload("cpl_input", inputs.cpl, inputs.sha256["cpl"], hashes)
    )

    def await_match() -> None:
        run.need("match_ready")

    run.step("await-match", await_match)

    # The operator downloads JLC's part-matching export themselves; we only read the bytes.
    report = compare_factory_selection(
        expected, get_match_csv(), mapping, source_kind="factory_selected_csv"
    )
    if not report["comparison_matches"]:
        raise DriverAbort(
            "factory-match",
            "part matching differs from the plan; cart blocked: " + "; ".join(_match_diff(report)),
            run._shot("abort-factory-match"),
        )
    run.records.append(
        StepRecord("factory-match", "", {"factory_csv_sha256": report["factory_csv_sha256"]})
    )

    if not fab_review_signed:
        raise DriverAbort(
            "fab-review",
            "the form is filled and the gerbers are uploaded, but the cart cannot be saved "
            "without the fab-view sign-off. Review the gerber viewer / DFM result on the "
            "JLCPCB page, record it with `kct tapeout-sign-off --stage fab-review`, then "
            "re-run --website",
            run._shot("abort-fab-review"),
        )

    quote: dict[str, str] = {}

    def save() -> dict[str, Any]:
        run.page.click(run.need("save_to_cart"))
        return {}

    run.step("save-to-cart", save)

    def read_result() -> dict[str, Any]:
        quote["total"] = run.page.text(run.need("quote_total")).strip()
        quote["lead"] = run.page.text(run.need("lead_time")).strip()
        quote["cart"] = run.page.value(run.need("cart_url")).strip()
        if not quote["cart"]:
            raise DriverAbort("read-cart", "no cart URL displayed")
        host = urlparse(quote["cart"]).hostname
        if host not in ALLOWED_HOSTS:
            raise DriverAbort("read-cart", f"cart URL host not allowed: {host}")
        return dict(quote)

    run.step("read-cart", read_result)
    return SiteResult(
        DRIVER_VERSION, run.records, hashes, report, quote["cart"], quote["total"], quote["lead"]
    )


def guided_checklist(spec: SiteOrderSpec, inputs: SiteInputs) -> list[str]:
    """The manual fallback (``--guided``): the same steps, done by the human."""
    lines = [
        f"1. Upload {inputs.gerber.name} (sha256 {inputs.sha256.get('gerber', '?')[:12]}…)",
        f"2. Layers {spec.layers}, thickness {spec.thickness_mm} mm, outer copper "
        f"{spec.outer_copper_oz} oz"
        + (f", inner copper {spec.inner_copper_oz} oz" if spec.inner_copper_oz else ""),
        f"3. Quantity {spec.quantity}",
        f"4. Enable PCBA: {_PCBA_TYPE[spec.has_tht]}, assembly side {spec.assembly_side}",
        f"5. Upload {inputs.bom.name} and {inputs.cpl.name}",
        "6. Download JLC's part-matching export and check it with the factory-match gate",
        "7. Select every THT line; confirm no part was substituted or left unselected",
        "8. Review the gerber viewer / DFM result on the page; it is the first look at the "
        "fab's rendering of your bytes",
        "9. Record that review: kct tapeout-sign-off --stage fab-review (cart stays blocked "
        "without it)",
        "10. Save to cart and STOP; place the order yourself",
    ]
    return lines


#: A lock older than this is presumed abandoned (a crashed or killed driver).
LOCK_STALE_AFTER_S = 2 * 60 * 60


def default_lock_dir() -> Path:
    """Where :func:`browser_lock` keeps its lock files (``~/.kct/locks``)."""
    return Path.home() / ".kct" / "locks"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


@contextmanager
def browser_lock(
    port: int,
    *,
    lock_dir: Path | None = None,
    stale_after_s: float = LOCK_STALE_AFTER_S,
    now: Callable[[], float] = time.time,
    pid_alive: Callable[[int], bool] = _pid_alive,
) -> Iterator[Path]:
    """Hold the one-driver-per-debug-browser lock for CDP ``port``.

    A second driver attached to the same Chrome could interleave actions on
    the operator's order mid-flow, so this refuses (``DriverAbort("lock")``)
    while another driver holds the lock. A lock is taken over only when it is
    stale: older than ``stale_after_s``, written by a dead process on this
    host, or unreadable. The lock file is removed on exit only if it is still
    ours. Nothing here touches the network.
    """
    directory = lock_dir or default_lock_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"jlc-cdp-{port}.lock"
    host = platform.node()
    mine = {"pid": os.getpid(), "host": host, "port": port, "acquired_at": now()}
    payload = json.dumps(mine).encode()
    for attempt in range(2):
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            try:
                held = json.loads(path.read_text())
                age = now() - float(held["acquired_at"])
                holder = int(held["pid"])
                same_host = held.get("host") == host
            except (OSError, ValueError, KeyError, TypeError):
                held, age, holder, same_host = {}, float("inf"), -1, True
            stale = age > stale_after_s or (same_host and not pid_alive(holder))
            if not stale or attempt:
                raise DriverAbort(
                    "lock",
                    f"another driver holds the CDP port {port} browser "
                    f"(pid {held.get('pid', '?')} on {held.get('host', '?')}, "
                    f"{int(age)}s ago); wait for it, or remove {path} if it is "
                    "certainly dead",
                ) from None
            path.unlink(missing_ok=True)
            continue
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        break
    try:
        yield path
    finally:
        try:
            if json.loads(path.read_text()).get("pid") == mine["pid"]:
                path.unlink()
        except (OSError, ValueError):
            pass


def connect_attended_page(
    port: int = 9222,
) -> tuple[SitePage, Callable[[], None]]:  # pragma: no cover
    """Attach to the operator's own Chrome over CDP (dedicated profile, never launched here).

    Needs the optional ``playwright`` package. Chrome must already be running
    with ``--remote-debugging-port=<port> --user-data-dir=<dedicated dir>``.
    """
    require_attended()
    try:
        from playwright.sync_api import sync_playwright  # type: ignore[import-not-found]
    except ImportError as exc:
        raise DriverAbort(
            "connect",
            "playwright is not installed; run pip install 'kicad-tools[submit]' (or use --guided)",
        ) from exc
    pw = sync_playwright().start()
    try:
        browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
    except Exception as exc:
        pw.stop()
        raise DriverAbort(
            "connect",
            f"cannot attach to Chrome on port {port}. Start it with "
            f"--remote-debugging-port={port} --user-data-dir=<dedicated profile dir>, "
            "sign in, and make sure a tab is open on the quote page (a Chrome with "
            "every window closed has no tab and CDP attach fails; issue #8629)",
        ) from exc
    pages = [pg for ctx in browser.contexts for pg in ctx.pages]
    if not pages:
        pw.stop()
        raise DriverAbort(
            "connect",
            f"the debug Chrome on port {port} has no open tab; open a tab in it "
            "and navigate to the quote page",
        )
    return _PlaywrightPage(pages[0]), pw.stop


class _PlaywrightPage:  # pragma: no cover - thin adapter, exercised only in a live attended run
    def __init__(self, page: Any):
        self._p = page

    @property
    def url(self) -> str:
        return str(self._p.url)

    def exists(self, selector: str) -> bool:
        return bool(self._p.query_selector(selector))

    # JLC's options are chip/button groups, not <select>s (issue #8629). A
    # ``<select>`` is still handled; otherwise the selector names the chip group
    # container and each child is a chip. The chip structure and the "selected"
    # markers below are UNVERIFIED until recorded from a live attended run.
    def options(self, selector: str) -> Sequence[str]:
        return list(
            self._p.eval_on_selector(
                selector,
                "e => (e.tagName==='SELECT' ? [...e.options] : [...e.children])"
                ".map(x => x.textContent.trim())",
            )
        )

    def select(self, selector: str, value: str) -> None:
        is_select = self._p.eval_on_selector(selector, "e => e.tagName==='SELECT'")
        if is_select:
            self._p.select_option(selector, label=value)
            return
        chips = self._p.query_selector(selector).query_selector_all(":scope > *")
        for chip in chips:
            if chip.text_content().strip() == value:
                chip.click()
                return
        raise DriverAbort("select", f"no chip {value!r} under {selector}")

    def value(self, selector: str) -> str:
        return str(
            self._p.eval_on_selector(
                selector,
                """e => {
                  if (e.tagName==='SELECT') return e.selectedOptions[0].textContent.trim();
                  const on = [...e.children].find(c =>
                    c.getAttribute('aria-pressed')==='true' ||
                    c.getAttribute('aria-checked')==='true' ||
                    /(^|[-_\\s])(active|selected|checked)([-_\\s]|$)/i.test(c.className));
                  return on ? on.textContent.trim()
                            : (e.value || e.textContent || e.href || '').trim();
                }""",
            )
        )

    def text(self, selector: str) -> str:
        return str(self._p.inner_text(selector))

    def click(self, selector: str) -> None:
        self._p.click(selector)

    def set_file(self, selector: str, path: Path) -> None:
        self._p.set_input_files(selector, str(path))

    def wait_settled(self) -> None:
        self._p.wait_for_load_state("networkidle")

    def screenshot(self, path: Path) -> None:
        self._p.screenshot(path=str(path))
