"use strict";

(() => {
  const REFRESH_MS = 30_000;
  const numberFormatter = new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 2 });
  const quantityFormatter = new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 6 });
  const percentFormatter = new Intl.NumberFormat("ko-KR", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const timeFormatter = new Intl.DateTimeFormat("ko-KR", {
    timeZone: "Asia/Seoul", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
  });
  const get = (id) => document.getElementById(id);
  const refreshButton = get("refresh-button");
  const autoRefresh = get("auto-refresh");
  const accountSelect = get("account-select");
  const accountOptions = get("account-options");
  const accountPicker = accountSelect.parentElement;
  const ACCOUNT_STORAGE_KEY = "kis-dashboard-account";
  let selectedAccountId = null;
  try { selectedAccountId = localStorage.getItem(ACCOUNT_STORAGE_KEY) || null; } catch { /* Storage may be unavailable. */ }
  let accounts = [];
  let catalogLoaded = false;
  let selectionInitialized = selectedAccountId !== null;
  let inFlight = false;
  let hasData = false;
  let timer = null;
  let generation = 0;
  let activeController = null;
  let activeAccountIndex = -1;
  let accountSearch = "";
  let accountSearchAt = 0;

  function numeric(value) {
    if (value === null || value === undefined || value === "" || typeof value === "boolean") return null;
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }

  function number(value, signed = false) {
    const parsed = numeric(value);
    return parsed === null ? "—" : `${signed && parsed > 0 ? "+" : ""}${numberFormatter.format(parsed)}`;
  }

  function percent(value) {
    const parsed = numeric(value);
    return parsed === null ? "—" : `${parsed > 0 ? "+" : ""}${percentFormatter.format(parsed)}%`;
  }

  function setSign(element, value) {
    const parsed = numeric(value);
    element.classList.toggle("gain", parsed !== null && parsed > 0);
    element.classList.toggle("loss", parsed !== null && parsed < 0);
  }

  function setStatus(label, state) {
    get("connection-label").textContent = label;
    get("connection-status").className = `status-badge status-${state}`;
  }

  function setTimestamp(value) {
    const element = get("updated-at");
    const date = value ? new Date(value) : null;
    if (date && Number.isFinite(date.getTime())) {
      element.textContent = `${timeFormatter.format(date)} KST`;
      element.dateTime = date.toISOString();
    } else {
      element.textContent = "—";
      element.removeAttribute("datetime");
    }
  }

  function appendText(parent, tag, text, className) {
    const element = document.createElement(tag);
    element.textContent = text;
    if (className) element.className = className;
    parent.append(element);
    return element;
  }

  function renderHoldings(holdings) {
    const body = get("holdings-body");
    const fragment = document.createDocumentFragment();
    for (const holding of holdings) {
      const row = document.createElement("tr");
      const nameCell = document.createElement("td");
      appendText(nameCell, "span", holding.name || "종목명 없음", "stock-name");
      appendText(nameCell, "span", holding.symbol || "—", "stock-symbol");
      row.append(nameCell);
      const quantity = numeric(holding.quantity);
      appendText(row, "td", quantity === null ? "—" : `${quantityFormatter.format(quantity)}주`, "holding-quantity");
      appendText(row, "td", number(holding.avg_price), "holding-average");
      appendText(row, "td", number(holding.price), "holding-price");
      appendText(row, "td", number(holding.market_value), "holding-value");
      const pnlCell = document.createElement("td");
      pnlCell.className = "holding-pnl";
      const pnl = appendText(pnlCell, "span", number(holding.pnl, true), "cell-primary");
      const returnValue = appendText(pnlCell, "span", percent(holding.return_pct), "cell-secondary");
      setSign(pnl, holding.pnl);
      setSign(returnValue, holding.return_pct);
      row.append(pnlCell);
      fragment.append(row);
    }
    body.replaceChildren(fragment);
    get("holdings-count").textContent = String(holdings.length);
    get("holdings-table-container").hidden = holdings.length === 0;
    get("holdings-empty").hidden = holdings.length !== 0;
    get("empty-title").textContent = "아직 보유한 주식이 없어요";
  }

  function renderAccount(data) {
    get("total-value").textContent = number(data.summary.total_value);
    get("cash").textContent = number(data.summary.cash);
    get("securities-value").textContent = number(data.summary.securities_value);
    get("unrealized-pnl").textContent = number(data.summary.unrealized_pnl, true);
    get("unrealized-return").textContent = percent(data.summary.unrealized_return_pct);
    setSign(get("unrealized-pnl"), data.summary.unrealized_pnl);
    setSign(get("unrealized-return"), data.summary.unrealized_return_pct);
    renderHoldings(data.holdings);
    setTimestamp(data.updated_at);
    hasData = true;
  }

  function clearAccount() {
    hasData = false;
    for (const id of ["total-value", "cash", "securities-value", "unrealized-pnl", "unrealized-return"]) {
      get(id).textContent = "—";
      setSign(get(id), null);
    }
    setTimestamp(null);
    get("holdings-body").replaceChildren();
    get("holdings-count").textContent = "—";
    get("holdings-table-container").hidden = true;
    get("holdings-empty").hidden = false;
    get("empty-title").textContent = "계좌를 불러오고 있어요";
    get("error-notice").hidden = true;
    get("error-notice").textContent = "";
    get("refresh-announcement").textContent = "";
    setStatus("계좌 연결 중", "loading");
  }

  function selectedAccount() {
    return accounts.find((account) => account.id === selectedAccountId);
  }

  function renderAccountChoices() {
    closeAccountOptions();
    const fragment = document.createDocumentFragment();
    accounts.forEach((account, index) => {
      const option = document.createElement("li");
      option.id = `account-option-${index}`;
      option.className = "account-option";
      option.dataset.index = String(index);
      option.setAttribute("role", "option");
      option.setAttribute("aria-selected", String(account.id === selectedAccountId));
      option.setAttribute("aria-disabled", String(!account.configured));
      appendText(option, "span", account.name, "account-option-name");
      if (!account.configured) appendText(option, "span", "설정 필요", "account-option-meta");
      fragment.append(option);
    });
    accountOptions.replaceChildren(fragment);
    get("account-select-value").textContent = selectedAccount()?.name
      || (accounts.length ? "계좌 선택" : "등록된 계좌 없음");
    accountSelect.disabled = accounts.length === 0;
    get("account-label").textContent = selectedAccount()?.name || "계좌 선택 필요";
  }

  function closeAccountOptions() {
    accountOptions.hidden = true;
    accountSelect.setAttribute("aria-expanded", "false");
    accountSelect.removeAttribute("aria-activedescendant");
    activeAccountIndex = -1;
    accountSearch = "";
  }

  function activateAccount(index) {
    activeAccountIndex = index;
    Array.from(accountOptions.children).forEach((option, optionIndex) => {
      option.classList.toggle("is-active", optionIndex === index);
    });
    const option = accountOptions.children[index];
    if (option) {
      accountSelect.setAttribute("aria-activedescendant", option.id);
      option.scrollIntoView({ block: "nearest" });
    } else {
      accountSelect.removeAttribute("aria-activedescendant");
    }
  }

  function openAccountOptions(fromEnd = false) {
    if (accountSelect.disabled) return;
    const availableWidth = document.documentElement.clientWidth - accountPicker.getBoundingClientRect().left - 16;
    accountOptions.style.maxWidth = `${Math.max(accountPicker.clientWidth, availableWidth)}px`;
    accountOptions.hidden = false;
    accountSelect.setAttribute("aria-expanded", "true");
    const selectedIndex = accounts.findIndex((account) => account.id === selectedAccountId && account.configured);
    const enabled = accounts.map((account, index) => account.configured ? index : -1).filter((index) => index >= 0);
    activateAccount(selectedIndex >= 0 ? selectedIndex : (fromEnd ? enabled.at(-1) : enabled[0]) ?? -1);
  }

  function chooseAccount(index) {
    const account = accounts[index];
    if (!account?.configured) return;
    closeAccountOptions();
    accountSelect.focus();
    if (account.id === selectedAccountId) return;
    selectedAccountId = account.id;
    selectionInitialized = true;
    try { localStorage.setItem(ACCOUNT_STORAGE_KEY, selectedAccountId); } catch { /* Selection still works without persistence. */ }
    renderAccountChoices();
    clearAccount();
    refreshAccount({ switchAccount: true });
  }

  function showSelectionIssue() {
    clearAccount();
    const account = selectedAccount();
    if (account) {
      showError("이 계좌는 설정이 필요해요. 계좌 설정을 완료한 뒤 새로고침해 주세요.");
      get("empty-title").textContent = "계좌 설정이 필요해요";
    } else {
      showError(selectedAccountId
        ? "선택했던 계좌가 목록에 없어요. 조회할 계좌를 다시 선택해 주세요."
        : "조회할 계좌를 선택해 주세요. 등록된 계좌가 없다면 계좌 설정 후 새로고침해 주세요.");
      get("empty-title").textContent = "계좌를 선택해 주세요";
    }
    setStatus("계좌 선택 확인", "stale");
  }

  function showError(message) {
    if (!catalogLoaded) get("account-select-value").textContent = "계좌 목록 확인 필요";
    const notice = get("error-notice");
    notice.textContent = hasData ? `${message} 이전 조회 데이터를 표시하고 있습니다.` : message;
    notice.hidden = false;
    setStatus(hasData ? "갱신 지연 · 이전 데이터" : "연결 확인 필요", "stale");
    if (!hasData) {
      get("empty-title").textContent = "계좌 정보를 불러오지 못했습니다";
    }
    get("refresh-announcement").textContent = "계좌 조회에 실패했습니다.";
  }

  async function refreshAccount({ reloadAccounts = false, switchAccount = false } = {}) {
    if ((inFlight && !switchAccount) || document.hidden) return;
    const requestGeneration = ++generation;
    activeController?.abort();
    const controller = new AbortController();
    activeController = controller;
    inFlight = true;
    refreshButton.disabled = true;
    refreshButton.setAttribute("aria-busy", "true");
    get("refresh-label").textContent = "조회 중";
    get("account-summary").setAttribute("aria-busy", "true");
    const timeout = window.setTimeout(() => controller.abort(), 25_000);
    const isCurrent = () => requestGeneration === generation;
    try {
      const options = {
        headers: { Accept: "application/json", "X-KIS-Dashboard": "1" }, cache: "no-store", signal: controller.signal,
      };
      if (reloadAccounts || !catalogLoaded) {
        const catalogResponse = await fetch("/api/accounts", options);
        const catalog = await catalogResponse.json();
        if (!isCurrent()) return;
        if (!catalogResponse.ok) {
          showError(typeof catalog?.error === "string" && catalog.error
            ? catalog.error : "계좌 목록을 불러오지 못했습니다. 설정을 확인한 뒤 새로고침해 주세요.");
          return;
        }
        if (!Array.isArray(catalog?.accounts)
          || catalog.accounts.some((account) => !account || typeof account.id !== "string"
            || typeof account.name !== "string" || typeof account.configured !== "boolean")) {
          throw new Error("invalid account catalog");
        }
        accounts = catalog.accounts;
        catalogLoaded = true;
        if (!selectionInitialized) {
          selectedAccountId = typeof catalog.default_account === "string" ? catalog.default_account : null;
          selectionInitialized = true;
        }
        renderAccountChoices();
      }
      if (!selectedAccount()?.configured) {
        showSelectionIssue();
        return;
      }
      const requestAccountId = selectedAccountId;
      const response = await fetch(`/api/account?account=${encodeURIComponent(requestAccountId)}`, options);
      const data = await response.json();
      if (!isCurrent() || requestAccountId !== selectedAccountId) return;
      if (response.status === 404) {
        clearAccount();
        showError("선택한 계좌를 찾을 수 없어요. 새로고침 후 계좌를 다시 선택해 주세요.");
        return;
      }
      if (!data || (data.status !== "ok" && data.status !== "error")) {
        throw new Error("invalid response");
      }
      if (data.account?.id !== requestAccountId) {
        clearAccount();
        throw new Error("account mismatch");
      }
      if (data.status === "error" && !data.updated_at && !data.stale) clearAccount();
      const validSnapshot = data.summary && Array.isArray(data.holdings) && data.updated_at;
      if (validSnapshot) renderAccount(data);
      if (!response.ok || data.status === "error" || data.stale) {
        showError(typeof data.error === "string" && data.error ? data.error : "계좌 정보를 갱신하지 못했습니다.");
      } else if (validSnapshot) {
        get("error-notice").hidden = true;
        get("error-notice").textContent = "";
        setStatus("계좌 연결됨", "connected");
        get("refresh-announcement").textContent = "계좌 정보가 갱신되었습니다.";
      } else {
        throw new Error("missing account snapshot");
      }
    } catch {
      if (!isCurrent()) return;
      showError("계좌를 불러오지 못했습니다. 서버 연결 상태를 확인한 뒤 다시 조회해 주세요.");
    } finally {
      window.clearTimeout(timeout);
      if (isCurrent()) {
        inFlight = false;
        activeController = null;
        refreshButton.disabled = false;
        refreshButton.setAttribute("aria-busy", "false");
        get("refresh-label").textContent = "새로고침";
        get("account-summary").setAttribute("aria-busy", "false");
      }
    }
  }

  function scheduleRefresh() {
    if (timer !== null) window.clearInterval(timer);
    timer = null;
    if (autoRefresh.checked && !document.hidden) timer = window.setInterval(() => refreshAccount(), REFRESH_MS);
  }

  refreshButton.addEventListener("click", () => refreshAccount({ reloadAccounts: true }));
  accountSelect.addEventListener("click", () => {
    if (accountOptions.hidden) openAccountOptions();
    else closeAccountOptions();
  });
  accountSelect.addEventListener("keydown", (event) => {
    if (event.isComposing || event.ctrlKey || event.metaKey || event.altKey) return;
    const open = !accountOptions.hidden;
    if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
      event.preventDefault();
      if (!open) openAccountOptions(event.key === "ArrowUp" || event.key === "End");
      const enabled = accounts.map((account, index) => account.configured ? index : -1).filter((index) => index >= 0);
      if (!enabled.length) return;
      let position = enabled.indexOf(activeAccountIndex);
      if (event.key === "Home") position = 0;
      else if (event.key === "End") position = enabled.length - 1;
      else if (open) position = (position + (event.key === "ArrowDown" ? 1 : -1) + enabled.length) % enabled.length;
      activateAccount(enabled[position]);
    } else if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      if (open) chooseAccount(activeAccountIndex);
      else openAccountOptions();
    } else if (event.key === "Escape" && open) {
      event.preventDefault();
      closeAccountOptions();
    } else if (event.key === "Tab") {
      closeAccountOptions();
    } else if (event.key.length === 1) {
      event.preventDefault();
      if (!open) openAccountOptions();
      const now = Date.now();
      accountSearch = (now - accountSearchAt < 700 ? accountSearch : "") + event.key.toLocaleLowerCase();
      accountSearchAt = now;
      const match = accounts.findIndex((account) => account.configured && account.name.toLocaleLowerCase().startsWith(accountSearch));
      if (match >= 0) activateAccount(match);
    }
  });
  accountOptions.addEventListener("pointerdown", (event) => event.preventDefault());
  accountOptions.addEventListener("click", (event) => {
    const option = event.target.closest(".account-option");
    if (option) chooseAccount(Number(option.dataset.index));
  });
  accountOptions.addEventListener("pointermove", (event) => {
    const option = event.target.closest(".account-option");
    if (option && option.getAttribute("aria-disabled") !== "true") activateAccount(Number(option.dataset.index));
  });
  document.addEventListener("pointerdown", (event) => {
    if (!accountPicker.contains(event.target)) closeAccountOptions();
  });
  accountPicker.addEventListener("focusout", (event) => {
    if (!accountPicker.contains(event.relatedTarget)) closeAccountOptions();
  });
  window.addEventListener("resize", closeAccountOptions);
  autoRefresh.addEventListener("change", scheduleRefresh);
  document.addEventListener("visibilitychange", () => {
    scheduleRefresh();
    if (!document.hidden && autoRefresh.checked) refreshAccount();
  });
  scheduleRefresh();
  refreshAccount();
})();
