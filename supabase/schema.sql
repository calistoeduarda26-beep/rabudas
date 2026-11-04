-- ============================================================
-- rabudas - esquema da base de dados (Supabase / PostgreSQL)
-- Corre isto em: Supabase Dashboard -> SQL Editor -> New query
-- ============================================================

create extension if not exists "pgcrypto";

-- ------------------------------------------------------------
-- Perfis (1:1 com auth.users)
-- ------------------------------------------------------------
create table if not exists public.profiles (
  id          uuid primary key references auth.users (id) on delete cascade,
  email       text not null,
  full_name   text not null default '',
  role        text not null default 'user' check (role in ('user', 'admin')),
  balance_mt  numeric(14, 2) not null default 0 check (balance_mt >= 0),
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);

create index if not exists profiles_role_idx on public.profiles (role);

-- ------------------------------------------------------------
-- Depositos em cripto (NOWPayments)
-- ------------------------------------------------------------
create table if not exists public.payments (
  id                text primary key,
  user_id           uuid not null references public.profiles (id) on delete cascade,
  order_id          text not null unique,
  amount_mt         numeric(14, 2) not null,
  amount_usdt       numeric(18, 8) not null,
  rate_mt_per_usdt  numeric(14, 4) not null default 80,
  pay_currency      text not null default 'usdttrc20',
  status            text not null default 'pending'
                    check (status in ('pending', 'processing', 'partial', 'paid', 'failed', 'refunded', 'expired')),
  np_payment_id     text,
  np_status         text,
  pay_address       text,
  payin_extra_id    text,
  payment_url       text,
  invoice_id        text,
  actually_paid     numeric(18, 8) not null default 0,
  outcome_amount    numeric(18, 8) not null default 0,
  outcome_currency  text,
  credited_mt       numeric(14, 2) not null default 0,
  note              text not null default '',
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now()
);

create index if not exists payments_user_idx on public.payments (user_id, created_at desc);
create index if not exists payments_status_idx on public.payments (status);

-- ------------------------------------------------------------
-- Levantamentos (M-Pesa / e-Mola)
-- ------------------------------------------------------------
create table if not exists public.payouts (
  id          text primary key,
  user_id     uuid not null references public.profiles (id) on delete cascade,
  amount_mt   numeric(14, 2) not null check (amount_mt > 0),
  method      text not null check (method in ('mpesa', 'emola')),
  phone       text not null,
  holder_name text not null default '',
  status      text not null default 'pending' check (status in ('pending', 'approved', 'rejected')),
  admin_note  text not null default '',
  decided_by  uuid references public.profiles (id),
  decided_at  timestamptz,
  created_at  timestamptz not null default now()
);

create index if not exists payouts_user_idx on public.payouts (user_id, created_at desc);
create index if not exists payouts_status_idx on public.payouts (status);

-- ------------------------------------------------------------
-- Registo de tentativas de login (auditoria)
-- ------------------------------------------------------------
create table if not exists public.login_logs (
  id         bigserial primary key,
  email      text not null,
  user_id    uuid,
  success    boolean not null default false,
  reason     text not null default '',
  ip         text not null default '',
  user_agent text not null default '',
  created_at timestamptz not null default now()
);

create index if not exists login_logs_created_idx on public.login_logs (created_at desc);
create index if not exists login_logs_email_idx on public.login_logs (email);

-- ------------------------------------------------------------
-- Definicoes chave/valor (taxa de cambio definida pelo admin)
-- ------------------------------------------------------------
create table if not exists public.settings (
  key        text primary key,
  value      text not null,
  updated_at timestamptz not null default now()
);

insert into public.settings (key, value)
values ('rate_mt_per_usdt', '80')
on conflict (key) do nothing;

-- ============================================================
-- Funcoes atomicas (usadas pelo backend; evitam condicoes de corrida)
-- ============================================================

create or replace function public.credit_balance(
  p_user_id   uuid,
  p_amount    numeric,
  p_reference text default ''
) returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_balance numeric;
begin
  if p_amount <= 0 then
    return jsonb_build_object('ok', false, 'reason', 'invalid_amount');
  end if;

  update public.profiles
     set balance_mt = balance_mt + p_amount,
         updated_at = now()
   where id = p_user_id
  returning balance_mt into v_balance;

  if not found then
    return jsonb_build_object('ok', false, 'reason', 'no_profile');
  end if;

  return jsonb_build_object('ok', true, 'balance', v_balance, 'reference', p_reference);
end;
$$;

