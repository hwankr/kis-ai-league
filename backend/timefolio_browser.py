"""User-invoked Timefolio browser adapter. Importing this module has no I/O.

Uses the site's visible DOM and its CSV download buttons only. It never calls
private endpoints, reads browser storage, or imports another browser's profile.
"""
import csv
from datetime import datetime, time as daytime
from decimal import Decimal
import hashlib
import io
from pathlib import Path
import re
import time
from urllib.parse import parse_qs, urlsplit

from backend.kis import KST
from backend.mirror_runtime import MirrorBlocked, MirrorRejected, MirrorUnknown
from backend.timefolio_dom import rejection_reason

ORIGIN = "https://contest.timefolio.net"
CONTEST = "RFM 13회 대회"

# data-colid and cell IDs are rendered by the site's datagrid. No JS state access.
GRID_DOM = """table => {
 const direct = (selector) => Array.from(table.querySelectorAll(selector))
     .filter(e => e.closest('table') === table);
 return {
   headers: direct('thead tr:last-child th').filter(e=>e.id).map(e=>({id:e.id,
     text:Array.from((e.querySelector('button') || e).childNodes)
       .filter(n=>n.nodeType===3).map(n=>n.textContent).join('').trim()})),
   rows: direct('tbody tr[data-rowidx]').map(r=>({cells:Object.fromEntries(
     Array.from(r.children).filter(e=>e.matches('td[data-colid]')).map(c=>[
       c.getAttribute('data-colid'), {text:c.innerText.trim(),id:c.id,title:c.title,
       errors:Array.from(c.querySelectorAll('span')).filter(e=>
         getComputedStyle(e).backgroundColor==='rgb(170, 51, 51)').map(e=>e.innerText.trim()),
       badges:Array.from(c.querySelectorAll('[title]')).map(e=>({text:e.innerText,title:e.title})),
       progress:Array.from(c.querySelectorAll('progress')).map(e=>({value:e.value,max:e.max}))}
     ]))})),
   filtered: direct('thead button span').some(e=>e.textContent.trim()),
   // Count text, when present, must also agree with the unfiltered CSV.
   count_text: table.closest('.space-y-1')?.innerText.match(/선택[^\\n]*전체[^\\n]*/)?.[0] || ''
 };
}"""


def _row_identity(row):
    cells = row["cells"]
    key = tuple((column, cell.get("id")) for column, cell in cells.items())
    if not key or not any(identity for _, identity in key):
        raise MirrorBlocked("destination_grid_row_identity_missing")
    # Live prices, weights, fills, states, badges and progress are CSV facts.
    # DOM only binds each CSV row to its rendered ID and immutable attributes.
    fixed = tuple((column, cells[column].get("text"))
                  for column in ("prodId", "sgn", "genT") if column in cells)
    return key, fixed


def _grid_identity(state):
    count = re.search(r"전체\s+([\d,]+)", state.get("count_text", ""))
    return (state["headers"], state["filtered"],
            int(count[1].replace(",", "")) if count else None,
            tuple(_row_identity(row) for row in state["rows"]))


def match_receipt(proposal, orders, *, order_id=None):
    """Associate only an exact newly observed receipt, never an ambiguous match."""
    prior = set(proposal.get("prior_order_ids", []))
    matches = []
    for order in orders:
        if order.get("verified") is not True:
            continue
        identity = order.get("order_id")
        if order_id is not None and identity != order_id:
            continue
        if order_id is None and identity in prior:
            continue
        if (order.get("symbol") == proposal["symbol"] and order.get("side") == proposal["side"]
                and Decimal(str(order.get("weight"))) == Decimal(proposal["weight"])):
            matches.append(order)
    if len(matches) != 1:
        raise MirrorUnknown("destination_receipt_ambiguous_or_missing")
    return matches[0]


