name: Update crypto data

# Runs every day, out of US and European trading hours so it doesn't clash with
# the stock update. CoinGecko's free tier allows about 30 calls per minute
# without a key, and this job uses only a handful, so it's well inside the limit.
on:
  schedule:
    - cron: "17 03 * * *"       # 03:17 UTC daily
  workflow_dispatch:             # also runnable by hand from the Actions tab

concurrency:
  group: crypto-${{ github.ref }}
  cancel-in-progress: true

jobs:
  update:
    runs-on: ubuntu-latest
    timeout-minutes: 20
    steps:
      - uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip

      - name: Install dependencies
        run: pip install -r requirements.txt

      - name: Update crypto
        env:
          SUPABASE_URL: ${{ secrets.SUPABASE_URL }}
          SUPABASE_SERVICE_ROLE_KEY: ${{ secrets.SUPABASE_SERVICE_ROLE_KEY }}
        run: python update_crypto.py