create or replace function public.debit_balance(p_user_id uuid, p_amount numeric)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_balance numeric;
begin
  if p_amount <= 0 then
    return jsonb_build_object('ok', false, 'reason', 'invalid_amount');
  end if;

  select balance_mt into v_balance from public.profiles where id = p_user_id for update;
  if not found then
    return jsonb_build_object('ok', false, 'reason', 'no_profile');
  end if;
  if v_balance < p_amount then
    return jsonb_build_object('ok', false, 'reason', 'insufficient_funds', 'balance', v_balance);
  end if;

  v_balance := v_balance - p_amount;
  update public.profiles set balance_mt = v_balance, updated_at = now() where id = p_user_id;

  return jsonb_build_object('ok', true, 'balance', v_balance);
end;
$$;

-- Cria um pedido de levantamento debitando o saldo na mesma transacao.
create or replace function public.request_payout(
  p_user_id uuid,
  p_amount  numeric,
  p_method  text,
  p_phone   text,
  p_holder  text default ''
) returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_balance numeric;
  v_id      text;
begin
  if p_amount <= 0 then
    return jsonb_build_object('ok', false, 'reason', 'invalid_amount');
  end if;

  select balance_mt into v_balance from public.profiles where id = p_user_id for update;
  if not found then
    return jsonb_build_object('ok', false, 'reason', 'no_profile');
  end if;
  if v_balance < p_amount then
    return jsonb_build_object('ok', false, 'reason', 'insufficient_funds', 'balance', v_balance);
  end if;

  update public.profiles set balance_mt = balance_mt - p_amount, updated_at = now()
   where id = p_user_id;

  v_id := encode(gen_random_bytes(16), 'hex');
  insert into public.payouts (id, user_id, amount_mt, method, phone, holder_name, status)
  values (v_id, p_user_id, p_amount, p_method, p_phone, coalesce(p_holder, ''), 'pending');

  return jsonb_build_object('ok', true, 'payout_id', v_id, 'balance', v_balance - p_amount);
end;
$$;

-- Aprova (dinheiro ja debitado) ou rejeita (reembolsa o saldo).
create or replace function public.settle_payout(
  p_payout_id text,
  p_status    text,
  p_note      text default '',
  p_admin_id  uuid default null
) returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_user   uuid;
  v_amount numeric;
  v_status text;
begin
  select user_id, amount_mt, status into v_user, v_amount, v_status
    from public.payouts where id = p_payout_id for update;
  if not found then
    return jsonb_build_object('ok', false, 'reason', 'not_found');
  end if;
  if v_status <> 'pending' then
    return jsonb_build_object('ok', false, 'reason', 'already_decided');
  end if;
  if p_status not in ('approved', 'rejected') then
    return jsonb_build_object('ok', false, 'reason', 'bad_status');
  end if;

  update public.payouts
     set status = p_status,
         admin_note = coalesce(p_note, ''),
         decided_by = p_admin_id,
         decided_at = now()
   where id = p_payout_id;

  if p_status = 'rejected' then
    update public.profiles set balance_mt = balance_mt + v_amount, updated_at = now()
     where id = v_user;
  end if;

  return jsonb_build_object('ok', true, 'status', p_status);
end;
$$;

-- ============================================================
-- Row Level Security
-- O backend usa a SUPABASE_SECRET_KEY (ignora RLS). As politicas abaixo
-- protegem o acesso directo com a chave publica sb_publishable_.
-- ============================================================
alter table public.profiles   enable row level security;
alter table public.payments   enable row level security;
alter table public.payouts    enable row level security;
alter table public.login_logs enable row level security;
alter table public.settings   enable row level security;

drop policy if exists "profiles select own" on public.profiles;
create policy "profiles select own" on public.profiles
  for select using (auth.uid() = id);

drop policy if exists "profiles update own" on public.profiles;
create policy "profiles update own" on public.profiles
  for update using (auth.uid() = id)
  with check (auth.uid() = id and role = 'user' and balance_mt = (select balance_mt from public.profiles where id = auth.uid()));

drop policy if exists "payments select own" on public.payments;
create policy "payments select own" on public.payments
  for select using (auth.uid() = user_id);

drop policy if exists "payouts select own" on public.payouts;
create policy "payouts select own" on public.payouts
  for select using (auth.uid() = user_id);

drop policy if exists "settings read" on public.settings;
create policy "settings read" on public.settings for select using (true);

-- login_logs: sem politica de leitura -> so o backend (chave secreta) le.

-- ============================================================
-- Primeiro administrador
-- Depois de criares a tua conta na app, corre (substitui o email):
--   update public.profiles set role = 'admin' where email = 'tu@exemplo.com';
-- Ou usa: python scripts/make_admin.py tu@exemplo.com
-- ============================================================
