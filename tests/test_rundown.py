import asyncio
import json
import os
import sys
from datetime import datetime, timezone

import pytest
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rundown


PROFILE = {"login": "adwitiyashukla", "name": "Adwitiya Shukla", "public_repos": 56, "followers": 0, "following": 0}

REPO_MIT = {
    "name": "product-search-engine",
    "language": "C++",
    "pushed_at": "2026-09-08T11:59:33Z",
    "open_issues_count": 0,
    "stargazers_count": 0,
    "license": {"spdx_id": "MIT"},
}

REPO_NO_LICENSE = {
    "name": "marketpay",
    "language": "Java",
    "pushed_at": "2026-09-08T10:24:01Z",
    "open_issues_count": 2,
    "stargazers_count": 1,
    "license": None,
}

MAILS = [
    {
        "id": "m1",
        "subject": "Security alert",
        "from": {"name": "Google", "email": "no-reply@accounts.google.com"},
        "date": "Mon, 21 Sep 2026 08:45:33 GMT",
        "labels": ["UNREAD", "INBOX"],
    },
    {
        "id": "m2",
        "subject": "1 Python Programmer jobs near you",
        "from": {"name": "DirectlyApply", "email": "team@directlyapply.com"},
        "date": "Mon, 21 Sep 2026 05:13:14 +0000",
        "labels": ["UNREAD", "INBOX"],
    },
    {
        "id": "m3",
        "subject": "Your weekly digest",
        "from": {"name": "Quora", "email": "digest@quora.com"},
        "date": "Sun, 20 Sep 2026 09:00:00 +0000",
        "labels": ["INBOX"],
    },
    {
        "id": "m4",
        "subject": "Another digest",
        "from": {"name": "Quora", "email": "digest@quora.com"},
        "date": "Sat, 19 Sep 2026 09:00:00 +0000",
        "labels": ["UNREAD", "INBOX"],
    },
]

NOW = 1_800_000_000

CFG = {
    "repos": ["marketpay", "product-search-engine"],
    "query": "in:inbox",
    "latest": 2,
    "top_senders": 2,
    "hours": 24,
    "slices": 2,
}

def tool(name, props):
    return Tool(name=name, input_schema={"type": "object", "properties": props})


def text_result(payload, is_error=False):
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(payload))], is_error=is_error)


DIRECT_TOOLS = [
    tool("github_find_user", {"instructions": {}}),
    tool("github_find_repository", {"instructions": {}, "repo": {}}),
    tool("gmail_find_email", {"instructions": {}, "query": {}}),
]


class FakeSession:
    def __init__(self, tools, responder):
        self.tools = tools
        self.responder = responder
        self.calls = []

    async def list_tools(self):
        return ListToolsResult(tools=self.tools)

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return self.responder(name, arguments)


def github_only(name, arguments):
    if name == "github_find_user":
        return text_result({"results": [PROFILE]})
    if name == "github_find_repository":
        repo = REPO_NO_LICENSE if arguments["repo"] == "marketpay" else REPO_MIT
        return text_result({"results": [repo]})
    return None


def window_of(arguments):
    parts = dict(p.split(":") for p in arguments["query"].split()[1:])
    return int(parts["after"]), int(parts["before"])


def write_config(tmp_path, body):
    path = tmp_path / "config.toml"
    path.write_text(body)
    return str(path)


def test_load_config_defaults(tmp_path):
    cfg = rundown.load_config(write_config(tmp_path, '[github]\nrepos = ["a", "b"]\n'))
    assert cfg == {"repos": ["a", "b"], "query": "in:inbox", "latest": 6, "top_senders": 5, "hours": 24, "slices": 6}


@pytest.mark.parametrize(
    "body",
    [
        "[github]\nrepos = []\n",
        '[github]\nrepos = ["owner/repo"]\n',
        '[github]\nrepos = ["a"]\n[gmail]\nquery = ""\n',
        '[github]\nrepos = ["a"]\n[gmail]\nlatest = 0\n',
        '[github]\nrepos = ["a"]\n[gmail]\ntop_senders = true\n',
        '[github]\nrepos = ["a"]\n[gmail]\nhours = 0\n',
        '[github]\nrepos = ["a"]\n[gmail]\nslices = 100\n',
    ],
)
def test_load_config_rejects_bad_values(tmp_path, body):
    with pytest.raises(ValueError):
        rundown.load_config(write_config(tmp_path, body))


