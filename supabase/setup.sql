-- Astaroth Terminal: database setup
-- Paste this whole file into Supabase → SQL Editor → New query, then press Run.
-- It is safe to run again: it only creates what is missing.

-- ---------------------------------------------------------------
-- 1. Stocks: one row per listing. Filled every night by the GitHub job.
--    "stocks" now holds every asset class — stocks, ETFs, futures,
--    REITs, indices, crypto and FX — one row per instrument. The
--    asset_class column tells the website which kind each one is.
-- ---------------------------------------------------------------
create table if not exists public.stocks (
  symbol                  text primary key,   -- AAPL, NESN.SW, ES=F, ^SSMI, BTC-USD, EURUSD=X
  name                    text,
  exchange                text,               -- NYSE, NASDAQ, SIX, CME, EUREX, CoinGecko, ECB
  region                  text not null,      -- 'US', 'Switzerland', 'Europe', 'Asia', 'Global'
  asset_class             text,               -- stock | etf | future | reit | index | crypto | fx
  currency                text,               -- trading currency of the price
  sector                  text,               -- one of the screener's sectors
  industry                text,
  summary                 text,               -- short descriptive blurb for the detail pane
  price                   numeric,
  change_pct              numeric,            -- today's change, in %
  market_cap_usd_b        numeric,            -- market cap in billions of USD (AUM for ETFs)
  pe                      numeric,            -- trailing P/E (null when loss-making)
  pb                      numeric,            -- price / book
  ffo_ps                  numeric,            -- Funds From Operations per share (REITs)
  affo_ps                 numeric,            -- Adjusted FFO per share (REITs)
  dividend_yield          numeric,            -- in % (distribution yield for ETFs)
  return_1y               numeric,            -- 52-week price return, in %
  beta                    numeric,
  revenue_growth          numeric,            -- year over year, in %
  eps_growth              numeric,            -- EPS growth used for PEG, in %
  net_margin              numeric,            -- in %
  expense_ratio           numeric,            -- in % (ETFs)
  high_52w                numeric,
  low_52w                 numeric,
  quote_updated_at        timestamptz,
  fundamentals_updated_at timestamptz
);

-- Older installs: add the new columns without touching existing data.
alter table public.stocks add column if not exists asset_class    text;
alter table public.stocks add column if not exists summary        text;
alter table public.stocks add column if not exists pb             numeric;
alter table public.stocks add column if not exists ffo_ps         numeric;
alter table public.stocks add column if not exists affo_ps        numeric;
alter table public.stocks add column if not exists eps_growth     numeric;
alter table public.stocks add column if not exists expense_ratio  numeric;

-- Keep asset_class in a known set. Dropped and recreated so re-running this file
-- always widens the allowed values, even on an older install.
alter table public.stocks drop constraint if exists stocks_asset_class_check;
alter table public.stocks
  add constraint stocks_asset_class_check
  check (asset_class is null
         or asset_class in ('stock','etf','future','reit','index','crypto','fx'));

create index if not exists stocks_region_idx on public.stocks (region);
create index if not exists stocks_asset_idx on public.stocks (asset_class);
create index if not exists stocks_fund_updated_idx on public.stocks (fundamentals_updated_at nulls first);

-- Only signed-in users can read stocks. Nobody can write from the website;
-- the nightly job writes with the service_role key, which bypasses these rules.
alter table public.stocks enable row level security;
drop policy if exists "Signed-in users can read stocks" on public.stocks;
create policy "Signed-in users can read stocks"
  on public.stocks for select
  to authenticated
  using (true);

-- ---------------------------------------------------------------
-- 2. Profiles: the username exactly as typed (case-sensitive).
-- ---------------------------------------------------------------
create table if not exists public.profiles (
  id         uuid primary key references auth.users (id) on delete cascade,
  username   text not null unique,              -- Postgres text is case-sensitive: 'Batman' <> 'batman'
  created_at timestamptz not null default now(),
  constraint username_format check (username ~ '^[A-Za-z0-9._]{3,20}$')
);

alter table public.profiles enable row level security;
drop policy if exists "Users can read their own profile" on public.profiles;
create policy "Users can read their own profile"
  on public.profiles for select
  to authenticated
  using (auth.uid() = id);

create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  insert into public.profiles (id, username)
  values (new.id, new.raw_user_meta_data ->> 'username');
  return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function public.handle_new_user();
