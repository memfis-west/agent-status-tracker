(function () {
  const KEY = "tracker_refresh_ms";
  const select = document.getElementById("refresh-interval");
  if (!select) return;

  let timer = null;

  function formatInterval(ms) {
    if (ms >= 60000) return Math.round(ms / 60000) + " min";
    if (ms >= 1000) return Math.round(ms / 1000) + " sec";
    return ms + " ms";
  }

  function updateRefreshUI() {
    const ms = parseInt(select.value, 10) || 0;
    const statusEl = document.getElementById("refresh-status");
    const lastEl = document.getElementById("last-refreshed");
    const warnEl = document.getElementById("refresh-warning");

    if (statusEl) {
      statusEl.innerHTML = ms > 0
        ? "Auto: <strong>on</strong> (" + formatInterval(ms) + ")"
        : "Auto: <strong>off</strong>";
    }
    if (lastEl) {
      const now = new Date();
      lastEl.textContent = "Last updated: " + now.toLocaleTimeString();
    }
    if (warnEl) {
      warnEl.classList.remove("visible");
    }
  }

  function apply() {
    const ms = parseInt(select.value, 10) || 0;
    localStorage.setItem(KEY, String(ms));
    if (timer) {
      clearInterval(timer);
      timer = null;
    }
    if (ms > 0) {
      timer = setInterval(function () {
        try {
          sessionStorage.setItem("tracker_refresh_pending", String(Date.now()));
        } catch (e) { /* ignore */ }
        window.location.reload();
      }, ms);
    }
    updateRefreshUI();
  }

  select.addEventListener("change", apply);

  const saved = localStorage.getItem(KEY);
  if (saved !== null && select.querySelector('option[value="' + saved + '"]')) {
    select.value = saved;
  }

  try {
    const pending = sessionStorage.getItem("tracker_refresh_pending");
    if (pending) {
      sessionStorage.removeItem("tracker_refresh_pending");
      const elapsed = Date.now() - parseInt(pending, 10);
      const ms = parseInt(select.value, 10) || 0;
      if (elapsed > ms * 3 && ms > 0) {
        const warnEl = document.getElementById("refresh-warning");
        if (warnEl) warnEl.classList.add("visible");
      }
    }
  } catch (e) { /* ignore */ }

  apply();
  window.addEventListener("pageshow", updateRefreshUI);
})();