def test_override_query():
    assert rundown.override_query(CFG, None) is CFG
    assert rundown.override_query(CFG, " in:inbox is:important ")["query"] == "in:inbox is:important"
    assert rundown.override_query(CFG, "x")["repos"] == CFG["repos"]
    with pytest.raises(ValueError):
        rundown.override_query(CFG, "   ")


def test_gmail_windows_cover_the_span_without_gaps():
    windows = rundown.gmail_windows(NOW, 24, 6)
    assert len(windows) == 6
    assert windows[0][0] == NOW - 24 * 3600
    assert windows[-1][1] == NOW
    assert all(windows[i][1] == windows[i + 1][0] for i in range(5))
    assert all(end - start in (14400,) for start, end in windows)


def test_gmail_query_uses_epoch_operators():
    assert rundown.gmail_query("in:inbox", 10, 20) == "in:inbox after:10 before:20"


def test_build_call_prefers_direct_tool_and_adds_instructions():
    tools = {"gmail_find_email": tool("gmail_find_email", {"instructions": {}, "query": {}})}
    name, args = rundown.build_call(tools, rundown.GMAIL_API, "message", "gmail_find_email", {"query": "in:inbox"})
    assert name == "gmail_find_email"
    assert args == {"query": "in:inbox", "instructions": rundown.INSTRUCTIONS}


def test_build_call_falls_back_to_generic_tool():
    tools = {rundown.GENERIC_TOOL: tool(rundown.GENERIC_TOOL, {})}
    name, args = rundown.build_call(tools, rundown.GITHUB_API, "user", "github_find_user", {})
    assert name == rundown.GENERIC_TOOL
    assert args == {"selected_api": rundown.GITHUB_API, "action": "user", "tool_name": "github_find_user", "params": {}}


def test_build_call_fails_when_nothing_matches():
    with pytest.raises(RuntimeError):
        rundown.build_call({"other": tool("other", {})}, rundown.GITHUB_API, "user", "github_find_user", {})


def test_parse_result_unwraps_results_key():
    assert rundown.parse_result(text_result({"results": [PROFILE]})) == [PROFILE]


def test_parse_result_wraps_single_object():
    assert rundown.parse_result(text_result(PROFILE)) == [PROFILE]


def test_parse_result_reads_structured_result_string():
    result = CallToolResult(content=[], structured_content={"result": json.dumps({"results": MAILS})})
    assert rundown.parse_result(result) == MAILS


def test_parse_result_raises_on_error():
    with pytest.raises(RuntimeError):
        rundown.parse_result(text_result({"error": "no default connection"}, is_error=True))


def test_dedupe_keeps_first_copy_of_each_id():
    rows = [MAILS[0], MAILS[1], dict(MAILS[0]), {"subject": "no id"}, {"subject": "no id"}]
    assert rundown.dedupe(rows) == [MAILS[0], MAILS[1], {"subject": "no id"}]


def test_collect_reads_github_then_each_gmail_window():
    def responder(name, arguments):
        if name == "gmail_find_email":
            start, end = window_of(arguments)
            return text_result({"results": MAILS[:2] if end == NOW else MAILS[2:]})
        return github_only(name, arguments)

    session = FakeSession(DIRECT_TOOLS, responder)
    data, errors = asyncio.run(rundown.collect(session, CFG, now=NOW))
    assert errors == {}
    assert set(data) == {"profile", "repo:marketpay", "repo:product-search-engine", "inbox"}
    assert data["repo:marketpay"][0]["name"] == "marketpay"
    assert [m["id"] for m in data["inbox"]] == ["m3", "m4", "m1", "m2"]
    names = [name for name, _ in session.calls]
    assert names == ["github_find_user", "github_find_repository", "github_find_repository", "gmail_find_email", "gmail_find_email"]
    assert all("instructions" in args for _, args in session.calls)
    assert [window_of(a) for n, a in session.calls if n == "gmail_find_email"] == rundown.gmail_windows(NOW, 24, 2)


