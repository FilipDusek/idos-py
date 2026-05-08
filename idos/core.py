"""HTTP + parser layer for idos.cz multi-modal connection search.

Flow:
  1. GET `/{shield}/spojeni/vysledky/?f=...&t=...&date=...&time=...` → HTML
     with embedded `var connResult = new Conn.ConnResult(params, null, {…});`
     plus rendered connection cards (one `<div id="connectionBox-N">` each).
  2. Parse the connResult JSON (handle, connData with train IDs / numbers /
     station IDs) and the rendered HTML cards in parallel — the JSON has
     stable identifiers, the HTML has human-readable category, carrier,
     station names, and the price.
  3. For pagination, POST to `/{shield}/Ajax/ConnPaging` with the last
     connID + handle. The response is a JSONP payload containing more
     `connectionBox-N` HTML chunks plus the matching connData entries.

There is no separate "resolve station" step: idos accepts free-text station
names in the `f` / `t` query params and resolves them server-side. The
resolved name is echoed back in `searchItem.oConn.oUserInput.oFrom.sName`.
"""
from __future__ import annotations

import datetime as _dt
import html as _html
import http.cookiejar
import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, Request, build_opener


BASE = "https://idos.cz"

# A "shield" is idos's term for a timetable database. The default
# `vlakyautobusymhdvse` covers everything (CZ trains + buses + MHD + intl);
# regional ones (e.g. `pid`, `odis`) only contain a subset. For travel
# planning the all-modes shield is virtually always what you want.
SHIELDS = {
    "all": "vlakyautobusymhdvse",
    "pid": "pid",        # PID — capital-area integrated transit
    "odis": "odis",      # Ostrava Integrated Transport
    "plzen": "plzen",    # Plzeň MHD
}

LANG_PREFIX = {"cs": "", "en": "/en", "de": "/de"}

# idos's transport-type IDs, mapped from the advanced-form `trTypeId[…]`
# checkboxes (verified live against idos.cz). Grouped for friendly --types
# filtering. Anything not listed falls into "other".
#
#   150 SC/ICE       151 EC/IC      152 R          153 Os/Sp      315 vlak
#   200 místní bus   201 dálkový    202 mezinár.   304 regionál.  308 noční
#   300 tramvaj      301 autobus    302 metro      303 lanovka    306 trolejbus
#   309 noční tram   307 loď                       155 loď
#
# 154/155 sit inside the train range — they're replacement buses/ferries
# operated as part of CD timetables, so we route them to bus/ferry groups
# rather than train.
TRANSPORT_GROUPS = {
    "train": {150, 151, 152, 153, 315},
    "bus":   {154, 200, 201, 202, 304, 308, 310, 318},
    "mhd":   {300, 301, 302, 303, 306, 309},
    "ferry": {155, 307},
    "other": set(),  # 156, 305, 311, 312, 314, 317, 319, 321 fall here
}


# ─────────────────── dataclasses ───────────────────


@dataclass(frozen=True)
class Leg:
    """One physical segment within a connection (one train/bus/tram ride)."""
    name: str             # human-readable line label, e.g. "Bus 854", "R 634 Rožmberk"
    number: str           # raw train number, e.g. "854", "634"
    category: str         # category from h3 title, e.g. "dálkový autobus", "railjet"
    type_id: int          # idos numeric transport type id (see TRANSPORT_GROUPS)
    type_group: str       # coarse group: train | bus | tram | metro | trolley | ferry | other
    carrier: str          # carrier name, e.g. "FlixBus CZ s.r.o.", "České dráhy, a.s."
    from_station: str     # full station name as rendered, e.g. "Brno,,ÚAN Zvonařka"
    to_station: str
    dep_time: str         # "HH:MM"
    arr_time: str
    detail_url: str       # full URL to leg-detail page on idos.cz


