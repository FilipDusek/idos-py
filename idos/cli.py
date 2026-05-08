"""`idos` CLI: typer wrapper around `core.search_connections`."""
from __future__ import annotations

import json as _json
import sys
from typing import Optional

try:
    import typer
except ImportError:
    sys.stderr.write(
        "idos CLI requires typer. Install with:\n"
        "  uv tool install 'idos @ git+https://github.com/FilipDusek/idos.git@main'\n"
    )
    raise SystemExit(1)

from .core import Connection, SHIELDS, TRANSPORT_GROUPS, build_search_url, search_connections


def _render(c: Connection) -> dict:
    return {
        "conn_id": c.conn_id,
        "from": c.from_station,
        "to": c.to_station,
        "departure": c.departure,
        "arrival": c.arrival,
        "duration": c.duration,
        "transfers": c.transfers,
        "price": c.price_label,
        "share_url": c.share_url,
        "legs": [
            {
                "name": leg.name,
                "number": leg.number,
                "category": leg.category,
                "type_id": leg.type_id,
                "type_group": leg.type_group,
                "carrier": leg.carrier,
                "from": leg.from_station,
                "to": leg.to_station,
                "dep_time": leg.dep_time,
                "arr_time": leg.arr_time,
                "detail_url": leg.detail_url,
            }
            for leg in c.legs
        ],
    }


def _print_table(rows: list[dict], *, from_label: str, to_label: str,
                 date: str, time: str, lang: str, search_url: str) -> None:
    try:
        from rich import box
        from rich.console import Console
        from rich.table import Table
    except ImportError:
        # Plain fallback when rich isn't installed
        print(f"\n{from_label} → {to_label}  ·  {date} {time}  ·  lang={lang}\n")
        for i, r in enumerate(rows, 1):
            print(f"[{i}] {r['departure']} → {r['arrival']}  {r['duration']}  "
                  f"{r['transfers']} transfers  ·  {r['price']}")
            print(f"    legs: {', '.join(leg['name'] for leg in r['legs'])}")
            if r["share_url"]:
                print(f"    details: {r['share_url']}")
            print()
        print(f"Search: {search_url}\n")
        return

    console = Console()
    console.print(
        f"\n[bold]{from_label} → {to_label}[/bold]  [dim]·[/dim]  "
        f"{date} {time}  [dim]·[/dim]  [dim]lang={lang}[/dim]\n"
    )
    if not rows:
        console.print("[dim]no connections returned[/dim]\n")
        console.print(f"[dim][link={search_url}]Search ↗[/link][/dim]\n")
        return

    table = Table(box=box.SIMPLE_HEAVY, header_style="bold", padding=(0, 1), expand=False)
    table.add_column("price", justify="right", style="bold green", no_wrap=True)
    table.add_column("dep", no_wrap=True)
    table.add_column("arr", no_wrap=True)
    table.add_column("duration", no_wrap=True)
    table.add_column("transfers", justify="right", no_wrap=True)
    table.add_column("legs", overflow="ellipsis")

    for r in rows:
        # extract just the time portion of "DD.MM. HH:MM"
        dep_time = r["departure"].split(" ", 1)[-1] if r["departure"] else ""
        arr_time = r["arrival"]
        leg_names = " → ".join(leg["name"] for leg in r["legs"])
        if r["share_url"]:
            leg_names = f"[link={r['share_url']}]{leg_names}[/link]"
        table.add_row(
            r["price"], dep_time, arr_time, r["duration"],
            str(r["transfers"]), leg_names,
        )

    console.print(table)
    console.print(
        f"[dim]· {len(rows)} result{'s' if len(rows) != 1 else ''} · "
        f"click legs for share URL · --json for machine-readable · "
        f"[link={search_url}]Search ↗[/link][/dim]\n"
    )


def main(
    from_station: str = typer.Argument(..., metavar="FROM",
        help="Origin station/town (e.g. 'Brno hl.n.')"),
    to_station: str = typer.Argument(..., metavar="TO",
        help="Destination station/town (e.g. 'Ostrava hl.n.')"),
    date: Optional[str] = typer.Argument(None, metavar="[DATE]",
        help="Travel date YYYY-MM-DD or D.M.YYYY (defaults to today)"),
    time: Optional[str] = typer.Option(None, "--time", "-t",
        help="Departure time HH:MM (defaults to now)"),
    is_arrival: bool = typer.Option(False, "--arrival",
        help="Treat --time as desired arrival time, not departure"),
    direct: bool = typer.Option(False, "--direct",
        help="Only direct connections (no transfers)"),
    types: Optional[str] = typer.Option(None, "--types",
        help=f"Comma-separated transport groups to include "
             f"({','.join(sorted(TRANSPORT_GROUPS))}). Default: any."),
    shield: str = typer.Option("all", "--shield",
        help=f"Timetable database. Friendly: {','.join(SHIELDS)}. "
             "Or pass a raw idos shield slug."),
    lang: str = typer.Option("cs", "--lang",
        help="Site language: cs | en | de"),
    limit: int = typer.Option(10, "--limit", "-n", min=1, max=50,
        help="Max connections to show"),
    json_output: bool = typer.Option(False, "--json",
        help="Emit JSON instead of a table"),
    no_rate_limit: bool = typer.Option(False, "--no-rate-limit",
        help="Disable the SQLite-backed rate limiter"),
) -> None:
    """Search Czech multi-modal transport (idos.cz: trains + buses + MHD)."""
    types_list = [t.strip() for t in types.split(",")] if types else None

    try:
        rows = search_connections(
            from_station, to_station, date,
            time=time, is_arrival=is_arrival, direct_only=direct,
            lang=lang, shield=shield, limit=limit, types=types_list,
            rate_limit=not no_rate_limit,
        )
    except (ValueError, LookupError) as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(2)
    except Exception as e:  # network or parse failure
        typer.echo(f"search failed: {type(e).__name__}: {e}", err=True)
        raise typer.Exit(1)

    rendered = [_render(r) for r in rows]
    search_url = build_search_url(
        from_station, to_station, date,
        time=time, is_arrival=is_arrival, direct_only=direct,
        lang=lang, shield=shield,
    )

    if json_output:
        from .core import _to_idos_date, _parse_time
        date_display = _to_idos_date(date) if date else ""
        typer.echo(_json.dumps(
            {
                "query": {
                    "from": from_station, "to": to_station,
                    "date": date_display,
                    "time": _parse_time(time),
                    "shield": shield, "lang": lang,
                    "types": types_list,
                    "url": search_url,
                },
                "results": rendered,
            },
            indent=2, ensure_ascii=False, default=str,
        ))
    else:
        from .core import _to_idos_date, _parse_time
        from_label = rows[0].from_station if rows else from_station
        to_label = rows[0].to_station if rows else to_station
        date_display = _to_idos_date(date) if date else "today"
        _print_table(
            rendered, from_label=from_label, to_label=to_label,
            date=date_display, time=_parse_time(time), lang=lang,
            search_url=search_url,
        )


def _entrypoint() -> None:
    typer.run(main)


if __name__ == "__main__":
    _entrypoint()