def test_collect_splits_a_window_that_is_too_big():
    def responder(name, arguments):
        if name == "gmail_find_email":
            start, end = window_of(arguments)
            if end - start > 6 * 3600:
                raise RuntimeError("SSE stream ended without a response")
            return text_result({"results": [MAILS[0]] if end == NOW else [MAILS[1]]})
        return github_only(name, arguments)

    logged = []
    session = FakeSession(DIRECT_TOOLS, responder)
    data, errors = asyncio.run(rundown.collect(session, CFG, logged.append, now=NOW))
    assert errors == {}
    assert [m["id"] for m in data["inbox"]] == ["m2", "m1"]
    spans = [end - start for n, a in session.calls if n == "gmail_find_email" for start, end in [window_of(a)]]
    assert spans == [43200, 21600, 21600, 43200, 21600, 21600]
    assert sum(1 for line in logged if "splitting" in line) == 2


def test_collect_gives_up_on_a_tiny_window_and_reports_it():
    def responder(name, arguments):
        if name == "gmail_find_email":
            start, end = window_of(arguments)
            if end == NOW:
                raise RuntimeError("still too big")
            return text_result({"results": [MAILS[3]]})
        return github_only(name, arguments)

    cfg = {**CFG, "hours": 1, "slices": 2}
    session = FakeSession(DIRECT_TOOLS, responder)
    data, errors = asyncio.run(rundown.collect(session, cfg, now=NOW))
    assert [m["id"] for m in data["inbox"]] == ["m4"]
    assert errors["inbox"].startswith("1 time slice(s) failed")
    assert "still too big" in errors["inbox"]
    smallest = min(end - start for n, a in session.calls if n == "gmail_find_email" for start, end in [window_of(a)])
    assert smallest == rundown.MIN_SLICE_SECONDS


def test_collect_uses_generic_tool_when_direct_tools_are_missing():
    def responder(name, arguments):
        assert name == rundown.GENERIC_TOOL
        if arguments["action"] == "user":
            return text_result({"results": [PROFILE]})
        if arguments["action"] == "repository_v2":
            return text_result({"results": [REPO_MIT]})
        assert arguments["params"]["query"].startswith("in:inbox after:")
        return text_result({"results": []})

    session = FakeSession([tool(rundown.GENERIC_TOOL, {})], responder)
    data, errors = asyncio.run(rundown.collect(session, CFG, now=NOW))
    assert errors == {}
    assert data["profile"][0]["login"] == "adwitiyashukla"
    assert data["inbox"] == []


def test_collect_reports_a_failed_github_read_and_continues():
    def responder(name, arguments):
        if name == "github_find_user":
            raise RuntimeError("boom")
        if name == "gmail_find_email":
            return text_result({"results": []})
        return github_only(name, arguments)

    session = FakeSession(DIRECT_TOOLS, responder)
    data, errors = asyncio.run(rundown.collect(session, CFG, now=NOW))
    assert errors == {"profile": "boom"}
    assert data["profile"] == []
    assert data["repo:marketpay"][0]["name"] == "marketpay"


def test_summarize_inbox_counts_and_orders():
    inbox = rundown.summarize_inbox(list(reversed(MAILS)), latest=2, top_senders=1, hours=48)
    assert inbox["total"] == 4
    assert inbox["unread"] == 3
    assert inbox["hours"] == 48
    assert [m["subject"] for m in inbox["latest"]] == ["Security alert", "1 Python Programmer jobs near you"]
    assert inbox["top_senders"] == [("digest@quora.com", 2)]
    assert inbox["oldest"] < inbox["newest"]


def test_summarize_inbox_handles_bad_dates():
    inbox = rundown.summarize_inbox([{"subject": "x", "from": {}, "date": "not a date", "labels": []}], 5, 5)
    assert inbox["latest"][0]["date"] is None
    assert inbox["oldest"] is None


def test_summarize_repo_defaults():
    repo = rundown.summarize_repo([], "ghost")
    assert repo == {"name": "ghost", "language": "n/a", "pushed_at": "", "open_issues": 0, "stars": 0, "license": "none"}


