# Claude Code as an alternate agent engine

Status: approved for planning
Date: 2026-08-30

## Motivation

Strix's agent loop (`strix/agents`, `strix/core/execution.py`, `strix/core/hooks.py`,
`strix/core/agents.py`) is built directly on OpenAI's `openai-agents` SDK
(`Runner.run`, `RunConfig`, `RunHooks`, `agents.sandbox.SandboxAgent`, the `Model`
abstraction). Every LLM call — regardless of provider — flows through that SDK's
run loop today, including the existing "run on your ChatGPT subscription" support
(`strix/config/codex.py`), which only swaps *credentials* into that same loop via a
custom `Model`.

We want an additional way to run a scan: **using a Claude Code subscription
(Pro/Max/Team/Enterprise) as the agent's actual dispatcher/tool-calling engine**,
not just as a credentialed model behind the existing loop. Concretely, an agent's
turn-by-turn reasoning and tool-call decisions are made by the real Claude Code
CLI (whatever the user is already logged into via `claude /login`), while every
tool it calls still executes through Strix's existing, sandboxed tool
implementations.

This is opt-in per run, not a replacement of the default engine.

## Non-goals (v1)

- No new OAuth flow. The `claude` CLI's own login/session is reused as-is; Strix
  never reads or manages Anthropic credentials directly.
- No change to the default engine, its behavior, or its supported providers.
- Claude Code's own subagent/Task mechanism is **not** used to replace Strix's
  multi-agent graph (`AgentCoordinator`, `create_agent`/`send_message_to_agent`).
  The graph stays engine-agnostic; only the per-agent reasoning loop is pluggable.
- No mixed engines within one run: a child spawned by a Claude-Code-driven agent
  inherits the parent's engine.
- Claude Code's native `Bash`/`Read`/`Write`/`WebSearch` tools are available to
  the agent as auxiliary research/reasoning aids (e.g. looking up a CVE, drafting
  or dry-running a PoC snippet, reading local skill docs) — but are never given
  access to the scan's sandboxed workspace or target. See "Native tool scoping"
  below. Every side effect against the target — filesystem, shell, HTTP — still
  goes only through Strix's own bridged tools.

## Architecture

### Engine seam

Introduce an engine abstraction that today's OpenAI-Agents-SDK-based run loop
implements implicitly. Extract it into two implementations:

- `OpenAIAgentsEngine` — today's path (`Runner.run` + `RunHooks` +
  `agents.sandbox.SandboxAgent`), unchanged behavior, remains the default.
- `ClaudeCodeEngine` — new. Drives one agent's turn loop via the
  `claude-agent-sdk` Python package (a new optional dependency; the package
  manages the actual `claude` CLI subprocess).

Selection mirrors the existing `chatgpt/<model>` convention:
`STRIX_LLM=claude-code/<model>` selects `ClaudeCodeEngine` for that run, parsed
the same way `strix/config/codex.py:subscription_model()` parses `chatgpt/`
(new sibling helper, e.g. `strix/config/claude_code.py:engine_model()`).

### Tool bridging

Every tool Strix already registers for an agent (`strix/agents/factory.py:
_BASE_TOOLS`, plus proxy/reporting/coverage/threat-model/agents-graph/shell/
filesystem tools) is wrapped a second way: with `claude_agent_sdk`'s `@tool`
decorator, registered into an in-process MCP server via
`create_sdk_mcp_server`, and passed to `ClaudeAgentOptions(mcp_servers=...)`
alongside Claude Code's own native tools (see "Native tool scoping" below) —
the underlying implementation functions are unchanged; only their SDK-facing
wrapper differs per engine. This keeps sandboxing, output-bounding
(`bound_and_store`), and argument coercion identical across engines, since
both wrappers call the same inner function.

### Native tool scoping

Claude Code's native `WebSearch`/`Bash`/`Read`/`Write` tools stay enabled
(not excluded via `allowed_tools`), for the agent's own auxiliary
reasoning — but are kept structurally unable to reach the scan target or
its sandboxed workspace, so no containerized-transport work is needed:

- The `claude` CLI subprocess (spawned locally by `claude-agent-sdk`, its
  default behavior) runs with `cwd` set to a scratch directory that is
  **not** the bind-mounted `/workspace` the Docker sandbox uses. Native
  `Read`/`Write` therefore cannot see or modify target source, findings,
  or run artifacts.
- Native `Bash` runs on the host process, outside the Docker sandbox
  network entirely — it has no path to the target and is not proxied
  through Caido. It's usable for local scratch work (e.g. testing a regex,
  formatting data) but never for anything that touches the target.
- Native `WebSearch` queries Anthropic's own search backend, not the
  target, so it carries no sandbox concern; Strix's own `web_search` tool
  remains available too.
