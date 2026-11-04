/* Painel do utilizador: depositos e levantamentos. */
(async function () {
  const me = await App.requireSession();
  if (!me) return;

  document.getElementById("whoami").textContent = me.email;
  document.getElementById("logout").addEventListener("click", App.logout);
  if (me.role === "admin") document.getElementById("admin-link").classList.remove("hidden");

  const cfg = await App.loadConfig();
  document.getElementById("rate").textContent = cfg.rateMtPerUsdt;

  const balanceEl = document.getElementById("balance");
  const amountInput = document.querySelector('#form-deposit [name="amount_mt"]');
  const quoteEl = document.getElementById("quote-usdt");

  function setBalance(value) {
    balanceEl.textContent = App.money(value);
  }
  setBalance(me.balance_mt);

  async function refreshQuote() {
    const amount = Number(amountInput.value || 0);
    if (!amount) {
      quoteEl.textContent = "—";
      return;
    }
    try {
      const q = await App.api(`/api/payments/quote?amount_mt=${amount}`);
      quoteEl.textContent = q.amount_usdt;
      document.getElementById("rate").textContent = q.rate_mt_per_usdt;
    } catch (e) {
      quoteEl.textContent = "—";
    }
  }
  amountInput.addEventListener("input", refreshQuote);
  refreshQuote();

  /* ---------------- depositos ---------------- */
  document.getElementById("form-deposit").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = new FormData(event.target);
    try {
      const payment = await App.api("/api/payments", {
        method: "POST",
        body: {
          amount_mt: Number(form.get("amount_mt")),
          pay_currency: form.get("pay_currency"),
        },
      });
      const box = document.getElementById("payment-box");
      document.getElementById("pay-amount").textContent = `${payment.amount_usdt} ${payment.pay_currency}`;
      document.getElementById("pay-address").textContent = payment.pay_address || "a aguardar endereco…";
      document.getElementById("pay-extra").textContent = payment.payin_extra_id
        ? `Memo / extra ID: ${payment.payin_extra_id}`
        : `Ordem: ${payment.order_id}`;
      const link = document.getElementById("pay-link");
      if (payment.payment_url) {
        link.href = payment.payment_url;
        link.classList.remove("hidden");
      } else {
        link.classList.add("hidden");
      }
      box.classList.remove("hidden");
      App.toast("Pagamento criado. Envia o valor exacto.", "ok");
      loadPayments();
    } catch (e) {
      App.toast(e.message, "error");
    }
  });

  /* ---------------- levantamentos ---------------- */
  document.getElementById("form-payout").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = new FormData(event.target);
    try {
      await App.api("/api/payouts", {
        method: "POST",
        body: {
          amount_mt: Number(form.get("amount_mt")),
          method: form.get("method"),
          phone: form.get("phone"),
          holder_name: form.get("holder_name") || "",
        },
      });
      event.target.reset();
      App.toast("Pedido de levantamento enviado.", "ok");
      loadPayouts();
      refreshMe();
    } catch (e) {
      App.toast(e.message, "error");
    }
  });

  /* ---------------- tabelas ---------------- */
  async function loadPayments(sync = false) {
    const tbody = document.querySelector("#table-payments tbody");
    try {
      let rows = await App.api("/api/payments?limit=25");
      if (sync) {
        rows = await Promise.all(
          rows.map(async (row) => {
            if (row.status === "paid") return row;
            try {
              return await App.api(`/api/payments/${row.id}`);
            } catch (e) {
              return row;
            }
          })
        );
      }
      if (!rows.length) {
        tbody.innerHTML = '<tr><td colspan="5" class="muted">Ainda nao ha depositos.</td></tr>';
        return;
      }
      tbody.innerHTML = rows
        .map(
          (p) => `<tr>
            <td>${App.fmtDate(p.created_at)}</td>
            <td>${App.money(p.amount_mt)}</td>
            <td>${p.amount_usdt} ${p.pay_currency}</td>
            <td>${App.badge(p.status)}</td>
            <td>${App.money(p.credited_mt)}</td>
          </tr>`
        )
        .join("");
    } catch (e) {
      tbody.innerHTML = `<tr><td colspan="5" class="muted">${e.message}</td></tr>`;
    }
  }

  async function loadPayouts() {
    const tbody = document.querySelector("#table-payouts tbody");
    try {
      const rows = await App.api("/api/payouts?limit=25");
      if (!rows.length) {
        tbody.innerHTML = '<tr><td colspan="5" class="muted">Ainda nao ha levantamentos.</td></tr>';
        return;
      }
      tbody.innerHTML = rows
        .map(
          (p) => `<tr>
            <td>${App.fmtDate(p.created_at)}</td>
            <td>${App.money(p.amount_mt)}</td>
            <td>${p.method}</td>
            <td class="mono">${p.phone_masked}</td>
            <td>${App.badge(p.status)}</td>
          </tr>`
        )
        .join("");
    } catch (e) {
      tbody.innerHTML = `<tr><td colspan="5" class="muted">${e.message}</td></tr>`;
    }
  }

  async function refreshMe() {
    try {
      const fresh = await App.api("/api/auth/me");
      setBalance(fresh.balance_mt);
    } catch (e) {
      /* ignora */
    }
  }

  document.getElementById("refresh-payments").addEventListener("click", async () => {
    await loadPayments(true);
    await refreshMe();
    App.toast("Estados actualizados.", "ok");
  });

  const params = new URLSearchParams(window.location.search);
  if (params.get("payment") === "success") App.toast("Pagamento confirmado pela rede.", "ok");
  if (params.get("payment") === "cancelled") App.toast("Pagamento cancelado.", "error");

  loadPayments();
  loadPayouts();
})();
