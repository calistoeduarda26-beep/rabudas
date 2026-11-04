# rabudas

Plataforma de pagamentos: **depósitos em criptomoeda** (NOWPayments) e
**levantamentos em Meticais** (M-Pesa / e-Mola), com autenticação Supabase e
painel de administração.

- Backend: FastAPI (Python 3.11+)
- Base de dados / auth: Supabase (PostgreSQL + GoTrue)
- Pagamentos cripto: NOWPayments (USDT TRC-20 por omissão)
- Frontend: HTML/CSS/JS estático servido pelo próprio backend

---

## 1. Configuração

```bash
cp .env.example .env      # e preenche os valores
```

O `.env` está no `.gitignore` — **nunca** é commitado. Se faltar alguma
variável obrigatória, a aplicação **não arranca** e diz exactamente qual falta
(mostra nomes de variáveis, nunca valores).

| Variável | Onde pode ser usada | Obrigatória |
| --- | --- | --- |
| `NOWPAYMENTS_API_KEY` | só backend | sim |
| `NOWPAYMENTS_PUBLIC_KEY` | frontend (se necessário) | não |
| `NOWPAYMENTS_IPN_SECRET` | só backend (webhook IPN) | recomendada |
| `SUPABASE_URL` | ambos | sim |
| `SUPABASE_PUBLISHABLE_KEY` (`sb_publishable_…`) | navegador (login/registo) | sim |
| `SUPABASE_SECRET_KEY` (`sb_secret_…`) | **só backend** | sim |
| `APP_SECRET_KEY` | só backend (sessões assinadas) | sim, ≥ 32 chars |
| `BASE_URL` | ambos | não (derivado do pedido) |
| `RATE_MT_PER_USDT` | ambos (por omissão 80) | não |

Validar a configuração sem arrancar o servidor:

```bash
python -m app.config
```

Saída (repara que as chaves aparecem mascaradas):

```
INFO  rabudas.config :: Configuracao OK
INFO  rabudas.config ::   NOWPAYMENTS_API_KEY        T4VF…YC (31 chars)
INFO  rabudas.config ::   SUPABASE_SECRET_KEY        sb_s…Rv (41 chars)
...
WARN  rabudas.config ::   NOWPAYMENTS_IPN_SECRET     <vazio> - funcionalidade degradada
```

### Regras de segurança aplicadas no código

- O GoTrue é chamado com `apikey: <API key do projecto>` e
  `Authorization: Bearer <token do utilizador>`. Nunca o contrário — mandar o
  JWT do utilizador no `apikey` faz a Supabase responder 401 e parte o
  registo/login (há teste que fixa isto).
- `SUPABASE_SECRET_KEY`, `NOWPAYMENTS_API_KEY` e `APP_SECRET_KEY` são
  `pydantic.SecretStr`: não aparecem em `repr()`, em respostas da API nem em
  logs. Um filtro de logging (`app/logging_conf.py`) apaga estes valores de
  **qualquer** mensagem registada, mesmo que alguém os imprima por engano.
- `/api/config` devolve apenas valores públicos (`supabaseUrl`,
  `supabasePublishableKey`, `baseUrl`, taxa). Há testes que garantem que a
  `sb_secret_…` e a API key da NOWPayments nunca aparecem em nenhuma resposta.
- A escrita em `payments`, `payouts` e `login_logs` é feita **só** pelo backend
  com a chave secreta. O navegador apenas autentica e chama a API.
- O webhook IPN só é aceite com assinatura HMAC-SHA512 válida
  (`x-nowpayments-sig`). Sem `NOWPAYMENTS_IPN_SECRET` configurado responde 503
  e não toca na base de dados.

> ⚠️ **Rotação de chaves**: estas chaves foram partilhadas em texto plano numa
> conversa. Assim que possível, roda a `NOWPAYMENTS_API_KEY`, a
> `SUPABASE_SECRET_KEY` e a `SUPABASE_PUBLISHABLE_KEY` nos respectivos painéis
> e actualiza o `.env`.