@dataclass(frozen=True)
class Connection:
    """One end-to-end journey, possibly with transfers."""
    conn_id: int          # idos's internal connection id; varies between page fetches
    from_station: str
    to_station: str
    departure: str        # "DD.MM. HH:MM" — idos only ships day+month+time, not year
    arrival: str
    duration: str         # free-form server text, e.g. "2 hod 36 min"
    transfers: int        # number of transfers (= len(legs) - 1)
    legs: list[Leg]
    price_label: str      # "n/a" | "278 Kč"
    share_url: str        # canonical share URL for this connection
    raw_train_data: list[dict[str, Any]] = field(default_factory=list)  # connData[].trains


# ─────────────────── helpers ───────────────────


# Date input parsing — accept both ISO and CZ formats. Hand-rolled to avoid
# dateparser/dateutil's cold-start cost and ISO-vs-DMY ambiguity.
def _parse_date(s: str) -> _dt.date:
    """Accepts YYYY-MM-DD (ISO) or D[D].M[M].YYYY (CZ). Returns a date."""
    s = s.strip()
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        y, mo, d = (int(g) for g in m.groups())
        return _dt.date(y, mo, d)
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", s)
    if m:
        d, mo, y = (int(g) for g in m.groups())
        return _dt.date(y, mo, d)
    raise ValueError(f"date must be YYYY-MM-DD or D.M.YYYY (got {s!r})")


def _to_idos_date(date: str) -> str:
    """YYYY-MM-DD or D.M.YYYY → DD.MM.YYYY (idos's expected wire format)."""
    return _parse_date(date).strftime("%d.%m.%Y")


def _parse_time(time_str: Optional[str]) -> str:
    """HH:MM (1- or 2-digit hour OK), or None for now. Returns zero-padded."""
    if time_str is None:
        return _dt.datetime.now().strftime("%H:%M")
    s = time_str.strip()
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", s)
    if not m:
        raise ValueError(f"time must be HH:MM (got {time_str!r})")
    hh, mm = int(m.group(1)), int(m.group(2))
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError(f"time out of 24h range (got {time_str!r})")
    return f"{hh:02d}:{mm:02d}"


def _cookie_opener():
    jar: http.cookiejar.CookieJar = http.cookiejar.CookieJar()
    return build_opener(HTTPCookieProcessor(jar), HTTPRedirectHandler())


def _fetch(opener, url: str, *, method: str = "GET", data: bytes | None = None,
           headers: Optional[dict[str, str]] = None, timeout: float = 60.0) -> str:
    req = Request(url, data=data, method=method, headers=headers or {})
    with opener.open(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "ignore")


def _parse_jsonp(text: str) -> Any:
    text = text.strip()
    m = re.match(r"^[^(]+\((.*)\);?\s*$", text, re.S)
    if not m:
        raise ValueError("not a JSONP response")
    return json.loads(m.group(1))


def _extract_conn_result(html: str) -> dict[str, Any]:
    """Pull the JSON object out of `var connResult = new Conn.ConnResult(params, null, {…});`."""
    marker = "var connResult = new Conn.ConnResult(params, null, "
    start = html.find(marker)
    if start == -1:
        raise ValueError("Could not find connResult in idos.cz response")
    sub = html[start + len(marker):]
    depth = 0
    in_str = False
    esc = False
    for i, ch in enumerate(sub):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(sub[: i + 1])
    raise ValueError("Could not parse connResult JSON (unbalanced braces)")


def _classify_type(type_id: int) -> str:
    for group, ids in TRANSPORT_GROUPS.items():
        if type_id in ids:
            return group
    return "other"


_BOX_HEAD_RE = re.compile(r'<div id="connectionBox-(\d+)"([^>]*)>')
_SHARE_RE = re.compile(r'data-share-url="([^"]*)"')


