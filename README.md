# Game Finder

Where to watch, odds, standings, live injury reports and news for NFL, NBA, MLB,
Korea MNT, the Premier League and the Champions League. Data comes from ESPN's
public endpoints.

## On your phone

- **App:** open the GitHub Pages address in Safari, tap Share → **Add to Home Screen**.
- **Alerts:** install **ntfy** from the App Store, tap **+**, and subscribe to your
  topic (the same value saved as the `NTFY_TOPIC` repository secret).
  `.github/workflows/injury-alerts.yml` checks injury reports every ~5 minutes and
  pushes changes for the teams and players in `watchlist.json`.
- **Change who's watched:** edit `watchlist.json` on GitHub (teams under `favs`,
  full player names in `roster`).

## On your Mac

- `python3 server.py` → http://localhost:8790. Includes the Fantasy AI tab, which runs
  on your Claude subscription through the Claude CLI and is never exposed online.
- `bash install-alerts.sh` turns on Mac notifications every minute; `bash uninstall-alerts.sh` removes them.

Note: GitHub pauses scheduled workflows in a public repo after 60 days with no
commits. Any commit (or re-enabling it on the Actions tab) restarts it.
