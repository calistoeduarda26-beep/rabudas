/* Helpers partilhados. Nenhuma chave privada e usada aqui: a unica
   configuracao recebida vem de /api/config (valores publicos). */
(function () {
  let configCache = null;

  async function loadConfig() {
    if (configCache) return configCache;
    const response = await fetch("/api/config", { credentials: "include" });
    if (!response.ok) throw new Error("Nao foi possivel carregar a configuracao.");
    configCache = await response.json();
    return configCache;
  }

  async function api(path, options = {}) {
    const { method = "GET", body, raw = false } = options;
    const response = await fetch(path, {
      method,
      credentials: "include",
      headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
    if (response.status === 401) {
      window.location.href = "/";
      throw new Error("Sessao expirada.");
    }
    if (!raw) {
      const text = await response.text();
      let data = null;
      try {
        data = text ? JSON.parse(text) : null;
      } catch (e) {
        data = { detail: text };
      }
      if (!response.ok) {
        const detail = (data && (data.detail || data.message)) || `Erro ${response.status}`;
        throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
      }
      return data;
    }
    if (!response.ok) throw new Error(`Erro ${response.status}`);
    return response;
  }

  function toast(message, kind = "ok") {
    const el = document.getElementById("toast");
    if (!el) {
      window.alert(message);
      return;
    }
    el.textContent = message;
    el.className = `toast ${kind}`;
    clearTimeout(el._timer);
    el._timer = window.setTimeout(() => el.classList.add("hidden"), 4200);
  }

  function money(value) {
    const n = Number(value || 0);
    return `${n.toFixed(2)} MT`;
  }

  function fmtDate(value) {
    if (!value) return "—";
    const d = new Date(value);
    if (Number.isNaN(d.getTime())) return String(value);
    return d.toLocaleString("pt-PT", { dateStyle: "short", timeStyle: "short" });
  }

  function badge(status) {
    return `<span class="badge ${status}">${status}</span>`;
  }

  async function logout() {
    try {
      await api("/api/auth/logout", { method: "POST" });
    } catch (e) {
      /* ignora */
    }
    window.location.href = "/";
  }

  async function requireSession() {
    try {
      return await api("/api/auth/me");
    } catch (e) {
      window.location.href = "/";
      return null;
    }
  }

  window.App = { loadConfig, api, toast, money, fmtDate, badge, logout, requireSession };
})();