def _split_boxes(html: str) -> list[tuple[int, str, str]]:
    """Split rendered HTML into (connId, share_url, block_html) tuples."""
    matches = list(_BOX_HEAD_RE.finditer(html))
    out: list[tuple[int, str, str]] = []
    for i, m in enumerate(matches):
        conn_id = int(m.group(1))
        attrs = m.group(2)
        share_m = _SHARE_RE.search(attrs)
        share_url = share_m.group(1) if share_m else ""
        end = matches[i + 1].start() if i + 1 < len(matches) else len(html)
        # also stop at prev-next nav or end-of-main
        block = html[m.start():end]
        for marker in ('<div class="prev-next"', '</main>'):
            mp = block.find(marker)
            if mp != -1:
                block = block[:mp]
        out.append((conn_id, share_url, block))
    return out


# Per-leg parser. Each leg is wrapped in an `<a class="title">` anchor whose
# onclick calls `connResult.showTrainDetail(connId, IDX, …)`. Some multi-leg
# connections render multiple legs inside the same `outside-of-popup` div
# (with `<div class="walk walk--detail">` between them), so we split on the
# per-leg anchors directly rather than the popup div.
_LEG_ANCHOR_RE = re.compile(
    r'<a [^>]*onclick="[^"]*showTrainDetail\((\d+),\s*(\d+),[\s\S]*?class="title"',
)
_TITLE_RE = re.compile(r'<h3 title="([^"]*)"[^>]*><span>([^<]+)</span>')
_OWNER_RE = re.compile(r'<span class="owner"><span>([^<]*)</span>')
_DETAIL_HREF_RE = re.compile(r'<a href="(https://idos\.cz/[^"]*?spojeni/draha/[^"]*)"')
_STATION_LI_RE = re.compile(
    r'<li class="item[^"]*"[^>]*>\s*<p[^>]*class="reset time[^"]*"[^>]*>([^<]+)</p>'
    r'\s*<p class="station"><strong[^>]*class="name[^"]*">([^<]+)</strong>'
)
_PRICE_RE = re.compile(r'<span class="price-value">([^<]+)</span>')
_TOTAL_RE = re.compile(r'<p class="reset total">[^<]*<strong>([^<]+)</strong></p>')
_DATE_HEAD_RE = re.compile(
    r'<h2 class="reset date[^"]*">([^<]+)<span class="date-after">([^<]+)</span></h2>'
)


def _parse_leg(block: str, train_data: dict[str, Any]) -> Leg:
    """Parse one leg HTML block. `train_data` is the matching entry from
    connData[].trains for this leg index — it gives us the numeric type_id."""
    title_m = _TITLE_RE.search(block)
    category = _html.unescape(title_m.group(1)).split(" (")[0] if title_m else ""
    name = _html.unescape(title_m.group(2)).strip() if title_m else ""
    owner_m = _OWNER_RE.search(block)
    carrier = _html.unescape(owner_m.group(1)) if owner_m else ""
    detail_m = _DETAIL_HREF_RE.search(block)
    detail_url = _html.unescape(detail_m.group(1)) if detail_m else ""
    stations = [
        (t.strip(), _html.unescape(s).strip())
        for t, s in _STATION_LI_RE.findall(block)
    ]
    type_id = int(train_data.get("id") or 0)
    return Leg(
        name=name,
        number=str(train_data.get("train") or "").strip(),
        category=category,
        type_id=type_id,
        type_group=_classify_type(type_id),
        carrier=carrier,
        from_station=stations[0][1] if stations else "",
        to_station=stations[-1][1] if stations else "",
        dep_time=stations[0][0] if stations else str(train_data.get("timeFrom", "")),
        arr_time=stations[-1][0] if stations else str(train_data.get("timeTo", "")),
        detail_url=detail_url,
    )