class TimefolioBrowser:
    def __init__(self, profile_dir, *, contest=CONTEST, channel="chrome", now=None, sleep=None):
        if contest != CONTEST:
            raise ValueError("unsupported_contest")
        self.profile_dir = Path(profile_dir)
        self.contest, self.channel = contest, channel
        self.now = now or (lambda: datetime.now(KST))
        self.sleep = sleep or time.sleep
        self.page = self.context = self.playwright = None
        self.jobs = []
        self._prepared = None
        self._account_key = None
        self._portfolio_id = None
        self._csv_download = False

    def open(self):
        # This is called only by the explicit run/login CLI commands.
        from playwright.sync_api import sync_playwright
        self.playwright = sync_playwright().start()
        try:
            self.context = self.playwright.chromium.launch_persistent_context(
                str(self.profile_dir), channel=self.channel, headless=False,
                accept_downloads=True, viewport={"width": 1500, "height": 1000})
            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
            self.page.set_default_timeout(15000)
            # A JS confirmation is not expected from the observed submit form.
            # Any unexpected dialog is dismissed rather than blindly accepted.
            self.page.on("dialog", self._dialog)
            self.page.goto(ORIGIN, wait_until="domcontentloaded")
        except Exception:
            self.close()
            raise

    def close(self):
        if self.context is not None:
            self.context.close()
            self.context = None
        if self.playwright is not None:
            self.playwright.stop()
            self.playwright = None
        self._account_key = self._portfolio_id = self._prepared = None

    def _origin(self):
        url = urlsplit(self.page.url)
        login = url.path.casefold().startswith("/auth/") or url.path.casefold() == "/logout"
        if not login:
            login = bool(self.page.locator('input[type="password"]:visible').count()
                         and self.page.get_by_role("button", name="로그인", exact=True).count())
        if url.scheme != "https" or url.netloc != "contest.timefolio.net" or login:
            self._account_key = self._portfolio_id = self._prepared = None
            raise MirrorBlocked("timefolio_login_required")

    def _dialog(self, dialog):
        if (self._csv_download and dialog.type == "confirm"
                and re.fullmatch(r"'[^'\r\n]+\.csv' 로 다운로드할까요\?", dialog.message)):
            dialog.accept()
        else:
            dialog.dismiss()

    def _close_form(self):
        dialog = self.page.get_by_role("dialog")
        if dialog.count():
            close = dialog.locator('button[type="reset"]')
            if close.count() != 1:
                raise MirrorBlocked("unexpected_destination_dialog")
            close.click()
        self._prepared = None

    def identity(self):
        """Hash the visible disabled account identifier; don't persist its value."""
        self._origin()
        if self._account_key is not None:
            return self._account_key
        self._close_form()
        self.page.get_by_role("link", name="설정", exact=True).click()
        self.page.get_by_role("button", name="사용자 정보", exact=True).click()
        field = self.page.locator('input#email[type="email"][disabled]')
        field.wait_for(state="visible")
        value = field.input_value().strip().casefold()
        if not value or "@" not in value:
            raise MirrorBlocked("destination_account_unverified")
        identity = hashlib.sha256((ORIGIN + "\n" + value).encode()).hexdigest()
        self.page.get_by_role("link", name="주문", exact=True).click()
        self._account_key = identity
        return identity

    def _main(self, *, receipts_only=False):
        self._origin()
        self._close_form()
        self.page.get_by_role("link", name="주문", exact=True).click()
        self._binding(initialize=True)
        self.page.locator("table:has(th#w2o)" if receipts_only else "table:has(th#pos)").wait_for(state="visible")
        self.page.get_by_role("button", name=re.compile("신규 주문")).wait_for(state="visible")
        if not receipts_only:
            self.page.wait_for_function("""() => Array.from(document.querySelectorAll('fieldset'))
          .some(e => e.querySelector('legend')?.textContent==='NAV' &&
            /^[\\d,]+\\.\\d+$/.test(e.querySelector('span')?.textContent.trim() || ''))""")
        self._origin()

    def _binding(self, *, initialize=False):
        self._origin()
        if not self._account_key:
            raise MirrorBlocked("destination_account_unverified")
        select = self.page.locator("select").filter(has=self.page.locator("option", has_text=self.contest))
        if select.count() != 1:
            raise MirrorBlocked("destination_contest_unverified")
        selected = select.locator("option:checked")
        label = selected.inner_text().strip()
        matches = label == self.contest or label.endswith("] " + self.contest)
        if self._portfolio_id is None and initialize and not matches:
            options = select.locator("option").evaluate_all("es=>es.map(e=>({value:e.value,text:e.textContent.trim()}))")
            candidates = [o for o in options if o["text"] == self.contest or o["text"].endswith("] " + self.contest)]
            if len(candidates) != 1:
                raise MirrorBlocked("destination_contest_unverified")
            select.select_option(value=candidates[0]["value"])
            matches = True
        value = select.input_value()
        if not matches or not value:
            raise MirrorBlocked("destination_binding_changed")
        if self._portfolio_id is None and initialize:
            self._portfolio_id = value
        if self._portfolio_id != value:
            raise MirrorBlocked("destination_binding_changed")

    def _ui_query(self, path, action):
        def matches(response):
            url = urlsplit(response.url)
            return (url.scheme == "https" and url.netloc == "contest.timefolio.net"
                    and url.path == path and response.request.method == "GET")
        with self.page.expect_response(matches, timeout=15000) as event:
            action()
        response = event.value
        if not response.ok or response.finished() is not None:
            raise MirrorBlocked("destination_read_failed")
        self.page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")

    def _open_order(self):
        self._ui_query("/Portfolio/Session", lambda: self.page.get_by_role(
            "button", name=re.compile("신규 주문")).click())

    def _table(self, field):
        table = self.page.locator(f'table:has(> thead th[id="{field}"])')
        if table.count() != 1:
            raise MirrorBlocked("destination_grid_missing_or_ambiguous_" + field)
        return table

    def _grid_dom(self, table, expected=None):
        state = table.evaluate(GRID_DOM)
        if expected is None or len(state["rows"]) >= expected:
            return state
        # Virtual rows must be exposed by scrolling the rendered grid. Row IDs
        # are read from DOM cells, never reconstructed from private JS objects.
        scroll = """(table, top) => {
          let e=table; while(e && e!==document.body){
            const s=getComputedStyle(e);
            if(e.scrollHeight>e.clientHeight+1 && /(auto|scroll)/.test(s.overflowY)){
              const original=e.scrollTop;
              if(top!==null) e.scrollTop=top;
              return {top:e.scrollTop,original,max:e.scrollHeight-e.clientHeight,height:e.clientHeight};
            } e=e.parentElement;
          } return null;
        }"""
        info = table.evaluate(scroll, None)
        if not info:
            return state
        original, seen, ordered = info["top"], {}, []
        try:
            top = 0
            for step in range(500):
                position = table.evaluate(scroll, top)
                if not position:
                    raise MirrorBlocked("destination_grid_scroll_changed")
                self.page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
                part = table.evaluate(GRID_DOM)
                if part["headers"] != state["headers"] or part["filtered"] != state["filtered"]:
                    raise MirrorBlocked("destination_grid_changed_during_scroll")
                identities = [_row_identity(row) for row in part["rows"]]
                keys = [identity[0] for identity in identities]
                if len(set(keys)) != len(keys):
                    raise MirrorBlocked("destination_grid_changed_during_scroll")
                indices = [seen[key][0] for key in keys if key in seen]
                if indices != sorted(indices):
                    raise MirrorBlocked("destination_grid_changed_during_scroll")
                new_seen = False
                for key in keys:
                    if key not in seen:
                        new_seen = True
                    elif new_seen:
                        raise MirrorBlocked("destination_grid_changed_during_scroll")
                if new_seen and indices and indices[-1] != len(ordered) - 1:
                    raise MirrorBlocked("destination_grid_changed_during_scroll")
                for row, (key, fixed) in zip(part["rows"], identities):
                    if key in seen and seen[key][1] != fixed:
                        raise MirrorBlocked("destination_grid_changed_during_scroll")
                    if key not in seen:
                        seen[key] = (len(ordered), fixed)
                        ordered.append(row)
                    else:
                        ordered[seen[key][0]] = row
                if position["top"] >= position["max"] - 1:
                    break
                top = min(position["max"], position["top"] + max(1, position["height"] * .75))
            state["rows"] = ordered
            return state
        finally:
            table.evaluate(scroll, original)
            self.page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")

    def _grid(self, table, kind, *, parent_symbol=None):
        # Every grid has its own nearest toolbar. Nested grids are not merged.
        wrapper = table.locator('xpath=ancestor::div[contains(concat(" ",normalize-space(@class)," ")," space-y-1 ")][1]')
        button = wrapper.locator(':scope > .grid-opt-tabs').get_by_role("button", name="CSV 다운로드", exact=True)
        if button.count() != 1:
            raise MirrorBlocked("destination_csv_unavailable")
        # Require stable row identities/order, while allowing live market updates.
        expected = None
        for attempt in range(3):
            before = self._grid_dom(table, expected)
            if before["filtered"]:
                raise MirrorBlocked("destination_grid_filtered")
            self._csv_download = True
            try:
                with self.page.expect_download(timeout=15000) as event:
                    button.click()
            finally:
                self._csv_download = False
            download = event.value
            path = download.path()
            if path is None:
                raise MirrorBlocked("destination_csv_failed")
            raw = Path(path).read_text(encoding="utf-8-sig")
            download.delete()
            after = self._grid_dom(table, expected)
            if _grid_identity(before) == _grid_identity(after):
                lines = [row for row in csv.reader(io.StringIO(raw)) if row]
                count = max(0, len(lines) - 1)
                if count > len(before["rows"]) and attempt < 2:
                    expected = count
                    continue
                count_text = before.get("count_text", "")
                match = re.search(r"전체\s+([\d,]+)", count_text)
                complete = count == len(before["rows"]) and (
                    not match or int(match[1].replace(",", "")) == count)
                return {**after, "kind": kind, "csv_text": raw, "complete": complete,
                        "stable": True, **({"parent_symbol": parent_symbol} if parent_symbol else {})}
        raise MirrorBlocked("destination_changed_during_csv")

    @staticmethod
    def _validation_errors(dialog):
        return dialog.evaluate("""e=>Array.from(e.querySelectorAll('fieldset div'))
          .filter(n=>getComputedStyle(n).color==='rgb(255, 0, 0)' && n.innerText.trim())
          .map(n=>n.innerText.trim())""")

    def _select_validated(self, dialog, suggestion, symbol, *, side="buy"):
        day = dialog.locator('input[name="d"]').input_value()
        def matches(response):
            url = urlsplit(response.url)
            query = parse_qs(url.query)
            return (url.scheme == "https" and url.netloc == "contest.timefolio.net"
                    and url.path == "/Portfolio/ValidateProduct" and response.request.method == "GET"
                    and query.get("d") == [day] and query.get("prodId") == ["A" + symbol]
                    and query.get("entry") == ["true" if side == "buy" else "false"])
        # Observe completion of the request caused by the UI, never send it or
        # read its response body. Validation results are read from the form.
        with self.page.expect_response(matches, timeout=15000) as event:
            suggestion.click()
        response = event.value
        if not response.ok or response.finished() is not None:
            raise MirrorBlocked("destination_stock_validation_failed")
        self.page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")

    def _date(self):
        values = self.page.locator("input[name=d]").evaluate_all("es=>es.map(e=>e.value)")
        if not values or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", values[0]):
            raise MirrorBlocked("destination_session_date_unverified")
        return values[0]

    def _receipt_tables(self):
        tables = [self._grid(self._table("cancel"), "unaccepted")]
        targets = self._table("w2o")
        expand = targets.get_by_role("button", name="모두 열기", exact=True)
        if expand.count():
            expand.click()
        # Details are nested inside the target grid, one for each parent symbol.
        details = self.page.locator('table:has(> thead th[id="avgPx"])')
        for i in range(details.count()):
            table = details.nth(i)
            parent = table.evaluate("""t=>{
              let r=t.closest('tr'); while(r){
                let p=r.previousElementSibling;
                while(p){ const c=p.querySelector(':scope > td[data-colid="prodId"]');
                  if(c) return c.innerText.trim(); p=p.previousElementSibling; }
                r=r.parentElement?.closest('tr');
              } return null;
            }""")
            if not parent or not re.fullmatch(r"A?[A-Z0-9]{6}", parent):
                raise MirrorBlocked("destination_detail_parent_missing")
            tables.append(self._grid(table, "order_details", parent_symbol=parent.removeprefix("A")))
        self._ui_query("/Portfolio/Orders", lambda: self.page.get_by_role("button", name="주문 내역", exact=True).click())
        self._ui_query("/Portfolio/Orders", lambda: self.page.locator('label[for="period_A"]').click())
        tables.append(self._grid(self._table("genT"), "orders"))
        return tables

    def _observation(self, *, symbols=(), receipts_only=False):
        from backend.timefolio_dom import parse_receipts, parse_snapshot
        account = self._account_key or self.identity()
        self._main(receipts_only=receipts_only)
        session_date = self._date()
        tables = [] if receipts_only else [self._grid(self._table("pos"), "positions"),
                                           self._grid(self._table("w2o"), "targets")]
        tables.extend(self._receipt_tables())
        self._binding()
        if self._date() != session_date:
            raise MirrorBlocked("destination_session_date_changed")
        jobs = self.jobs_reader() if hasattr(self, "jobs_reader") else self.jobs
        known = {job["receipt"]["order_id"]: job["receipt"] for job in jobs
                 if (job.get("receipt") or {}).get("order_id")}
        payload = {"url": self.page.url, "account_key": account, "contest": self.contest,
                   "session_date": session_date, "tables": tables, "known_receipts": known}
        if receipts_only:
            return parse_receipts(payload, account, self.contest)
        return parse_snapshot(payload, symbols, account, self.contest)

    def snapshot(self, symbols):
        return self._observation(symbols=symbols)

    def receipt_snapshot(self):
        return self._observation(receipts_only=True)

    def prepare(self, proposal):
        self._origin()
        self._session(proposal)
        now = self.now().astimezone(KST)
        if proposal["account_key"] != self._account_key or proposal["contest"] != self.contest:
            raise MirrorBlocked("destination_binding_changed")
        self._binding()
        self._close_form()
        self._open_order()
        dialog = self.page.get_by_role("dialog", name="신규 주문", exact=True)
        dialog.locator('label[for="매수도_' + ("true" if proposal["side"] == "buy" else "false") + '"]').click()
        search = dialog.get_by_placeholder("종목 선택", exact=True)
        search.fill(proposal["symbol"])
        suggestion = dialog.locator("li").filter(has_text=re.compile(r"\[\s*A" + proposal["symbol"] + r"\s*\]"))
        self._select_validated(dialog, suggestion, proposal["symbol"], side=proposal["side"])
        dialog.locator('input[type="number"][step="0.01"]').fill(proposal["weight"])
        dialog.locator('label[for="prcTy_Opp"]').click()
        dialog.locator('input[type="number"][max="10"]').fill("5")
        dialog.locator('label[for="isSlice_false"]').click()
        dialog.locator("input#hm0").fill(now.strftime("%H:%M"))
        result = self._verify_form(proposal)
        self._prepared = dict(proposal)
        return result

    def _session(self, proposal):
        now = self.now().astimezone(KST)
        if (proposal["session_date"] != now.date().isoformat() or now.weekday() > 4
                or not daytime(9, 0) <= now.time().replace(tzinfo=None) < daytime(15, 19)
                or not "2026-10-01" <= proposal["session_date"] <= "2026-11-30"):
            raise MirrorBlocked("destination_outside_order_session")

    def _verify_form(self, proposal):
        self._binding()
        dialog = self.page.get_by_role("dialog", name="신규 주문", exact=True)
        errors = self._validation_errors(dialog)
        if errors:
            raise MirrorRejected(self._reason(errors))
        fields = dialog.evaluate("""e=>({symbol:e.querySelector('[placeholder="종목 선택"]').value,
          date:e.querySelector('[name=d]').value,weight:e.querySelector('[step="0.01"]').value,
          side:e.querySelector('[name="매수도"]:checked')?.value,
          kind:e.querySelector('[name=prcTy]:checked')?.value,
          slice:e.querySelector('[name=isSlice]:checked')?.value,
          level:e.querySelector('input[max="10"]')?.value,
          exitAll:!!e.querySelector('input[type=checkbox]:checked'),
          valid:Array.from(e.querySelectorAll('input')).every(i=>i.validity.valid)})""")
        if (fields["symbol"] != "A" + proposal["symbol"] or fields["date"] != proposal["session_date"]
                or Decimal(fields["weight"]) != Decimal(proposal["weight"])
                or fields["side"] != ("true" if proposal["side"] == "buy" else "false")
                or fields["kind"] != "Opp" or fields["slice"] != "false" or fields["level"] != "5"
                or fields["exitAll"] or not fields["valid"]):
            raise MirrorBlocked("destination_form_changed")
        return {**proposal, "verified": True}

    @staticmethod
    def _reason(messages):
        return rejection_reason(messages)

    def _submission_error(self):
        dialog = self.page.get_by_role("dialog", name="신규 주문", exact=True)
        if dialog.count():
            errors = self._validation_errors(dialog)
            if errors:
                raise MirrorRejected(self._reason(errors))
        alerts = self.page.locator("[data-alert-dialog]")
        messages = alerts.evaluate_all("""es=>es.filter(e=>e.querySelector('h2')?.innerText.includes('오류'))
          .map(e=>e.querySelector('p')?.innerText || '')""")
        if messages:
            # A connectivity/server failure may occur after acceptance. Only an
            # explicit business refusal is a rejection; everything else is unknown.
            text = " ".join(messages)
            if (re.search(r"불가|제한|초과|미선택|부족|금지|허용|할 수 없|가능하지", text)
                    and not re.search(r"통신|네트워크|연결|시간 초과|타임아웃|서버 오류|조회 실패", text)):
                raise MirrorRejected(self._reason(messages))
            raise MirrorUnknown("destination_submit_response_unclear")

    def submit(self, proposal):
        # Runtime commits status=submitting before calling this method.
        if self._prepared != proposal:
            raise MirrorBlocked("destination_not_prepared")
        self._session(proposal)
        self._verify_form(proposal)
        self._prepared = None
        try:
            self.page.get_by_role("dialog", name="신규 주문", exact=True).locator('button[type="submit"]').click()
        except Exception:
            raise MirrorUnknown("destination_click_outcome_unknown") from None
        # A toast is not a receipt. The unique ID and exact CSV weight must agree.
        for attempt in range(3):
            self.sleep(1)
            try:
                self._submission_error()
                snapshot = self.receipt_snapshot()
                if (snapshot["account_key"] != proposal["account_key"]
                        or snapshot["contest"] != proposal["contest"]
                        or snapshot.get("session_date") != proposal["session_date"]):
                    raise MirrorUnknown("destination_binding_changed")
                if snapshot.get("verified") is not True:
                    raise MirrorUnknown("destination_receipt_unverified")
                return match_receipt(proposal, snapshot["orders"])
            except (MirrorBlocked, MirrorUnknown):
                continue
        raise MirrorUnknown("destination_receipt_not_confirmed")

    def lookup(self, job, snapshot=None):
        proposal = job.get("proposal") or {}
        if snapshot is None:
            snapshot = self.receipt_snapshot()
        if (snapshot["account_key"] != proposal.get("account_key")
                or snapshot["contest"] != proposal.get("contest")):
            raise MirrorUnknown("destination_binding_changed")
        if snapshot.get("verified") is not True:
            raise MirrorUnknown("destination_receipt_unverified")
        receipt = job.get("receipt")
        return match_receipt(proposal, snapshot["orders"], order_id=receipt["order_id"] if receipt else None)
