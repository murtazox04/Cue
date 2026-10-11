# Coding agents

Coding agents (OpenHands, Claude Code) work for minutes or hours, often several at once.
`cue-hook` lets them tell you when they finish or need you. Cue decides how: it merges
pings from parallel agents, keeps them out of your quiet hours, and sends them over your
own channels.

## Install

```bash
pip install cue-notify-client   # provides the cue-hook command
export CUE_URL=https://cue.example.com CUE_API_KEY=ck_… CUE_RECIPIENT=dev-42
```

`CUE_RECIPIENT` is your external id in Cue. Use a key bound to an [agent](../concepts/agents.md)
if you want a per-person budget on these pings.

## OpenHands

`.openhands/hooks.json`:

```json
{
  "stop": [{"matcher": "*", "hooks": [{"command": "cue-hook", "async": true}]}],
  "session_end": [{"matcher": "*", "hooks": [{"command": "cue-hook", "async": true}]}]
}
```

## Claude Code

`.claude/settings.json`. The `Notification` event fires when Claude is waiting for your
input:

```json
{
  "hooks": {
    "Notification": [{"hooks": [{"type": "command", "command": "cue-hook"}]}],
    "Stop": [{"hooks": [{"type": "command", "command": "cue-hook"}]}]
  }
}
```

## Events and a rule

Each hook becomes an event named after it: `agent.stop`, `agent.notification`,
`agent.session_end`. Their `data` holds `hook`, `session_id`, `project`, `agent`, and,
when the agent sends them, `message` and `stop_reason`.

```json
POST /v1/rules
{"key": "agents-waiting", "name": "Agent needs me", "event": "agent.notification",
 "template": "agent-waiting", "category": "agents", "channels": ["push", "telegram"],
 "importance": "high", "digest": {"window": "PT2M", "send_after": 5}}
```

The digest turns five agents stopping within two minutes into one notification. Create
the `agents` category first (`POST /v1/categories`), with whatever caps suit you.

The hook never blocks the agent. It always exits 0 and writes failures to stderr.
