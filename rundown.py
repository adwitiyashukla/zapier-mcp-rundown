import argparse
import asyncio
import json
import logging
import os
import sys
import time
import tomllib
from collections import Counter
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client


GITHUB_API = "GitHubCLIAPI"
GMAIL_API = "GoogleMailV2CLIAPI"
GENERIC_TOOL = "execute_zapier_read_action"
INSTRUCTIONS = "Run this action with the given parameters exactly as provided."
MIN_SLICE_SECONDS = 900


def load_config(path):
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    github = cfg.get("github", {})
    gmail = cfg.get("gmail", {})
    repos = github.get("repos")
    if not isinstance(repos, list) or not repos:
        raise ValueError("github.repos must be a non-empty list of repo names")
    for repo in repos:
        if not isinstance(repo, str) or not repo.strip() or "/" in repo:
            raise ValueError("github.repos entries must be plain repo names without the owner")
    query = gmail.get("query", "in:inbox")
    if not isinstance(query, str) or not query.strip():
        raise ValueError("gmail.query must be a non-empty string")
    latest = gmail.get("latest", 6)
    top_senders = gmail.get("top_senders", 5)
    hours = gmail.get("hours", 24)
    slices = gmail.get("slices", 6)
    checks = (
        ("gmail.latest", latest, 1, 50),
        ("gmail.top_senders", top_senders, 1, 50),
        ("gmail.hours", hours, 1, 720),
        ("gmail.slices", slices, 1, 48),
    )
    for name, value, low, high in checks:
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f"{name} must be an integer between {low} and {high}")
    return {
        "repos": [r.strip() for r in repos],
        "query": query.strip(),
        "latest": latest,
        "top_senders": top_senders,
        "hours": hours,
        "slices": slices,
    }


def override_query(cfg, query):
    if query is None:
        return cfg
    if not query.strip():
        raise ValueError("--query must not be empty")
    return {**cfg, "query": query.strip()}


def read_specs(cfg):
    specs = [("profile", GITHUB_API, "user", "github_find_user", {})]
    for repo in cfg["repos"]:
        specs.append((f"repo:{repo}", GITHUB_API, "repository_v2", "github_find_repository", {"repo": repo}))
    return specs


def gmail_windows(now, hours, slices):
    end = int(now)
    start = end - hours * 3600
    step = (end - start) / slices
    edges = [start + round(i * step) for i in range(slices)] + [end]
    return [(edges[i], edges[i + 1]) for i in range(slices)]


def gmail_query(base, start, end):
    return f"{base} after:{start} before:{end}"


def build_call(tools, selected_api, action, tool_name, params):
    if tool_name in tools:
        args = dict(params)
        schema = tools[tool_name].input_schema or {}
        if "instructions" in schema.get("properties", {}):
            args["instructions"] = INSTRUCTIONS
        return tool_name, args
    if GENERIC_TOOL in tools:
        return GENERIC_TOOL, {
            "selected_api": selected_api,
            "action": action,
            "tool_name": tool_name,
            "params": params,
        }
    raise RuntimeError(f"server has neither {tool_name} nor {GENERIC_TOOL}, tools seen: {sorted(tools)}")


def parse_result(result):
    texts = [c.text for c in result.content if getattr(c, "text", None)]
    if result.is_error:
        raise RuntimeError(" ".join(texts) or "tool call failed")
    data = None
    for text in texts:
        try:
            data = json.loads(text)
            break
        except ValueError:
            continue
    if data is None:
        data = result.structured_content
    if isinstance(data, dict) and set(data) == {"result"}:
        data = data["result"]
    if isinstance(data, str):
        data = json.loads(data)
    if isinstance(data, dict) and "results" in data:
        data = data["results"]
    if data is None:
        return []
    if isinstance(data, dict):
        return [data]
    return list(data)


async def call_read(session, tools, key, api, action, tool_name, params, log):
    name, args = build_call(tools, api, action, tool_name, params)
    if log:
        log(f"{key}: calling {name} with {params}")
    started = time.monotonic()
    try:
        rows = parse_result(await session.call_tool(name, args))
    except Exception as exc:
        message = str(exc) or type(exc).__name__
        if log:
            log(f"{key}: failed after {time.monotonic() - started:.1f}s, {message}")
        return None, message
    if log:
        log(f"{key}: {len(rows)} rows in {time.monotonic() - started:.1f}s")
    return rows, None


async def read_inbox_window(session, tools, cfg, start, end, log):
    params = {"query": gmail_query(cfg["query"], start, end)}
    rows, error = await call_read(session, tools, "inbox", GMAIL_API, "message", "gmail_find_email", params, log)
    if error is None:
        return rows, []
    if end - start <= MIN_SLICE_SECONDS:
        return [], [error]
    mid = (start + end) // 2
    if log:
        log(f"inbox: splitting {start}-{end} in half")
    left_rows, left_errors = await read_inbox_window(session, tools, cfg, start, mid, log)
    right_rows, right_errors = await read_inbox_window(session, tools, cfg, mid, end, log)
    return left_rows + right_rows, left_errors + right_errors


def dedupe(rows):
    seen = set()
    out = []
    for r in rows:
        key = r.get("id") or json.dumps(r, sort_keys=True)
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


async def collect(session, cfg, log=None, now=None):
    listed = await session.list_tools()
    tools = {t.name: t for t in listed.tools}
    if log:
        log(f"tools on server: {', '.join(sorted(tools))}")
    data = {}
    errors = {}
    for key, api, action, tool_name, params in read_specs(cfg):
        rows, error = await call_read(session, tools, key, api, action, tool_name, params, log)
        data[key] = rows or []
        if error:
            errors[key] = error
    inbox = []
    failed = []
    windows = gmail_windows(time.time() if now is None else now, cfg["hours"], cfg["slices"])
    for start, end in windows:
        rows, window_errors = await read_inbox_window(session, tools, cfg, start, end, log)
        inbox.extend(rows)
        failed.extend(window_errors)
    data["inbox"] = dedupe(inbox)
    if failed:
        errors["inbox"] = f"{len(failed)} time slice(s) failed, last error: {failed[-1]}"
    return data, errors