def test_format_rundown_flags_missing_license():
    today = datetime(2026, 9, 21, tzinfo=timezone.utc)
    profile = rundown.summarize_profile([PROFILE])
    repos = [rundown.summarize_repo([REPO_NO_LICENSE], "marketpay"), rundown.summarize_repo([REPO_MIT], "product-search-engine")]
    inbox = rundown.summarize_inbox(MAILS, 2, 2)
    text = rundown.format_rundown(today, profile, repos, inbox)
    assert text.startswith("Rundown for Monday, September 21, 2026")
    assert "GitHub (adwitiyashukla): 56 public repos" in text
    assert "marketpay: Java, last push 2026-09-08, 2 open issues, 1 stars, license none" in text
    assert "Gmail: 4 inbox mails in the last 24 hours, 3 unread" in text
    assert "Worth a look: marketpay has no license" in text
    assert text.isascii()


def test_format_rundown_empty_inbox():
    today = datetime(2026, 9, 21, tzinfo=timezone.utc)
    text = rundown.format_rundown(today, rundown.summarize_profile([]), [], rundown.summarize_inbox([], 6, 5, 12))
    assert "Gmail: no inbox mail in the last 12 hours" in text


def test_format_rundown_reports_failed_sources():
    today = datetime(2026, 9, 21, tzinfo=timezone.utc)
    repos = [rundown.summarize_repo([], "marketpay"), rundown.summarize_repo([REPO_MIT], "product-search-engine")]
    errors = {"profile": "boom", "repo:marketpay": "timeout", "inbox": "SSE stream ended without a response"}
    text = rundown.format_rundown(today, rundown.summarize_profile([]), repos, rundown.summarize_inbox([], 6, 5), errors)
    assert "GitHub: profile read failed, boom" in text
    assert "marketpay: read failed, timeout" in text
    assert "product-search-engine: C++" in text
    assert "Gmail: read failed, SSE stream ended without a response" in text
    assert "Worth a look" not in text


def test_format_rundown_shows_partial_inbox():
    today = datetime(2026, 9, 21, tzinfo=timezone.utc)
    inbox = rundown.summarize_inbox(MAILS[:2], 6, 5)
    text = rundown.format_rundown(today, rundown.summarize_profile([PROFILE]), [], inbox, {"inbox": "1 time slice(s) failed"})
    assert "Gmail: 2 inbox mails in the last 24 hours, 2 unread" in text
    assert "partial: 1 time slice(s) failed" in text


def test_build_report_end_to_end():
    data = {
        "profile": [PROFILE],
        "repo:marketpay": [REPO_NO_LICENSE],
        "repo:product-search-engine": [REPO_MIT],
        "inbox": MAILS,
    }
    text = rundown.build_report(data, CFG, datetime(2026, 9, 21, tzinfo=timezone.utc))
    assert "product-search-engine: C++" in text
    assert "top senders: digest@quora.com 2" in text
    partial = rundown.build_report({**data, "inbox": []}, CFG, datetime(2026, 9, 21, tzinfo=timezone.utc), {"inbox": "nope"})
    assert "Gmail: read failed, nope" in partial


def test_main_requires_url(monkeypatch, capsys):
    monkeypatch.delenv("ZAPIER_MCP_URL", raising=False)
    assert rundown.main([]) == 2
    assert "ZAPIER_MCP_URL" in capsys.readouterr().err


def test_root_cause_unwraps_nested_groups():
    inner = ConnectionError("All connection attempts failed")
    wrapped = ExceptionGroup("outer", [ExceptionGroup("inner", [inner])])
    assert rundown.root_cause(wrapped) is inner
    assert rundown.root_cause(inner) is inner


def test_main_reports_unreachable_server_in_one_line(monkeypatch, capsys, tmp_path):
    async def failing_fetch(url, token, cfg, log=None):
        raise ExceptionGroup("boom", [ConnectionError("All connection attempts failed")])

    monkeypatch.setenv("ZAPIER_MCP_URL", "http://127.0.0.1:1/mcp")
    monkeypatch.setattr(rundown, "fetch", failing_fetch)
    assert rundown.main(["--config", write_config(tmp_path, '[github]\nrepos = ["a"]\n')]) == 1
    err = capsys.readouterr().err
    assert err.count("\n") == 1
    assert "could not reach the MCP server at http://127.0.0.1:1/mcp" in err
    assert "ConnectionError: All connection attempts failed" in err
