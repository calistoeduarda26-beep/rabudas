/* Login e registo.
   Caminho principal: supabase-js no navegador com a chave PUBLICA.
   Caminho alternativo (se o CDN nao carregar): endpoints /api/auth/* do backend,
   que tambem usam apenas a chave publica. */
(async function () {
  const message = document.getElementById("auth-message");
  const rateEl = document.getElementById("rate");

  function say(text, kind = "") {
    message.textContent = text;
    message.className = `message ${kind}`;
  }

  let cfg;
  try {
    cfg = await App.loadConfig();
    rateEl.textContent = cfg.rateMtPerUsdt;
  } catch (e) {
    say("Nao foi possivel falar com o servidor.", "error");
    return;
  }

  let sb = null;
  if (window.supabase && cfg.supabaseUrl && cfg.supabasePublishableKey) {
    try {
      sb = window.supabase.createClient(cfg.supabaseUrl, cfg.supabasePublishableKey);
    } catch (e) {
      sb = null;
    }
  }

  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
      document.querySelectorAll(".pane").forEach((p) => p.classList.remove("active"));
      tab.classList.add("active");
      document.getElementById(`form-${tab.dataset.tab}`).classList.add("active");
      say("");
    });
  });

  async function finishWithAccessToken(accessToken, email) {
    await App.api("/api/auth/session", { method: "POST", body: { access_token: accessToken, email } });
    window.location.href = "/dashboard.html";
  }

  document.getElementById("form-login").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = new FormData(event.target);
    const email = form.get("email").trim();
    const password = form.get("password");
    say("A entrar…");
    try {
      if (sb) {
        const { data, error } = await sb.auth.signInWithPassword({ email, password });
        if (error) throw new Error(error.message);
        await finishWithAccessToken(data.session.access_token, email);
      } else {
        await App.api("/api/auth/login", { method: "POST", body: { email, password } });
        window.location.href = "/dashboard.html";
      }
    } catch (e) {
      say(e.message || "Credenciais invalidas.", "error");
    }
  });

  document.getElementById("form-signup").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = new FormData(event.target);
    const email = form.get("email").trim();
    const password = form.get("password");
    const fullName = (form.get("full_name") || "").trim();
    say("A criar conta…");
    try {
      if (sb) {
        const { data, error } = await sb.auth.signUp({
          email,
          password,
          options: { data: { full_name: fullName } },
        });
        if (error) throw new Error(error.message);
        if (!data.session) {
          say("Conta criada. Confirma o email e depois faz login.", "ok");
          return;
        }
        await finishWithAccessToken(data.session.access_token, email);
      } else {
        await App.api("/api/auth/signup", { method: "POST", body: { email, password, full_name: fullName } });
        window.location.href = "/dashboard.html";
      }
    } catch (e) {
      say(e.message || "Nao foi possivel criar a conta.", "error");
    }
  });
})();
