name: Update preferred shares

# Preferred shares don't need daily updates — their price barely moves and the
# figures that matter (coupon, par) never change. Once a week on a Saturday
# morning is plenty.
on:
  schedule:
    - cron: "33 06 * * 6"       # Saturday 06:33 UTC
  workflow_dispatch:

concurrency:
  group: preferred-${{ github.ref }}
  cancel-in-progress: true

jobs:
  update:
    runs-on: ubuntu-latest
    # ~110 tickers at ~1s each + yfinance overhead; cap it so a stall can't
    # burn an hour of runner time.
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip

      - name: Install dependencies
        run: pip install -r requirements.txt

      - name: Update preferred shares
        env:
          SUPABASE_URL: ${{ secrets.SUPABASE_URL }}
          SUPABASE_SERVICE_ROLE_KEY: ${{ secrets.SUPABASE_SERVICE_ROLE_KEY }}
        run: python update_preferred.py
