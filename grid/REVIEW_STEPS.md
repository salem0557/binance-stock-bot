# Daily review steps (run by the scheduled routine)

Paper trading only. Never touch live mode, API keys, Railway variables, or files outside `grid/`.

1. Setup (fresh container):
   ```
   pip install -q ccxt
   cat /root/.ccr/ca-bundle.crt >> "$(python3 -c 'import certifi;print(certifi.where())')"
   ```
   (api.binance.com is geo-blocked here; the public mirror below works.)
2. Run the review:
   ```
   cd grid && BINANCE_PUBLIC_URL=https://data-api.binance.vision/api/v3 python3 daily_review.py
   ```
3. If `grid/params.json` or `grid/journal.md` changed, commit only those two files on `main`
   with message `Daily review <date>` and `git push -u origin main`. Railway redeploys and the bot
   switches to the new settings automatically.
4. Reply to the user in simple Arabic: yesterday's result, the mistake found, what changed (or
   that nothing changed), and a one-line reminder that it is virtual money.
