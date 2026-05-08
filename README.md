# idos

Search Czech multi-modal transport on [idos.cz](https://idos.cz) from the
command line. One tool covers all transport modes — CZ and international
trains, intercity buses (FlixBus, RegioJet), and city public transit
(tram, metro, trolley, ferry).

```
$ idos Brno Ostrava 2026-05-08 --time 09:00 --types train --limit 4
```

```
Brno hl.n. → Ostrava hl.n.  ·  08.05.2026 09:00  ·  lang=cs

  price       dep     arr     duration       transfers   legs
 ───────────────────────────────────────────────────────────────────────
  219 Kč      9:13    11:19   2 hod 6 min             0   Ex3
  219 Kč      10:13   12:19   2 hod 6 min             0   Ex3
  179 Kč      11:13   13:19   2 hod 6 min             0   Ex3
  219 Kč      12:13   14:19   2 hod 6 min             0   Ex3

· 4 results · click legs for share URL · --json for machine-readable · Search ↗
```

## Install

Not on PyPI — install from GitHub:

```
uv tool install 'idos @ git+https://github.com/FilipDusek/idos.git@main'
# or one-shot, no install:
uvx --from 'git+https://github.com/FilipDusek/idos.git' idos Brno Ostrava 2026-05-08
```

## CLI

```
idos FROM TO [DATE] [options]

Positional:
  FROM         Origin station/town (e.g. "Brno hl.n.")
  TO           Destination station/town (e.g. "Ostrava hl.n.")
  DATE         YYYY-MM-DD or D.M.YYYY (defaults to today)

Options:
  -t, --time HH:MM       Departure time (default: now)
  --arrival              Treat --time as desired arrival, not departure
  --direct               Only direct connections
  --types train,bus      Filter to specific transport groups
                         (train | bus | mhd | ferry | other)
  --shield all|pid|odis|plzen   Timetable database (default: all)
  --lang cs|en|de        Site language (default: cs)
  -n, --limit 10         Max results
  --json                 JSON output
  --no-rate-limit        Disable the local SQLite rate limiter
```

## Programmatic use

```python
from idos import search_connections

rows = search_connections(
    "Brno hl.n.", "Ostrava hl.n.", "2026-05-08",
    time="09:00", types=["train"], limit=5,
)
for r in rows:
    print(f"{r.departure} → {r.arrival}  ({r.duration}, {r.transfers} xfer)  {r.price_label}")
    for leg in r.legs:
        print(f"  · {leg.name} ({leg.category}) by {leg.carrier}")
        print(f"    {leg.from_station} {leg.dep_time} → {leg.to_station} {leg.arr_time}")
```

## Output shape

The CLI's `--json` output and the `Connection` dataclass returned by
`search_connections()` carry these fields per result:

- `from`, `to` — resolved station names
- `departure`, `arrival` — wall-clock times as shown on idos.cz
- `duration` — free-form server text (`"2 hod 6 min"`)
- `transfers` — number of vehicle changes
- `price` — e.g. `"219 Kč"`, or `"n/a"` when the carrier doesn't sell via IDOS
- `share_url` — canonical idos.cz share link for this connection
- `legs[]` — one entry per ride with `name`, `number`, `category`,
  `type_id`, `type_group`, `carrier`, `from`, `to`, `dep_time`, `arr_time`,
  `detail_url`

## Rate limiting

Cross-invocation rate limit defaults to 5 req / 5s, 30 / min, 300 / hr.
State persists across CLI invocations and parallel processes via SQLite +
file-lock at `~/Library/Caches/idos/ratelimit.sqlite` (macOS) or
`$XDG_CACHE_HOME/idos/ratelimit.sqlite` (Linux).

The exact threshold of idos.cz is not published — these limits are
arbitrary. Disable with `--no-rate-limit`, `IDOS_NO_RATE_LIMIT=1`, or
`rate_limit=False`.

## Caveats

- **Prices for some carriers come back as `n/a`** — RegioJet trains, for
  instance, don't sell their tickets via IDOS. The connection still appears;
  click `share_url` to open the carrier's site.
- **Scrapes idos.cz HTML.** It will break when they redesign. Pin a commit
  if you depend on it.

## License

MIT