---

## 2. Base de dados

**Opção A** — abre **Supabase → SQL Editor → New query**, cola o conteúdo de
[`supabase/schema.sql`](supabase/schema.sql) e corre.

**Opção B** — põe `POSTGRES_URL` no `.env` (porta 5432, não a 6543 do pgbouncer)
e corre:

```bash
pip install "psycopg[binary]"
python scripts/init_db.py --check   # ver o que existe
python scripts/init_db.py           # aplicar o schema
```

Sem este passo **não há tabelas**: o registo cria o utilizador em `auth.users`
mas não consegue criar o perfil, e a API responde `503` com a indicação para
correr o `schema.sql`.

O schema cria:

`profiles`, `payments`, `payouts`, `login_logs`, `settings`, as funções
atómicas (`credit_balance`, `debit_balance`, `request_payout`, `settle_payout`)
e as políticas de Row Level Security.

Sem as funções SQL a aplicação continua a funcionar (usa actualização
condicional com `balance_mt`), mas com elas as operações de saldo ficam
atómicas — recomendado em produção.

---

## 3. Deploy na Vercel

O projecto já traz `vercel.json` (builder `@vercel/python` em `main.py`, com
todas as rotas a apontar para a função) e `main.py` exporta uma instância
`app` ao nível do módulo — é isso que a Vercel procura; sem ela o build falha
com `FASTAPI_ENTRYPOINT_NOT_FOUND`.

```bash
vercel link && vercel --prod
```

Variáveis a definir em **Project → Settings → Environment Variables**
(as da Supabase já lá estão se ligaste a integração):

| Variável | Notas |
| --- | --- |
| `NOWPAYMENTS_API_KEY` | obrigatória |
| `SUPABASE_SECRET_KEY` | obrigatória |
| `APP_SECRET_KEY` | obrigatória, ≥ 32 chars |
| `SUPABASE_URL` · `SUPABASE_PUBLISHABLE_KEY` | obrigatórias |
| `NOWPAYMENTS_IPN_SECRET` | recomendada (créditos automáticos) |
| `RATE_MT_PER_USDT` | opcional (80) |
| `ENVIRONMENT` | `production` (activa cookies `Secure`) |
| `BASE_URL` | **deixa vazia** — o URL público é derivado de `x-forwarded-host`, por isso os callbacks da NOWPayments saem com o domínio real da Vercel e não com `localhost` |

Se faltar alguma obrigatória, o deploy **não rebenta**: sobe uma aplicação que
responde `503` e mostra em `/health` o nome das variáveis em falta (nomes,
nunca valores).

Depois do deploy, usa o URL da Vercel como `ipn_callback_url` no painel da
NOWPayments: `https://<o-teu-dominio>/api/webhooks/nowpayments`.

---

## 4. Arrancar

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python main.py                 # http://localhost:8000
```

Ou directamente com uvicorn:

```bash
uvicorn main:create_app --factory --host 0.0.0.0 --port 8000 --reload
```

Documentação interactiva da API em `/docs`.

---

## 5. Primeiro administrador

Regista a tua conta em `http://localhost:8000` e depois:

```bash
python scripts/make_admin.py --list          # ver utilizadores
python scripts/make_admin.py tu@exemplo.mz   # promover
```

(Ou, no SQL Editor: `update public.profiles set role='admin' where email='tu@exemplo.mz';`)

---

## 6. Fluxo de dinheiro

1. **Depósito** — o utilizador indica o valor em MT. O backend converte com a
   taxa (`RATE_MT_PER_USDT`, alterável pelo admin em `/api/admin/rate`), cria o
   pagamento na NOWPayments e mostra o endereço USDT.