def _parse_connection(
    conn_id: int,
    share_url: str,
    block: str,
    train_data_list: list[dict[str, Any]],
) -> Connection:
    # Each leg is one `<a class="title" onclick="…showTrainDetail(connId, idx,…)">`
    # anchor. Slice the block at every such anchor that matches our conn_id.
    leg_anchors = [
        (int(m.group(2)), m.start())
        for m in _LEG_ANCHOR_RE.finditer(block)
        if int(m.group(1)) == conn_id
    ]
    legs: list[Leg] = []
    if leg_anchors:
        leg_anchors.sort()
        for i, (idx, start) in enumerate(leg_anchors):
            end = leg_anchors[i + 1][1] if i + 1 < len(leg_anchors) else len(block)
            td = train_data_list[idx] if idx < len(train_data_list) else {}
            legs.append(_parse_leg(block[start:end], td))

    head = _DATE_HEAD_RE.search(block)
    dep_time = head.group(1).strip() if head else ""
    dep_after = head.group(2).strip() if head else ""  # e.g. "8.5. pá"
    departure = f"{dep_after.split()[0]} {dep_time}".strip() if dep_after else dep_time

    arrival = ""
    if legs:
        # idos doesn't render arrival date next to the time; use last leg arr_time
        arrival = legs[-1].arr_time

    duration = ""
    total_m = _TOTAL_RE.search(block)
    if total_m:
        duration = _html.unescape(total_m.group(1)).strip()

    price_label = "n/a"
    price_m = _PRICE_RE.search(block)
    if price_m:
        raw = re.sub(r"\s+", " ", _html.unescape(price_m.group(1)).strip())
        if raw:
            price_label = raw

    return Connection(
        conn_id=conn_id,
        from_station=legs[0].from_station if legs else "",
        to_station=legs[-1].to_station if legs else "",
        departure=departure,
        arrival=arrival,
        duration=duration,
        transfers=max(0, len(legs) - 1),
        legs=legs,
        price_label=price_label,
        share_url=share_url,
        raw_train_data=train_data_list,
    )


def _parse_page_html(html: str, conn_data: list[dict[str, Any]]) -> list[Connection]:
    """Parse rendered HTML connection cards, joining them with the structured
    connData entries (matched by conn_id)."""
    by_id = {int(c.get("connId") or 0): c.get("trains") or [] for c in conn_data}
    out: list[Connection] = []
    for conn_id, share_url, block in _split_boxes(html):
        out.append(_parse_connection(conn_id, share_url, block, by_id.get(conn_id, [])))
    return out


def _sellable_ids(conn_data: list[dict[str, Any]]) -> set[int]:
    return {int(c["connId"]) for c in conn_data if c.get("isSellable")}


def _fetch_prices(
    opener,
    base_path: str,
    referer: str,
    handle: int,
    conn_ids: list[int],
) -> dict[int, str]:
    """POST `/Ajax/GetPriceOffer` once per connection to fill in prices.

    The endpoint returns a JSONP-wrapped object with `connId` and `price`
    (e.g. "329 Kč") plus optional `errorMsg`. The cd.cz two-step pattern
    is similar: search returns placeholders, prices come from a follow-up
    POST. Failures are non-fatal; missing prices fall through to "n/a".
    """
    url = f"{BASE}{base_path}/Ajax/GetPriceOffer?callback=cb"
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": BASE,
        "Referer": referer,
    }
    out: dict[int, str] = {}
    for cid in conn_ids:
        body = urlencode({
            "handle": str(handle),
            "connId": str(cid),
            "handleThere": "0",
            "connIdThere": "0",
            "format": "json",
        }).encode()
        try:
            text = _fetch(opener, url, method="POST", data=body, headers=headers)
            payload = _parse_jsonp(text)
        except Exception:
            continue
        if payload.get("errorMsg"):
            continue
        price = payload.get("price")
        if price:
            out[cid] = re.sub(r"\s+", " ", str(price)).strip()
    return out


def _apply_prices(conns: list[Connection], prices: dict[int, str]) -> list[Connection]:
    """Replace `price_label` on connections that got a real price from
    GetPriceOffer; leave the rest alone."""
    out: list[Connection] = []
    for c in conns:
        if c.conn_id in prices:
            out.append(Connection(
                conn_id=c.conn_id, from_station=c.from_station, to_station=c.to_station,
                departure=c.departure, arrival=c.arrival, duration=c.duration,
                transfers=c.transfers, legs=c.legs,
                price_label=prices[c.conn_id], share_url=c.share_url,
                raw_train_data=c.raw_train_data,
            ))
        else:
            out.append(c)
    return out


