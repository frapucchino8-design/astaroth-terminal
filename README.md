# Astaroth Terminal: setup

Astaroth Terminal shows every US-listed stock (about 6,000) and every Swiss share on SIX.

- A GitHub job runs each weekday evening. It fetches the data and stores it in Supabase.
- The website reads that table and handles sign-in with a username and password.

## Where each key goes

| Key | Where you get it | Where it goes |
|---|---|---|
| Finnhub API key | finnhub.io → Dashboard | GitHub secret `FINNHUB_API_KEY` |
| Supabase Project URL | Supabase → Project Settings → Data API | GitHub secret `SUPABASE_URL` **and** `index.html` (`SUPABASE_URL`) |
| Supabase **anon** key (or **Publishable** key) | Supabase → Project Settings → API Keys | `index.html` (`SUPABASE_ANON_KEY`) |
| Supabase **service_role** key (or **Secret** key) | Supabase → Project Settings → API Keys | GitHub secret `SUPABASE_SERVICE_ROLE_KEY`, **nowhere else** |

The anon/publishable key is meant to be public, because the database only lets signed-in users read stock data. The service_role/secret key can change anything in your database. Never put it in the HTML, and never paste it into a chat.

## Step 1: Finnhub

1. Sign up for free at finnhub.io.
2. Copy the API key from your dashboard.

## Step 2: Supabase

1. Create a new project.
2. Open **SQL Editor → New query**, paste all of `supabase/setup.sql`, and press **Run**. This creates the `stocks` and `profiles` tables and their access rules.
3. Go to **Authentication → Sign In / Providers → Email** and switch **Confirm email** off. Users only have a username, so there is no inbox to confirm.
4. Go to **Project Settings → API Keys** and copy the keys from the table above. If you see a **Legacy API Keys** tab, use the anon and service_role keys from there. If you only see Publishable and Secret keys, use those instead. Both kinds work.

## Step 3: GitHub

1. Create a new repository. Make it **public**: GitHub Actions minutes are unlimited for public repos. A private repo only gets 2,000 free minutes a month, and this job needs about 5 hours per weekday. Your keys stay private either way, because they live in secrets, not in the code.
2. Upload every file from this folder and keep the folder structure. Check that `.github/workflows/update-stocks.yml` is there. On a Mac, folders starting with a dot are hidden; press Cmd+Shift+. in Finder to show them.
3. Go to **Settings → Secrets and variables → Actions → New repository secret** and add three secrets:
   - `FINNHUB_API_KEY`
   - `SUPABASE_URL`
   - `SUPABASE_SERVICE_ROLE_KEY`

## Step 4: First data run

1. Open the **Actions** tab, then **Update stock data → Run workflow → both**.
2. The runs take this long:
   - Switzerland: about 5 minutes.
   - US prices: about 2 hours.
   - US fundamentals (P/E, margins, sector): they fill in during the next ~3 hours.
3. You can open the site while it runs. Refresh the page to see more stocks.
4. After this, it runs by itself every weekday at 22:17 UTC, which is 23:17 in Zurich in winter.

## Step 5: The website

1. Open `index.html` in a text editor. Near the top of the `<script>` section, paste in your keys:
   ```js
   const SUPABASE_URL = "https://YOUR-PROJECT.supabase.co";
   const SUPABASE_ANON_KEY = "your anon or publishable key";
   ```
2. Put it online the same way as BatMessage. For example, drag the folder onto Netlify.
3. Open the site, create an account and sign in.

If both values are left empty, the page runs in demo mode with 51 sample stocks.

## If something goes wrong

- **Sign-up says the email is invalid.** Change `LOGIN_EMAIL_DOMAIN` in `index.html` to a domain you own, for example `users.batmessage.cloud`. No emails are ever sent.
- **"The stocks table doesn't exist yet."** Run `supabase/setup.sql` again.
- **"No stock data yet."** Run the workflow from the Actions tab.
- **A run failed.** Open it in the Actions tab. The log says what went wrong; a wrong or missing secret is the usual cause.
- **Scheduled runs stopped.** GitHub pauses scheduled jobs in public repos after 60 days without a commit. Re-enable it in the Actions tab.

## Data sources

- **US:** the Finnhub free plan, an official API. It allows about 60 calls a minute.
- **Switzerland:** Yahoo Finance through the `yfinance` library. It's free but unofficial: fine for a personal project, but Yahoo's terms don't allow commercial use. Switch to a paid source before charging for the site.
- **Delays:** prices are end of day, not live.
