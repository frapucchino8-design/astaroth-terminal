name: Update ETFs and futures

on:
  schedule:
    # Every weeknight at 21:42 UTC, before the big US stock job.
    - cron: "42 21 * * 1-5"
  workflow_dispatch:

concurrency:
  group: funds
  cancel-in-progress: false

jobs:
  funds:
    runs-on: ubuntu-latest
    timeout-minutes: 120
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip
          cache-dependency-path: scripts/requirements.txt
      - run: pip install -r scripts/requirements.txt
      - name: Update ETFs and futures
        run: python scripts/update_funds.py --budget-minutes 90
        env:
          FINNHUB_API_KEY: ${{ secrets.FINNHUB_API_KEY }}
          SUPABASE_URL: ${{ secrets.SUPABASE_URL }}
          SUPABASE_SERVICE_ROLE_KEY: ${{ secrets.SUPABASE_SERVICE_ROLE_KEY }}
