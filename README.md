# WON2 Server Browser

Queries the community WON2 (and NeuWON) GoldSrc master servers for their
Half-Life 1 server lists, then pings every server it gets back with a real
`A2S_INFO` request and only shows the ones that actually answer - so you get
a *working* server list instead of the stale/offline entries master servers
tend to accumulate.

## Files

- `master_query.py` - the master-server query protocol + per-server liveness
  check (A2S_INFO). This is the part worth reading/reusing elsewhere.
- `app.py` - small Flask app: caches results for `CACHE_SECONDS` (default 90s)
  so a page load doesn't hammer the masters, exposes `/` (HTML) and
  `/api/servers` (JSON).
- `templates/index.html` - the page.
- `requirements.txt`, `render.yaml` - deploy config.

## Test locally first

I wrote this against the published Master Server Query Protocol and the
addresses currently listed for WON2 / NeuWON's `woncomm.lst` replacement, but
I have no outbound network access in my own sandbox, so none of this has
actually been run against the live masters. Before you deploy, run it
locally where you have a normal internet connection:

```bash
pip install -r requirements.txt
python app.py
# open http://localhost:5000
```

If the table stays empty, click "Force refresh" once (first load always
queries live), then check the obvious suspects:

- the WON2/NeuWON master hostnames in `MASTER_SERVERS` at the top of
  `master_query.py` may have moved or gone down - that community network is
  small and not always up. Swap in whatever current addresses you're using
  in your own `woncomm.lst`.
- a strict firewall/NAT blocking outbound UDP on port 27010 or the
  server ports would also show up as an empty list.

## Deploy to Render

**Option A - Blueprint (easiest):** push this folder to a GitHub repo, then
in Render: New -> Blueprint -> pick the repo. `render.yaml` sets everything
up (build command, start command, free plan).

**Option B - manual Web Service:** New -> Web Service -> pick the repo ->
- Build command: `pip install -r requirements.txt`
- Start command: `gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 120`

One thing to watch: Render's free web services are just containers, so
outbound UDP *should* work the same as any outbound connection, but it
isn't something Render documents or guarantees for the free tier. If the
deployed version shows 0 servers while your local run finds some, that's
the most likely explanation - worth testing once right after deploy.

## Customizing

- `MASTER_SERVERS` in `master_query.py` - add/remove master server
  host:port pairs.
- Filter by mod in the UI/API with `?gamedir=cstrike` (maps to the master
  protocol's `\gamedir\cstrike` filter), or add other filter fields
  (`\map\`, `\empty\1`, etc. - see Valve's Master Server Query Protocol
  docs) by extending `_filter_from_gamedir` in `app.py`.
- `CACHE_SECONDS` env var - how often results actually refresh.