# ─────────────────── high-level API ───────────────────


def build_search_url(
    from_station: str,
    to_station: str,
    date: Optional[str] = None,
    *,
    time: Optional[str] = None,
    is_arrival: bool = False,
    direct_only: bool = False,
    lang: str = "cs",
    shield: str = "all",
) -> str:
    """Return the canonical idos.cz results-page URL for a query.

    Same defaulting as `search_connections` (today / now / `all` shield).
    Useful for surfacing a clickable "open this on idos.cz" link.
    """
    if lang not in LANG_PREFIX:
        raise ValueError(f"lang must be one of: {', '.join(LANG_PREFIX)}")
    shield_slug = SHIELDS.get(shield, shield)
    display_date = _to_idos_date(date) if date else _dt.date.today().strftime("%d.%m.%Y")
    display_time = _parse_time(time)
    base_path = f"{LANG_PREFIX[lang]}/{shield_slug}"
    params = {
        "f": from_station, "fc": "0",
        "t": to_station, "tc": "0",
        "date": display_date, "time": display_time,
    }
    if is_arrival:
        params["arr"] = "true"
    if direct_only:
        params["direct"] = "true"
    return f"{BASE}{base_path}/spojeni/vysledky/?{urlencode(params)}"


def search_connections(
    from_station: str,
    to_station: str,
    date: Optional[str] = None,
    *,
    time: Optional[str] = None,
    is_arrival: bool = False,
    direct_only: bool = False,
    lang: str = "cs",
    shield: str = "all",
    limit: int = 10,
    types: Optional[list[str]] = None,
    max_pages: int = 6,
    fetch_prices: bool = True,
    rate_limit: bool = True,
    rate_limiter=None,
) -> list[Connection]:
    """Search idos.cz for connections.

    Args:
        from_station, to_station: free-text station/town names
            (e.g. "Brno", "Ostrava hl.n."). Server-side resolution.
        date: YYYY-MM-DD or DD.MM.YYYY. Defaults to today.
        time: HH:MM. Defaults to now.
        is_arrival: True = `time` is desired arrival time, not departure.
        direct_only: only direct connections (no transfers).
        lang: "cs" | "en" | "de".
        shield: which timetable database to use. Pass a key from `SHIELDS`
            (default "all" = vlakyautobusymhdvse covers everything),
            or pass a raw shield slug.
        limit: max connections to return.
        types: filter results to only include connections whose legs are
            entirely within these transport groups (train, bus, tram,
            metro, trolley, ferry, other). None = no filter.
        max_pages: hard cap on pagination requests.
        fetch_prices: fire follow-up GetPriceOffer POSTs for sellable
            connections (one per connection, like the UI does).
        rate_limit: throttle outgoing requests via the shared SQLite limiter.
    """
    if lang not in LANG_PREFIX:
        raise ValueError(f"lang must be one of: {', '.join(LANG_PREFIX)}")

    shield_slug = SHIELDS.get(shield, shield)
    if types is not None:
        unknown = [t for t in types if t not in TRANSPORT_GROUPS]
        if unknown:
            raise ValueError(
                f"unknown transport types: {unknown}. "
                f"Valid: {sorted(TRANSPORT_GROUPS)}"
            )

    display_date = _to_idos_date(date) if date else _dt.date.today().strftime("%d.%m.%Y")
    display_time = _parse_time(time)
    opener = _cookie_opener()

    if rate_limit:
        from .ratelimit import BUCKET_NAME, shared as _shared
        limiter = rate_limiter or _shared()
        if limiter is not None:
            limiter.try_acquire(BUCKET_NAME)

    base_path = f"{LANG_PREFIX[lang]}/{shield_slug}"
    search_url = f"{BASE}{base_path}/spojeni/vysledky/"
    params = {
        "f": from_station,
        "fc": "0",
        "t": to_station,
        "tc": "0",
        "date": display_date,
        "time": display_time,
    }
    if is_arrival:
        params["arr"] = "true"
    if direct_only:
        params["direct"] = "true"

    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "text/html,application/xhtml+xml",
    }
    full_url = f"{search_url}?{urlencode(params)}"
    html = _fetch(opener, full_url, headers=headers)
    try:
        cr = _extract_conn_result(html)
    except ValueError:
        # No connResult on the page = idos rendered the form instead of
        # results. Most common causes: unrecognized station name, or date
        # outside the loaded timetable validity window.
        raise LookupError(
            f"idos returned no results for {from_station!r} → {to_station!r} "
            f"on {display_date} {display_time} "
            f"(unrecognized station, or date outside the timetable window)"
        ) from None
    if cr.get("isMc"):
        raise LookupError(
            f"idos couldn't resolve station(s): from={from_station!r} to={to_station!r}"
        )

    connections = _parse_page_html(html, cr.get("connData") or [])
    sellable: set[int] = _sellable_ids(cr.get("connData") or [])

    handle = cr.get("handle")
    search_date = cr.get("searchItem", {}).get("dtTimeStamp") or ""
    arrival_there = cr.get("arrivalThere") or "0001-01-01T00:00:00"
    resolved_from = (cr.get("searchItem", {}).get("oConn", {})
                     .get("oUserInput", {}).get("oFrom", {}).get("sName") or from_station)
    resolved_to = (cr.get("searchItem", {}).get("oConn", {})
                   .get("oUserInput", {}).get("oTo", {}).get("sName") or to_station)

    # Pagination: POST `/Ajax/ConnPaging` with the last connId on the page.
    pages = 0
    while connections and len(_filter(connections, types)) < limit and pages < max_pages - 1:
        pages += 1
        if rate_limit:
            limiter = rate_limiter or _shared()
            if limiter is not None:
                limiter.try_acquire(BUCKET_NAME)

        listed_ids = [c.conn_id for c in connections]
        last_id = listed_ids[-1]
        body = [
            *[("listedIds[]", str(i)) for i in listed_ids],
            ("isPrev", "false"),
            ("handle", str(handle)),
            ("searchDate", search_date),
            ("connId", str(last_id)),
            ("arrivalThere", arrival_there),
            ("from", resolved_from),
            ("to", resolved_to),
        ]
        ajax_url = f"{BASE}{base_path}/Ajax/ConnPaging?callback=cb"
        ajax_headers = {
            "User-Agent": "Mozilla/5.0",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": BASE,
            "Referer": full_url,
        }
        try:
            resp_text = _fetch(
                opener, ajax_url, method="POST",
                data=urlencode(body).encode("utf-8"),
                headers=ajax_headers,
            )
            payload = _parse_jsonp(resp_text)
        except Exception:
            break
        new_html = "".join(payload.get("newConnections") or [])
        new_data = payload.get("connData") or []
        new_conns = _parse_page_html(new_html, new_data)
        if not new_conns:
            break
        connections.extend(new_conns)
        sellable.update(_sellable_ids(new_data))
        if not payload.get("allowNext"):
            break

    filtered = _filter(connections, types)[:limit]

    if fetch_prices and filtered:
        sellable_to_fetch = [c.conn_id for c in filtered if c.conn_id in sellable]
        if sellable_to_fetch and handle:
            prices = _fetch_prices(opener, base_path, full_url, int(handle),
                                   sellable_to_fetch)
            filtered = _apply_prices(filtered, prices)

    return filtered


def _filter(conns: list[Connection], types: Optional[list[str]]) -> list[Connection]:
    if not types:
        return conns
    keep = set(types)
    return [c for c in conns if c.legs and all(leg.type_group in keep for leg in c.legs)]
