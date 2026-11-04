/* Painel de administracao. */
(async function () {
  const me = await App.requireSession();
  if (!me) return;
  if (me.role !== "admin") {
    App.toast("Sem permissoes de administrador.", "error");
    window.setTimeout(() => (window.location.href = "/dashboard.html"), 1200);
    return;
  }

  document.getElementById("whoami").textContent = me.email;
  document.getElementById("logout").addEventListener("click", App.logout);

  async function loadStats() {
    try {
      const s = await App.api("/api/admin/stats");
      document.getElementById("s-users").textContent = s.users;
      document.getElementById("s-paid").textContent = s.payments_paid;
      document.getElementById("s-credited").textContent = App.money(s.total_credited_mt);
      document.getElementById("s-pending").textContent = s.payouts_pending;
      document.getElementById("s-paidout").textContent = App.money(s.total_paid_out_mt);
      document.querySelector('[name="rate_mt_per_usdt"]').value = s.rate_mt_per_usdt;
      document.getElementById("env-rate").textContent = s.rate_mt_per_usdt;
    } catch (e) {
      App.toast(e.message, "error");
    }
  }

  async function loadHealth() {
    const el = document.getElementById("health");
    try {
      const h = await App.api("/api/admin/health-check");
      el.textContent = JSON.stringify(h, null, 2);
    } catch (e) {
      el.textContent = e.message;
    }
  }

  async function loadPayouts() {
    const tbody = document.querySelector("#table-payouts tbody");
    try {
      const rows = await App.api("/api/admin/payouts?status=pending");
      if (!rows.length) {
        tbody.innerHTML = '<tr><td colspan="6" class="muted">Nada pendente.</td></tr>';
        return;
      }
      tbody.innerHTML = rows
        .map(
          (p) => `<tr data-id="${p.id}">
            <td>${App.fmtDate(p.created_at)}</td>
            <td>${p.email || "—"}</td>
            <td>${App.money(p.amount_mt)}</td>
            <td>${p.method}</td>
            <td class="mono">${p.phone_masked}${p.holder_name ? ` — ${p.holder_name}` : ""}</td>
            <td>
              <button class="ghost" data-decision="approve">Aprovar</button>
              <button class="ghost" data-decision="reject">Rejeitar</button>
            </td>
          </tr>`
        )
        .join("");
    } catch (e) {
      tbody.innerHTML = `<tr><td colspan="6" class="muted">${e.message}</td></tr>`;
    }
  }

  async function loadPayments() {
    const tbody = document.querySelector("#table-payments tbody");
    try {
      const rows = await App.api("/api/admin/payments?limit=40");
      if (!rows.length) {
        tbody.innerHTML = '<tr><td colspan="6" class="muted">Sem depositos.</td></tr>';
        return;
      }
      tbody.innerHTML = rows
        .map(
          (p) => `<tr>
            <td>${App.fmtDate(p.created_at)}</td>
            <td class="mono">${p.order_id}</td>
            <td>${App.money(p.amount_mt)}</td>
            <td>${p.amount_usdt} ${p.pay_currency}</td>
            <td>${App.badge(p.status)}</td>
            <td>${App.money(p.credited_mt)}</td>
          </tr>`
        )
        .join("");
    } catch (e) {
      tbody.innerHTML = `<tr><td colspan="6" class="muted">${e.message}</td></tr>`;
    }
  }

  async function loadUsers() {
    const tbody = document.querySelector("#table-users tbody");
    try {
      const rows = await App.api("/api/admin/users");
      tbody.innerHTML = rows
        .map(
          (u) => `<tr data-id="${u.id}">
            <td>${u.email}</td>
            <td>${u.full_name || "—"}</td>
            <td>${u.role}</td>
            <td>${App.money(u.balance_mt)}</td>
            <td><button class="ghost" data-role="${u.role === "admin" ? "user" : "admin"}">
              ${u.role === "admin" ? "Tornar utilizador" : "Tornar admin"}</button></td>
          </tr>`
        )
        .join("");
    } catch (e) {
      tbody.innerHTML = `<tr><td colspan="5" class="muted">${e.message}</td></tr>`;
    }
  }

  async function loadLogs() {
    const tbody = document.querySelector("#table-logs tbody");
    try {
      const rows = await App.api("/api/admin/login-logs?limit=40");
      if (!rows.length) {
        tbody.innerHTML = '<tr><td colspan="5" class="muted">Sem registos.</td></tr>';
        return;
      }
      tbody.innerHTML = rows
        .map(
          (l) => `<tr>
            <td>${App.fmtDate(l.created_at)}</td>
            <td>${l.email}</td>
            <td>${App.badge(l.success ? "paid" : "failed")}</td>
            <td class="small">${(l.reason || "").slice(0, 60)}</td>
            <td class="mono small">${l.ip || "—"}</td>
          </tr>`
        )
        .join("");
    } catch (e) {
      tbody.innerHTML = `<tr><td colspan="5" class="muted">${e.message}</td></tr>`;
    }
  }

  document.querySelector("#table-payouts").addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-decision]");
    if (!button) return;
    const id = button.closest("tr").dataset.id;
    const decision = button.dataset.decision;
    const note = decision === "reject" ? window.prompt("Motivo da rejeicao (o valor volta ao saldo):") || "" : "";
    try {
      await App.api(`/api/admin/payouts/${id}/decision`, {
        method: "POST",
        body: { decision, note },
      });
      App.toast(decision === "approve" ? "Levantamento aprovado." : "Levantamento rejeitado.", "ok");
      loadPayouts();
      loadStats();
    } catch (e) {
      App.toast(e.message, "error");
    }
  });

  document.querySelector("#table-users").addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-role]");
    if (!button) return;
    const id = button.closest("tr").dataset.id;
    try {
      await App.api(`/api/admin/users/${id}/role?role=${button.dataset.role}`, { method: "POST" });
      App.toast("Papel actualizado.", "ok");
      loadUsers();
    } catch (e) {
      App.toast(e.message, "error");
    }
  });

  document.getElementById("form-rate").addEventListener("submit", async (event) => {
    event.preventDefault();
    const value = Number(new FormData(event.target).get("rate_mt_per_usdt"));
    try {
      await App.api("/api/admin/rate", { method: "PUT", body: { rate_mt_per_usdt: value } });
      App.toast(`Taxa definida para ${value} MT/USDT.`, "ok");
      loadStats();
    } catch (e) {
      App.toast(e.message, "error");
    }
  });

  loadStats();
  loadHealth();
  loadPayouts();
  loadPayments();
  loadUsers();
  loadLogs();
})();