def summarize_profile(rows):
    p = rows[0] if rows else {}
    return {
        "login": p.get("login", ""),
        "name": p.get("name") or p.get("login", ""),
        "public_repos": p.get("public_repos", 0),
        "followers": p.get("followers", 0),
        "following": p.get("following", 0),
    }


def summarize_repo(rows, fallback_name):
    r = rows[0] if rows else {}
    license_info = r.get("license") or {}
    return {
        "name": r.get("name", fallback_name),
        "language": r.get("language") or "n/a",
        "pushed_at": (r.get("pushed_at") or "")[:10],
        "open_issues": r.get("open_issues_count", 0),
        "stars": r.get("stargazers_count", 0),
        "license": license_info.get("spdx_id") or "none",
    }


def parse_date(value):
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone()


def summarize_inbox(rows, latest, top_senders, hours=24):
    mails = []
    for r in rows:
        sender = r.get("from") or {}
        mails.append({
            "date": parse_date(r.get("date")),
            "name": sender.get("name") or sender.get("email", ""),
            "email": sender.get("email", ""),
            "subject": r.get("subject", ""),
            "unread": "UNREAD" in (r.get("labels") or []),
        })
    floor = datetime.min.replace(tzinfo=timezone.utc)
    mails.sort(key=lambda m: m["date"] or floor, reverse=True)
    dates = [m["date"] for m in mails if m["date"]]
    counts = Counter(m["email"] for m in mails if m["email"])
    return {
        "total": len(mails),
        "unread": sum(1 for m in mails if m["unread"]),
        "oldest": min(dates) if dates else None,
        "newest": max(dates) if dates else None,
        "latest": mails[:latest],
        "top_senders": counts.most_common(top_senders),
        "hours": hours,
    }


def format_rundown(today, profile, repos, inbox, errors=None):
    errors = errors or {}
    lines = [f"Rundown for {today:%A, %B %d, %Y}", ""]
    if "profile" in errors:
        lines.append(f"GitHub: profile read failed, {errors['profile']}")
    else:
        lines.append(
            f"GitHub ({profile['login']}): {profile['public_repos']} public repos, "
            f"{profile['followers']} followers, {profile['following']} following"
        )
    for r in repos:
        repo_key = f"repo:{r['name']}"
        if repo_key in errors:
            lines.append(f"  {r['name']}: read failed, {errors[repo_key]}")
            continue
        lines.append(
            f"  {r['name']}: {r['language']}, last push {r['pushed_at'] or 'unknown'}, "
            f"{r['open_issues']} open issues, {r['stars']} stars, license {r['license']}"
        )
    lines.append("")
    if inbox["total"] == 0 and "inbox" in errors:
        lines.append(f"Gmail: read failed, {errors['inbox']}")
    elif inbox["total"] == 0:
        lines.append(f"Gmail: no inbox mail in the last {inbox['hours']} hours")
    else:
        lines.append(f"Gmail: {inbox['total']} inbox mails in the last {inbox['hours']} hours, {inbox['unread']} unread")
        for m in inbox["latest"]:
            when = f"{m['date']:%b %d %H:%M}" if m["date"] else "unknown"
            flag = "unread" if m["unread"] else "read"
            lines.append(f"  {when} | {m['name']} | {m['subject']} | {flag}")
        if inbox["top_senders"]:
            top = ", ".join(f"{email} {n}" for email, n in inbox["top_senders"])
            lines.append(f"  top senders: {top}")
        if "inbox" in errors:
            lines.append(f"  partial: {errors['inbox']}")
    notes = [f"{r['name']} has no license" for r in repos if r["license"] == "none" and "repo:" + r["name"] not in errors]
    if notes:
        lines.append("")
        lines.append("Worth a look: " + "; ".join(notes))
    return "\n".join(lines)


async def fetch(url, token, cfg, log=None):
    headers = {"Authorization": f"Bearer {token}"} if token else None
    async with create_mcp_http_client(headers=headers) as http_client:
        async with streamable_http_client(url, http_client=http_client) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await collect(session, cfg, log)


def build_report(data, cfg, today, errors=None):
    profile = summarize_profile(data["profile"])
    repos = [summarize_repo(data[f"repo:{name}"], name) for name in cfg["repos"]]
    inbox = summarize_inbox(data["inbox"], cfg["latest"], cfg["top_senders"], cfg.get("hours", 24))
    return format_rundown(today, profile, repos, inbox, errors)


def log_to_stderr(message):
    print(message, file=sys.stderr, flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="print a GitHub and Gmail rundown through Zapier MCP")
    default_config = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.toml")
    parser.add_argument("--config", default=default_config)
    parser.add_argument("--query", default=None, help="gmail search string, overrides config.toml")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    url = os.environ.get("ZAPIER_MCP_URL")
    if not url:
        print("set ZAPIER_MCP_URL to your Zapier MCP server url", file=sys.stderr)
        return 2
    logging.getLogger("mcp").setLevel(logging.ERROR)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    cfg = override_query(load_config(args.config), args.query)
    log = log_to_stderr if args.verbose else None
    data, errors = asyncio.run(fetch(url, os.environ.get("ZAPIER_MCP_TOKEN"), cfg, log))
    print(build_report(data, cfg, datetime.now(), errors))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
