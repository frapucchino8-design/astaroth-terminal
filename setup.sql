-- Astaroth Terminal: database setup
-- Paste this whole file into Supabase → SQL Editor → New query, then press Run.
-- It is safe to run again: it only creates what is missing.

-- ---------------------------------------------------------------
-- 1. Stocks: one row per listing. Filled every night by the GitHub job.
-- ---------------------------------------------------------------
create table if not exists public.stocks (
  symbol                  text primary key,   -- AAPL, BRK.B, NESN.SW
  name                    text,
  exchange                text,               -- NYSE, NASDAQ, SIX ...
  region                  text not null,      -- 'US' or 'Switzerland'
  currency                text,               -- trading currency of the price
  sector                  text,               -- one of the screener's sectors
  industry                text,
  price                   numeric,
  change_pct              numeric,            -- today's change, in %
  market_cap_usd_b        numeric,            -- market cap in billions of USD
  pe                      numeric,            -- trailing P/E (null when loss-making)
  dividend_yield          numeric,            -- in %
  return_1y               numeric,            -- 52-week price return, in %
  beta                    numeric,
  revenue_growth          numeric,            -- year over year, in %
  net_margin              numeric,            -- in %
  high_52w                numeric,
  low_52w                 numeric,
  quote_updated_at        timestamptz,
  fundamentals_updated_at timestamptz
);

create index if not exists stocks_region_idx on public.stocks (region);
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

-- Create the profile automatically when someone signs up.
-- The website sends the username in the sign-up metadata.
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