2. **Confirmação** — a NOWPayments envia um IPN para
   `BASE_URL/api/webhooks/nowpayments`. A assinatura é verificada e o saldo é
   creditado **uma única vez** (idempotente). Também é possível sincronizar
   manualmente em `GET /api/payments/{id}`.
3. **Levantamento** — o utilizador pede um levantamento (mínimo 200 MT). O
   saldo é debitado na mesma transação. O admin aprova (paga por M-Pesa/e-Mola
   manualmente) ou rejeita — e nesse caso o valor **volta ao saldo**.

Para os IPN funcionarem a partir da internet, `BASE_URL` tem de ser um URL
público com HTTPS (em desenvolvimento usa um túnel, por exemplo
`ngrok http 8000`, e coloca o URL público em `BASE_URL` + no painel da
NOWPayments).

---

## 7. Endpoints

| Método | Rota | Acesso |
| --- | --- | --- |
| GET | `/health` | público |
| GET | `/api/config` | público (só valores públicos) |
| GET | `/api/payments/quote?amount_mt=` | público |
| POST | `/api/auth/signup` · `/api/auth/login` · `/api/auth/session` | público |
| GET | `/api/auth/me` · POST `/api/auth/logout` | sessão |
| POST/GET | `/api/payments` · GET `/api/payments/{id}` | sessão |
| POST/GET | `/api/payouts` | sessão |
| POST | `/api/webhooks/nowpayments` | assinatura HMAC |
| GET | `/api/admin/stats` · `/payouts` · `/payments` · `/users` · `/login-logs` · `/rate` · `/health-check` | admin |
| POST | `/api/admin/payouts/{id}/decision` · `/api/admin/users/{id}/role` · PUT `/api/admin/rate` | admin |

Autenticação: cookie httpOnly `rabudas_session` assinado com `APP_SECRET_KEY`,
ou header `Authorization: Bearer <access_token da Supabase>`.

---

## 8. Testes

```bash
pytest -q
```

113 testes. Correm contra uma Supabase e uma NOWPayments falsas em memória
(`tests/fakes.py`), por isso **não** precisam de rede nem de chaves reais e
exercitam o código real de `app/services.py`, `app/db.py` e dos routers.
Cobrem, entre outros:

- falha de arranque quando falta uma variável do `.env` ou está um placeholder;
- chaves mascaradas em `repr()`, em logs e fora de `/api/config`;
- `.env` ignorado pelo git (`git check-ignore`);
- assinatura IPN válida / adulterada / com segredo errado;
- conversão MT ↔ USDT e validação de números moçambicanos;
- depósito → IPN → crédito de saldo uma única vez;
- levantamento sem saldo (409), aprovado, rejeitado com reembolso;
- rate limiting de logins e registo em `login_logs`;
- admin recusado a utilizadores normais (403);
- cotação continua a responder quando a base de dados está inacessível;
- regressões de deploy: `main.py` exporta `app`, `vercel.json` correcto,
  configuração incompleta devolve 503 em vez de rebentar, e os callbacks da
  NOWPayments usam o URL público do pedido.

---

## 9. Estrutura

```
app/
  config.py          # pydantic-settings; falha se faltar algo obrigatório
  logging_conf.py    # logging com redacção obrigatória de segredos
  db.py              # cliente Supabase (PostgREST + GoTrue) — só backend
  nowpayments.py     # cliente NOWPayments + verificação de assinatura IPN
  security.py        # sessões assinadas, rate limiting, MT/USDT, telefones
  services.py        # regras de negócio (depósitos, créditos, levantamentos)
  schemas.py         # modelos Pydantic
  deps.py            # dependências FastAPI (auth, admin, base de dados)
  routers/           # auth, payments, payouts, admin, webhooks
static/              # index.html, dashboard.html, admin.html, css, js
supabase/schema.sql  # tabelas, funções SQL e RLS
scripts/make_admin.py
tests/               # 106 testes com serviços externos falsos
```
