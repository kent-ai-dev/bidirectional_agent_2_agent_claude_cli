# cc-bridge

A tiny bidirectional bridge between **Hermes** (any external agent) and a
**specific** running Claude Code instance. One bridge process per instance,
each with its own port and friendly id, all discoverable through a shared
registry. Stdlib only — no pip installs.

```
Hermes ──POST /send──▶ cc-bridge ──tmux send-keys──▶ Claude Code (pane)
Hermes ◀──GET /recv─── cc-bridge ◀──Stop hook /_reply── Claude Code (turn end)
```

## Prerequisites

- `python3`, `tmux`, and Claude Code with hooks enabled.
- Each Claude Code instance must run **inside a named tmux pane** (that's how the
  bridge delivers into the exact instance; see Limitations for the no-tmux path).

## Install

1. Clone this repo (or drop `cc_bridge.py` somewhere stable, e.g.
   `/opt/cc-bridge/cc_bridge.py`):

   ```bash
   git clone https://github.com/kent-ai-dev/bidirectional_agent_2_agent_claude_cli.git
   ```
2. Merge `settings.snippet.json` into `~/.claude/settings.json` (adjust the
   `cc_bridge.py` path to wherever you cloned it). This wires the `SessionStart`
   and `Stop` hooks for **every** instance.

## Launch an instance

Each instance gets a unique `id` and `port`. Pairing happens via the
`CC_BRIDGE_PORT` env var — the `SessionStart` hook reads it plus `$TMUX_PANE`
and registers the pane with its bridge. Drop this helper in your shell rc:

```bash
cc_instance() {                      # usage: cc_instance <id> <port> [workdir]
  local id="$1" port="$2" wd="${3:-$PWD}"
  python3 /opt/cc-bridge/cc_bridge.py serve --id "$id" --port "$port" \
      --hermes-url "${HERMES_URL:-}" >>"$HOME/.cc-bridge/$id.log" 2>&1 &
  tmux new-window -n "$id" "cd '$wd' && CC_BRIDGE_PORT=$port claude"
}
```

```bash
cc_instance trading-ea  8131 ~/strategies/momentum
cc_instance research    8132 ~/notes
cc_instance ops         8133 ~/infra
```

`--hermes-url` is optional: set it (or export `HERMES_URL`) to have every
finished turn POSTed straight to Hermes as `{"from": "<id>", "text": "..."}`.
Leave it unset and Hermes pulls with `recv` instead.

## Hermes side

```bash
# discover live instances (auto-prunes dead ones)
python3 cc_bridge.py list

# send to a specific instance
python3 cc_bridge.py send --to trading-ea \
    --text "rerun the MomentumTestScaffold backtest on XAUUSD H4, report Sharpe + maxDD"

# pull replies (long-poll up to 25s with --wait)
python3 cc_bridge.py recv --to trading-ea --since 0 --wait
```

Or talk raw HTTP from any language Hermes is written in:

```bash
curl -s localhost:8131/send  -d '{"from":"hermes","text":"status?"}'
curl -s 'localhost:8131/recv?since=0&wait=1'
curl -s localhost:8131/status
```

`recv` returns a `cursor`; pass it as the next `since` to get only new replies.

## How identity works

Every bridge writes `{id, port, pid, pane, started}` to
`~/.cc-bridge/registry.json` on startup and removes itself on exit. `list`
prunes any entry whose PID is dead, so a crashed instance never misleads Hermes.
Hermes addresses instances by `id`; the client resolves `id → port` from the
registry. That's the unique self-identification across all open windows.

## Message flow detail

- **Inbound** is queued and delivered **one at a time, only when the instance is
  idle**. If you `send` while a turn is running, it queues and flushes when the
  instance next finishes (its `Stop` hook clears the busy flag). A 15-min
  safety timeout clears a stuck flag if a turn dies without a `Stop`.
- Messages ≤ 600 chars and single-line are typed inline. Longer/multiline
  messages are written to `~/.cc-bridge/msgs/<id>/<msg_id>.txt` and the instance
  is told to read that file — avoids premature submits from embedded newlines.

## Limitations / next steps (the honest part)

1. **tmux is the inbound transport.** Instances not in tmux can't be reached by
   `send-keys`. If you don't want tmux, swap the inbound hop for
   [`mberg/agent-http`](https://github.com/mberg/agent-http) (native MCP channel,
   no tmux, exact messages) — it exposes the same `POST /message` shape but rides
   the experimental `--dangerously-load-development-channels` flag.
2. **Reply extraction parses the transcript.** `last_assistant_text()` reads the
   Claude Code transcript JSONL for the last assistant turn. That schema is not a
   stable public contract; if a CC update changes it, that one function is the
   spot to adjust. (More robust alternative: give the instance an MCP `reply`
   tool it calls explicitly, instead of scraping the transcript on `Stop`.)
3. **No request/response correlation yet.** Every finished turn is relayed, and a
   reply isn't tied to the `msg_id` that triggered it. When you need strict
   pairing, echo the `msg_id` into the prompt and have the instance prefix its
   reply with it, then match on `/_reply`.
4. **Bound to 127.0.0.1.** If Hermes lives on another host, tunnel over SSH or
   bind to an interface with auth in front — don't expose these ports raw.
