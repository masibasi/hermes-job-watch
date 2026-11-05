#!/usr/bin/env python3
"""job-watch: new software-engineering postings from public job boards.

Reads the public, keyless job-board APIs of Greenhouse, Lever and Ashby. Code does the
deterministic work (fetch, title and location filters, de-duplication, first-seen dates); the
agent does the judgment (reading a job description and deciding fit).

    job_watch.py check [--days 7] [--max 15] [--location us|any] [--include REGEX] [--exclude REGEX] [--no-save]
    job_watch.py recent [--days 7] [--max 30]
    job_watch.py details URL [--max-chars 8000]
    job_watch.py boards
    job_watch.py add-board "Company" greenhouse|lever|ashby board-slug
    job_watch.py remove-board "Company"

Every command prints one JSON object. Standard library only (Python 3.9+).
State lives in $JOB_WATCH_HOME, else $HERMES_HOME/job-watch, else ~/.hermes/job-watch.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import ssl
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

VERSION = "1.0.3"
TIMEOUT = 20
NEW_WINDOW_DAYS = 7

DEFAULT_BOARDS = [
    {"company": "Anthropic", "source": "greenhouse", "slug": "anthropic"},
    {"company": "Stripe", "source": "greenhouse", "slug": "stripe"},
    {"company": "Roblox", "source": "greenhouse", "slug": "roblox"},
    {"company": "Datadog", "source": "greenhouse", "slug": "datadog"},
    {"company": "Cloudflare", "source": "greenhouse", "slug": "cloudflare"},
    {"company": "MongoDB", "source": "greenhouse", "slug": "mongodb"},
    {"company": "Palantir", "source": "lever", "slug": "palantir"},
    {"company": "OpenAI", "source": "ashby", "slug": "openai"},
    {"company": "Snowflake", "source": "ashby", "slug": "snowflake"},
]
SOURCES = ("greenhouse", "lever", "ashby")

ENGINEERING = re.compile(r"engineer|member of technical staff|\bdeveloper\b", re.I)
FOCUS = re.compile(
    r"software|back[- ]?end|full[- ]?stack|product|platform|developer (tools|experience|productivity)"
    r"|applied ai|\bai\b|agent|integration|forward deployed|member of technical staff|api", re.I)
EXCLUDE = re.compile(
    r"\b(senior|sr\.?|staff|principal|lead|manager|director|head of|vp|intern|internship|phd|architect"
    r"|distinguished|iii|iv)\b|hardware|mechanical|electrical|firmware|silicon|asic|robotics"
    r"|data ?center|site reliability|\bsre\b|network", re.I)
MTS = re.compile(r"member of technical staff", re.I)  # an IC title, not a staff-level one
US = re.compile(
    r"united states|\busa?\b|remote|san francisco|new york|seattle|los angeles|boston|austin|denver"
    r"|chicago|washington|palo alto|mountain view|menlo park|bellevue|san mateo|sunnyvale|san jose"
    r"|atlanta|miami|, (ca|ny|wa|tx|co|ma|il|dc|ga|fl|or|nc|va|pa|nj|ut|az)\b", re.I)
NON_US = re.compile(
    r"london|dublin|tokyo|seoul|singapore|toronto|vancouver|india|bangalore|bengaluru|berlin|paris"
    r"|amsterdam|sydney|zurich|munich|tel aviv|madrid|warsaw|são paulo|sao paulo|mexico|canada"
    r"|united kingdom|\buk\b|germany|japan|korea|australia|ireland|brazil|france|netherlands|israel"
    r"|poland|spain|switzerland|lisbon|portugal", re.I)


# ----------------------------------------------------------------------------- state

def home() -> Path:
    base = os.environ.get("JOB_WATCH_HOME")
    if base:
        path = Path(base)
    else:
        path = Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser() / "job-watch"
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_json(name: str, default):
    try:
        return json.loads((home() / name).read_text())
    except (FileNotFoundError, ValueError):
        return default


def write_json(name: str, data) -> None:
    """Atomic replace, so an interrupted run never leaves a half-written file."""
    target = home() / name
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{name}.")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, target)


def boards() -> list[dict]:
    return read_json("boards.json", None) or [dict(b) for b in DEFAULT_BOARDS]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def age_days(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        then = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).total_seconds() / 86400


# ----------------------------------------------------------------------------- filters

def title_matches(title: str, include: re.Pattern | None = None, exclude: re.Pattern | None = None) -> bool:
    focus = include or FOCUS
    if not ENGINEERING.search(title) or not focus.search(title):
        return False
    stripped = MTS.sub("", title)
    return not (EXCLUDE.search(stripped) or (exclude and exclude.search(stripped)))


def location_ok(location: str, country: str | None = None, mode: str = "us") -> bool:
    if mode == "any":
        return True
    if country:
        return country.upper() == "US"
    if US.search(re.sub(r"remote", "", location, flags=re.I)):
        return True
    return not NON_US.search(location) and "remote" in location.lower()


# ----------------------------------------------------------------------------- fetching

SYSTEM_CA_BUNDLES = ("/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt",
                     "/opt/homebrew/etc/ca-certificates/cert.pem")
_working_context = 0  # index of the context that last worked, so later requests skip the failing ones


def _contexts() -> list[ssl.SSLContext]:
    """Certificate-verifying contexts, in order: certifi's bundle, the interpreter's defaults, then the
    operating system's bundle (some macOS Pythons ship without any CA bundle of their own).
    Verification is never turned off."""
    found = []
    try:
        import certifi
        found.append(ssl.create_default_context(cafile=certifi.where()))
    except ImportError:
        pass
    found.append(ssl.create_default_context())
    for path in SYSTEM_CA_BUNDLES:
        if os.path.exists(path):
            found.append(ssl.create_default_context(cafile=path))
            break
    return found


def explain(error: Exception) -> str:
    text = f"{type(error).__name__}: {str(error)[:140]}"
    if "CERTIFICATE_VERIFY_FAILED" in str(error):
        text += (" (this Python has no CA certificates: on macOS run its 'Install Certificates.command', "
                 "or add the certifi package to it)")
    return text


def get_json(url: str):
    global _working_context
    request = Request(url, headers={"User-Agent": f"job-watch/{VERSION}", "Accept": "application/json"})
    contexts = _contexts()
    order = [_working_context] + [i for i in range(len(contexts)) if i != _working_context]
    failure = None
    for i in order:
        if i >= len(contexts):
            continue
        try:
            with urlopen(request, timeout=TIMEOUT, context=contexts[i]) as response:
                _working_context = i
                return json.load(response)
        except URLError as e:
            if isinstance(e.reason, ssl.SSLCertVerificationError):
                failure = e  # this CA bundle does not know the server; try the next one
                continue
            raise
    raise failure


def _iso_from_ms(ms) -> str | None:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat() if ms else None


def fetch_board(board: dict) -> list[dict]:
    """One board as normalized postings: id, title, location, url, posted, country (or None)."""
    source, slug, jobs = board["source"], board["slug"], []
    if source == "greenhouse":
        data = get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
        for j in data.get("jobs", []):
            jobs.append(dict(id=str(j["id"]), title=j["title"], location=(j.get("location") or {}).get("name", ""),
                             url=j["absolute_url"], posted=j.get("first_published") or j.get("updated_at"),
                             country=None))
    elif source == "lever":
        for j in get_json(f"https://api.lever.co/v0/postings/{slug}?mode=json"):
            cats = j.get("categories") or {}
            jobs.append(dict(id=j["id"], title=j["text"], location=cats.get("location", ""), url=j["hostedUrl"],
                             posted=_iso_from_ms(j.get("createdAt")), country=j.get("country")))
    elif source == "ashby":
        data = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
        for j in data.get("jobs", []):
            if j.get("isListed") is False:
                continue
            country = ((j.get("address") or {}).get("postalAddress") or {}).get("addressCountry")
            loc = " | ".join([j.get("location") or ""] + [s.get("location", "") for s in j.get("secondaryLocations") or []])
            jobs.append(dict(id=j["id"], title=j["title"], location=loc, url=j["jobUrl"], posted=j.get("publishedAt"),
                             country="US" if country in ("United States", "USA", "US") else None))
    else:
        raise ValueError(f"unknown source {source!r}")
    for j in jobs:
        j.update(company=board["company"], source=source, slug=slug, key=f"{source}:{slug}:{j['id']}")
    return jobs


# ----------------------------------------------------------------------------- commands

def board_key(board: dict) -> str:
    return f"{board['source']}:{board['slug']}"


def cmd_check(args) -> dict:
    include = re.compile(args.include, re.I) if args.include else None
    exclude = re.compile(args.exclude, re.I) if args.exclude else None
    state = read_json("state.json", {"jobs": {}})
    configured = boards()
    # Each board gets its own baseline, so a board that failed earlier is baselined when it first works
    # instead of flooding the next report with its old postings.
    if "initialized_boards" in state:
        initialized = set(state["initialized_boards"])
    else:  # state written before per-board baselines
        initialized = {board_key(b) for b in configured} if state.get("initialized_at") else set()

    errors, fetched = [], []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [(b, pool.submit(fetch_board, b)) for b in configured]
        for board, fut in futures:
            try:
                roles = [j for j in fut.result()
                         if title_matches(j["title"], include, exclude)
                         and location_ok(j["location"], j.get("country"), args.location)]
            except (URLError, HTTPError, ValueError, KeyError, TimeoutError) as e:
                errors.append(f"{board['company']}: {explain(e)}")
                continue
            fetched.append((board, roles))

    seen, new, total, baselined = state["jobs"], [], 0, []
    for board, roles in fetched:
        total += len(roles)
        has_baseline = board_key(board) in initialized
        for j in roles:
            if j["key"] in seen:
                continue
            record = {k: j[k] for k in ("company", "title", "location", "url", "posted", "source", "slug", "id")}
            record["first_seen"] = now_iso()  # written once, never refreshed
            # On a board's first successful check only recent postings count as news; the rest is a silent baseline.
            is_news = has_baseline or (age_days(j["posted"]) or 0) <= args.days
            if not is_news:
                record["baseline"] = True  # recorded so it is never reported later, but it is not "recent"
            seen[j["key"]] = record
            if is_news:
                new.append(record)
        if not has_baseline:
            initialized.add(board_key(board))
            baselined.append(board["company"])
    state["initialized_boards"] = sorted(initialized)
    state.pop("initialized_at", None)
    if not args.no_save and fetched:
        write_json("state.json", state)
    new.sort(key=lambda r: r.get("posted") or "", reverse=True)
    return {
        "boards_checked": len(fetched), "errors": errors, "first_run": bool(baselined),
        "baselined_boards": baselined, "open_matching_roles": total, "new_count": len(new),
        "new_roles": new[: args.max], "saved": bool(fetched) and not args.no_save,
    }


def cmd_recent(args) -> dict:
    rows = [j for j in read_json("state.json", {"jobs": {}})["jobs"].values()
            if not j.get("baseline") and (age_days(j.get("first_seen")) or 99) <= args.days]
    rows.sort(key=lambda j: j.get("posted") or "", reverse=True)
    return {"days": args.days, "count": len(rows), "roles": rows[: args.max]}


def plain(markup: str) -> str:
    text = re.sub(r"<(br|/p|/li|/h\d)[^>]*>", "\n", html.unescape(markup or ""), flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", text)).strip()


def cmd_details(args) -> dict:
    job = next((j for j in read_json("state.json", {"jobs": {}})["jobs"].values() if j["url"] == args.url), None)
    if job is None:
        return {"error": "unknown url: run `check` or `recent` first and pass a url exactly as returned"}
    source, slug, job_id = job["source"], job["slug"], job["id"]
    if source == "greenhouse":
        text = plain(get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job_id}").get("content", ""))
    elif source == "lever":
        data = get_json(f"https://api.lever.co/v0/postings/{slug}/{job_id}")
        text = "\n\n".join(filter(None, [data.get("descriptionPlain"), data.get("additionalPlain")]))
        text += "\n\n" + "\n".join(f"{l.get('text')}: {plain(l.get('content', ''))}" for l in data.get("lists", []))
    else:
        board = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
        text = next((j.get("descriptionPlain") or "" for j in board.get("jobs", []) if j["id"] == job_id), "")
    return {**job, "untrusted_text": True, "description": text[: args.max_chars], "truncated": len(text) > args.max_chars}


def cmd_boards(_args) -> dict:
    return {"boards": boards(), "sources": list(SOURCES)}


def cmd_add_board(args) -> dict:
    if args.source not in SOURCES:
        return {"error": f"source must be one of {', '.join(SOURCES)}"}
    current = [b for b in boards() if b["company"].lower() != args.company.lower()]
    current.append({"company": args.company, "source": args.source, "slug": args.slug})
    write_json("boards.json", current)
    return {"boards": current}


def cmd_remove_board(args) -> dict:
    current = [b for b in boards() if b["company"].lower() != args.company.lower()]
    write_json("boards.json", current)
    return {"boards": current}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="job_watch.py", description=__doc__.splitlines()[0])
    p.add_argument("--version", action="version", version=VERSION)
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("check", help="fetch boards and report roles not seen before")
    c.add_argument("--days", type=int, default=NEW_WINDOW_DAYS, help="first run: only postings this recent count as new")
    c.add_argument("--max", type=int, default=15)
    c.add_argument("--location", choices=("us", "any"), default="us")
    c.add_argument("--include", help="regex a title must match (replaces the default focus words)")
    c.add_argument("--exclude", help="extra regex for titles to drop")
    c.add_argument("--no-save", action="store_true", help="do not record these roles as seen")
    c.set_defaults(func=cmd_check)

    r = sub.add_parser("recent", help="roles first seen in the last N days (no network)")
    r.add_argument("--days", type=int, default=7)
    r.add_argument("--max", type=int, default=30)
    r.set_defaults(func=cmd_recent)

    d = sub.add_parser("details", help="official job description text for a role url")
    d.add_argument("url")
    d.add_argument("--max-chars", type=int, default=8000)
    d.set_defaults(func=cmd_details)

    sub.add_parser("boards", help="list the watched boards").set_defaults(func=cmd_boards)

    a = sub.add_parser("add-board", help="watch another company")
    a.add_argument("company")
    a.add_argument("source", choices=SOURCES)
    a.add_argument("slug", help="the board name in the company's job-board URL")
    a.set_defaults(func=cmd_add_board)

    x = sub.add_parser("remove-board", help="stop watching a company")
    x.add_argument("company")
    x.set_defaults(func=cmd_remove_board)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = args.func(args)
    except (URLError, HTTPError, TimeoutError, ValueError) as e:
        result = {"error": f"{type(e).__name__}: {e}"}
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 1 if "error" in result else 0


if __name__ == "__main__":
    sys.exit(main())
