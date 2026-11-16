# zapier-mcp-rundown

A small Python CLI that connects to my own Zapier MCP server, reads my GitHub profile, three of my repos and my Gmail inbox, and prints one combined rundown. No Zap, no browser, one command.

I built it while setting up Zapier MCP for the first time. The idea is simple: one command in the morning, one screen of what matters, pulled from two apps I already use.

## What it prints

```
Rundown for Monday, September 21, 2026

GitHub (adwitiyashukla): 57 public repos, 0 followers, 0 following
  marketpay: Java, last push 2026-09-08, 0 open issues, 0 stars, license none
  product-search-engine: C++, last push 2026-09-08, 0 open issues, 0 stars, license MIT
  lsm-storage-engine: C++, last push 2026-08-16, 0 open issues, 0 stars, license MIT

Gmail: 25 inbox mails in the last 24 hours, 25 unread
  Sep 21 04:45 | Google | Security alert | unread
  Sep 21 04:36 | Wade at Zapier | Welcome aboard | unread
  Sep 21 01:13 | DirectlyApply | Hey Adwitiya, 1 Python Programmer jobs near you! | unread
  top senders: noreply-accounts@google.com 3, english-quora-digest@quora.com 2, ...

Worth a look: marketpay has no license
```

That is a real run from September 21, 2026, trimmed to a few mail lines.

## How it works

Zapier MCP exposes every enabled action as an MCP tool on a URL that speaks Streamable HTTP. The script uses the official `mcp` package (2.x) as a client. It opens one session, lists the tools, then runs the reads one after another:

1. `github_find_user` for the profile
2. `github_find_repository` once per repo in `config.toml`
3. `gmail_find_email` with a search string, several times (see below)

Every result comes back as JSON text inside the tool result. The script only keeps the fields it prints. Email bodies are never printed.

If a server does not expose those direct tools but does expose Zapier's generic `execute_zapier_read_action`, the script falls back to that with the same action keys. Both paths are covered by the tests.

### Why Gmail is read in slices

Zapier's Find Email returns the full raw source and HTML body of every message, about 120 KB each, and there is no way to ask for less. On my inbox one `in:inbox` read is 100 messages and 12 MB. Zapier's streaming endpoint drops responses that large: a 25 message read (3.1 MB) fails with "SSE stream ended without a response" after 12.9 seconds, while a 1 message read (150 KB) works.

So the script never asks for the whole inbox. It splits the last `hours` into `slices` windows using Gmail's `after:` and `before:` epoch operators, reads each window on its own, and if a window still fails it halves it and retries, down to 15 minute windows. Rows are deduplicated by message id and merged. A window that fails even at 15 minutes is reported as partial instead of taking the whole run down.

## Setup

You need Python 3.11 or newer and a Zapier account.

1. Go to https://mcp.zapier.com and add an MCP server. Pick "Other" as the client.
2. Add the GitHub app and the Gmail app to it and connect the accounts you want read.
3. Open the Connect tab, click Generate token, and use Option 1: the server URL plus a Bearer token.

Then, in the project folder:

```
pip install -r requirements.txt
```

Set the two environment variables. On Windows Command Prompt:

```
set "ZAPIER_MCP_URL=https://mcp.zapier.com/api/v1/connect"
set "ZAPIER_MCP_TOKEN=your token here"
```

On bash use `export` instead of `set`. The token is a password. It never goes in a file in this repo.

Run it:

```
python rundown.py
```

Options:

| Flag | What it does |
|------|--------------|
| `--verbose` | prints every tool call, its row count and how long it took, to stderr |
| `--query "in:inbox is:important"` | overrides the Gmail search string from `config.toml` for one run |
| `--config path` | uses a different config file |

Exit code is 0 when every read worked, 1 when a source failed or was partial, 2 when `ZAPIER_MCP_URL` is missing.

## Config

Everything tunable is in `config.toml` and validated at startup.

| Key | Default | Meaning |
|-----|---------|---------|
| `github.repos` | three of my repos | repo names without the owner, read one by one |
| `gmail.query` | `in:inbox` | base Gmail search string, the time window is appended to it |
| `gmail.hours` | 24 | how far back to read |
| `gmail.slices` | 6 | how many windows to split those hours into |
| `gmail.latest` | 6 | how many of the newest mails to list |
| `gmail.top_senders` | 5 | how many senders to count |

## Measured run

From a `--verbose` run against my server on September 21, 2026:

| Read | Calls | Time per call |
|------|-------|---------------|
| GitHub profile | 1 | 2.3 s (10.3 s on a cold first call) |
| GitHub repos | 3 | 2.2 to 3.1 s |
| Gmail, 4 hour slices | 6 | 4.7 to 10.8 s |

The six slices returned 5, 6, 4, 3, 2 and 5 mails, 25 in total, none needed splitting. The whole run took about 50 seconds. Zapier bills two tasks per successful tool call and nothing for a failed one, so a default run with no splits is 10 calls and 20 tasks.

## Tests

```
python -m pytest -q
```

35 tests. They use a fake MCP session that returns real `mcp.types` objects, so the tool selection, the result parsing, the slice math, the split and retry path and the report formatting all run without a network. The fallback to the generic execute tool, the partial failure output and the unreachable server message are covered too.

## Layout

```
rundown.py              the whole CLI
config.toml             tunables
requirements.txt        mcp and pytest
tests/test_rundown.py   unit tests
```

## License

MIT