- Everything that must touch the target — shell exec, file access under
  `/workspace`, HTTP to/through the target — is only reachable via Strix's
  MCP-bridged tools (`exec_command`, filesystem-via-sandbox, proxy tools),
  exactly as in the original design. The sandbox boundary is therefore
  preserved without running the `claude` CLI process inside the container.

### Sandbox, reporting, multi-agent graph

Unchanged. The Docker sandbox (`strix/runtime`), Caido proxy tools, reporting/
dedupe/SARIF pipeline (`strix/report`), and the multi-agent graph
(`strix/tools/agents_graph`, `strix/core/agents.py:AgentCoordinator`) are
reached identically regardless of which engine drives a given agent, because
they're invoked through the same tool implementations either way.

### Turn limits, cost tracking, wind-down directives

`strix/core/hooks.py` currently implements this against `agents.lifecycle.
RunHooks`. `ClaudeCodeEngine` needs an equivalent adapter driven by the
Claude Agent SDK's own hook/message-stream API (`PreToolUse`/`PostToolUse`/
turn completion events), replicating: turn counting (`LLM_TURN_KEY`),
root/subagent wind-down directive injection at the same warn bands, and cost
accumulation feeding `ReportState`/`run.json`.

## Quota-exceeded handling and resume

Claude subscription quotas reset on a multi-hour window, unlike a transient
429 — the existing `DEFAULT_MODEL_RETRY` policy (5 attempts, capped at 90s
backoff) is the wrong tool for it and must not apply to quota-exhaustion
errors from this engine.

Reuse existing machinery rather than building new state persistence:
- `strix --resume RUN_NAME` (`strix/interface/cli_args.py`) already restores
  `run.json`, targets, and agent topology (`agents.json`) for a run, for any
  reason it stopped.
- `AgentCoordinator.pause_for_budget` / `resume_from_budget_pause` /
  `trigger_budget_stop` (`strix/core/agents.py`) already implement a
  pause/resume state machine for the existing budget-exceeded case.

Behavior when `ClaudeCodeEngine` detects a subscription quota/rate-limit
error (surfaced by the SDK from the underlying CLI, which reports a reset
time):

- **Default:** treat it like the existing budget-exceeded path — persist
  state (already automatic via `ReportState`/`agents.json` writes), print the
  reported reset time and the exact `strix --resume <run_name>` command, exit
  non-zero. No new state persistence needed beyond what `--resume` already
  reads.
- **`--auto-resume` flag (new, `strix/interface/cli_args.py`):** instead of
  exiting, sleep in-process until the reported reset time, then continue the
  same run loop in place — implemented as "sleep, then do what `--resume`
  already does," not a separate resume code path. For unattended/CI use;
  the default remains the hands-off exit-and-print behavior, since a
  multi-hour held-open process is not always wanted.

## Auth / setup UX

No login command to build. `strix auth status` gains a line reporting
whether the `claude` CLi is on `PATH` and logged in (best-effort shell-out to
`claude` or a `claude-agent-sdk` status check), so `STRIX_LLM=claude-code/…`
without a working `claude` login fails fast with a clear message — mirroring
today's `environment.py` check for the ChatGPT subscription case, but with no
credential of Strix's own to validate.

## Testing

- Unit: `engine_model()` parsing (mirrors existing `codex.subscription_model`
  tests), the tool-bridging adapter (a tool registered both ways produces
  equivalent behavior for a given input), the quota-error classifier, and the
  `--auto-resume` sleep-then-continue path (with the sleep mocked).
- Integration: a scan run end-to-end against a local target with
  `STRIX_LLM=claude-code/<model>` and a real or stubbed `claude` CLI,
  asserting findings/coverage/report artifacts match parity with a run on the
  default engine for the same target.
- Manual: quota-exhaustion simulated via a stub SDK response, verifying both
  the default exit-and-print path and `--auto-resume`.

## Open items for the implementation plan

- Exact `claude-agent-sdk` API surface to pin a version against (hooks,
  `create_sdk_mcp_server`, `ClaudeAgentOptions` fields) — verify during
  planning/implementation against the installed package, not assumed here.
- Exact shape of the quota/rate-limit error the SDK surfaces (message
  format, reset-time field) — verify against the SDK's actual error types
  before implementing the classifier.
- The `claude` CLI runs on the host (outside `containers/Dockerfile`'s
  image); only Strix's bridged tools reach into the sandbox, as today. No
  sandbox image change is expected for this feature.
- A scratch `cwd` for the native-tools process needs a concrete location
  and cleanup policy (e.g. under `strix/core/paths.py`'s run directory but
  outside `/workspace`) — pick this during planning.
