<div align="center">

# inovar-strix

### Inovar +AZ's AI-powered penetration testing tool, powered by [Strix.ai](https://strix.ai)

Autonomous AI hackers that find and exploit your applications' vulnerabilities - running your code dynamically and validating every finding with a real, working proof-of-concept, not a static-analysis guess.

<br/>

<a href="https://docs.strix.ai"><img src="https://img.shields.io/badge/Engine%20Docs-docs.strix.ai-2b9246?style=for-the-badge&logo=gitbook&logoColor=white" alt="Docs"></a>
<a href="LICENSE"><img src="https://img.shields.io/badge/License-Apache%202.0-3b82f6?style=flat-square" alt="License"></a>

</div>

> [!TIP]
> **New in this build:** run scans through the Claude Code engine, driving the agent's turn loop with your own Claude subscription instead of a metered API key. See [Run on your Claude Code subscription](#run-on-your-claude-code-subscription) below.

---

## Overview

**inovar-strix** is Inovar +AZ's internal build of [Strix](https://strix.ai), an open-source autonomous AI pentesting framework. The agents act like real hackers - they run your code dynamically, find vulnerabilities, and validate them through actual proofs-of-concept - so findings come with a working exploit, not a false-positive-prone static-analysis alert.

**Key Capabilities:**

- **Full pentesting toolkit** - reconnaissance, exploitation, and validation out of the box
- **Multi-agent orchestration** - teams of AI pentesters that collaborate and scale
- **Real exploit validation** - working PoCs, not false positives like legacy vulnerability scanners
- **Developer‑first CLI** - actionable findings with remediation guidance
- **Auto‑fix & reporting** - generate patches and compliance-ready pentest reports
- **Run on your own Claude subscription** - drive scans through the Claude Code engine instead of a metered API key (new in this build)

<br>

<div align="center">
  <img src=".github/screenshot.png" alt="Pentest Demo" width="1000" style="border-radius: 16px;">
</div>

## Use Cases

- **Application Security Testing** - Detect and validate critical vulnerabilities in your applications
- **Rapid Penetration Testing** - Get penetration tests done in hours, not weeks, with compliance reports
- **Bug Bounty Automation** - Automate bug bounty research and generate PoCs for faster reporting
- **CI/CD Integration** - Run tests in CI/CD to block vulnerabilities before reaching production

## 🚀 Quick Start

**Prerequisites:**
- Docker (running)
- An LLM API key from any [supported provider](https://docs.strix.ai/llm-providers/overview) (OpenAI, Anthropic, Google, etc.) - or a Claude Code subscription, see below

### Installation & First Scan

```bash
# Install the CLI
curl -sSL https://strix.ai/install | bash

# Configure your AI provider
export STRIX_LLM="openai/gpt-5.4"
export LLM_API_KEY="your-api-key"

# Run your first security assessment
strix --target ./app-directory
```

> [!NOTE]
> First run automatically pulls the sandbox Docker image. Results are saved to `strix_runs/<run-name>`

---

## 🤖 Use From Your Coding Agent

Give Claude Code, Cursor, Codex, or any [SKILL.md-compatible](https://agentskills.io) agent the ability to run pentests, fix findings, and set up CI scanning:

```bash
npx skills add usestrix/strix
```

This installs nine skills: **penetration-testing-with-strix** (run headless scans and read results), **managed-pentesting-with-strix** (drive the managed [app.strix.ai](https://app.strix.ai) platform via REST — no local Docker or LLM key), **fix-security-vulnerabilities-with-strix** (remediate + re-scan to verify), **ci-security-scanning-with-strix** (PR scanning in CI), plus target-specific workflows: **application-security-testing**, **web-app-penetration-testing**, **api-security-testing**, **owasp-top-10-testing**, and **find-security-vulnerabilities-in-code**. Agents can run scans two ways with the same engine — the open-source CLI locally, or the managed cloud when there's no local infra — and read [`AGENTS.md`](AGENTS.md) for a quick reference, [docs.strix.ai/llms.txt](https://docs.strix.ai/llms.txt) for the CLI docs, and [docs.app.strix.ai](https://docs.app.strix.ai) for the API.

---

## ✨ Features

### Agentic Pentesting Tools

The agents come equipped with a comprehensive offensive security toolkit - the same tools used by professional penetration testers and ethical hackers:

- **HTTP Interception Proxy** - Full request/response manipulation and analysis with Caido
- **Browser Exploitation** - Automated browser for testing XSS, CSRF, clickjacking, and auth bypass flows
- **Shell & Command Execution** - Interactive terminal for exploit development and post-exploitation
- **Custom Exploit Runtime** - Python sandbox for writing and validating proof-of-concept exploits
- **Reconnaissance & OSINT** - Automated attack surface mapping, subdomain enumeration, and fingerprinting
- **Static & Dynamic Code Analysis** - SAST + DAST capabilities for comprehensive application security testing
- **Vulnerability Knowledge Base** - Structured findings with CVSS scoring and OWASP classification

### Comprehensive Vulnerability Scanner

Identifies, validates, and exploits a wide range of security vulnerabilities across the OWASP Top 10 and beyond:

- **Broken Access Control** - IDOR, privilege escalation, auth bypass
- **Injection Attacks** - SQL injection, NoSQL injection, OS command injection, SSTI
- **Server-Side Vulnerabilities** - SSRF, XXE, insecure deserialization, RCE
- **Client-Side Attacks** - XSS (stored/reflected/DOM), prototype pollution, CSRF
- **Business Logic Flaws** - Race conditions, payment manipulation, workflow bypass
- **Authentication & Session** - JWT attacks, session fixation, credential stuffing vectors
- **Infrastructure & Cloud** - Misconfigurations, exposed services, cloud security issues
- **API Security** - Broken authentication, mass assignment, rate limiting bypass

### Graph of Agents (Multi-Agent Pentesting)

Advanced multi-agent orchestration for comprehensive automated penetration testing:

- **Distributed Pentesting** - Specialized AI agents for recon, exploitation, and post-exploitation
- **Scalable Security Testing** - Parallel execution across multiple targets for fast, comprehensive coverage
- **Dynamic Coordination** - Agents share discoveries, chain vulnerabilities, and collaborate like a red team

---

## 🖥️ Local Web Viewer

Every scan writes its results to disk as it runs. Bring them up in a local dashboard with a single command:

```bash
# Open the most recent run
strix view

# ...or open a specific run by name
strix view my-run-name

# Expose the viewer on all IPv4 interfaces at a fixed port
strix view --host 0.0.0.0 --port 8080 --no-open
```

`strix view` starts a lightweight local server (bound to `127.0.0.1` on a random port) and opens your browser to a private, tokened link. Nothing leaves your machine: the dashboard reads the run's files straight off disk, with no cloud account or upload required. The UI ships prebuilt, so there is no extra install and no JS build step.

Use `--host 0.0.0.0` to make the viewer reachable from other machines. Replace `0.0.0.0` in the printed URL with the server's reachable IP or hostname. The token in that URL grants access to the selected run's scan data, history, and steering, so only share it with trusted users and restrict the port with your firewall. Requests without the token-derived session cannot read run data.

### What's in the dashboard

- **Overview**: run status, target, and a severity breakdown of everything found so far.
- **Vulnerabilities**: each validated finding with its severity, details, and reproduction steps.
- **Agent graph**: a live map of the multi-agent team, showing which agent is doing what.
- **Steering**: send instructions to a live scan from the browser to redirect the agents mid-run.
- **History**: browse past runs on this machine and jump between them.
- **Reports**: generate a shareable report and email it to yourself or your team.

---

## Usage Examples

### Basic Usage

```bash
# Scan a local codebase
strix --target ./app-directory

# Security review of a GitHub repository
strix --target https://github.com/org/repo

# Black-box web application assessment
strix --target https://your-app.com
```

### API Testing (OpenAPI / Swagger / Postman)

Point the agent at an API contract and it tests every declared endpoint instead of
having to discover them by crawling. Pair the spec with the live base URL so the
agent knows where to send traffic:

```bash
# OpenAPI / Swagger file (.json / .yaml)
strix --target ./openapi.yaml --target https://api.your-app.com

# Postman collection export
strix --target ./collection.postman_collection.json --target https://api.your-app.com

# Postman collection pulled live by id (no manual export)
export POSTMAN_API_KEY="PMAK-..."
strix --target postman://<collection-uuid>

# ...with a Postman environment to resolve {{baseUrl}} / token variables
strix --target "postman://<collection-uuid>?env=<environment-uuid>"
```


### Advanced Testing Scenarios

```bash
# Grey-box authenticated testing
strix --target https://your-app.com --instruction "Perform authenticated testing using credentials: user:pass"

# Multi-target testing (source code + deployed app)
strix -t https://github.com/org/app -t https://your-app.com

# Targets from a file, one target per non-empty, non-comment line
strix --target-list ./targets.txt

# White-box source-aware scan (local repository)
strix --target ./app-directory --scan-mode standard

# Focused testing with custom instructions
strix --target api.your-app.com --instruction "Focus on business logic flaws and IDOR vulnerabilities"

# Provide detailed instructions through file (e.g., rules of engagement, scope, exclusions)
strix --target api.your-app.com --instruction-file ./instruction.md

# Force PR diff-scope against a specific base branch
strix -n --target ./ --scan-mode quick --scope-mode diff --diff-base origin/main
```

### Headless Mode

Run programmatically without interactive UI using the `-n/--non-interactive` flag - perfect for servers and automated jobs. The CLI prints real-time vulnerability findings and the final report before exiting. Exits with non-zero code when vulnerabilities are found.

```bash
strix -n --target https://your-app.com
```

### CI/CD (GitHub Actions)

Add a security test on pull requests with a lightweight GitHub Actions workflow:

```yaml
name: inovar-strix-penetration-test

on:
  pull_request:

jobs:
  security-scan:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
        with:
          fetch-depth: 0

      - name: Install the CLI
        run: curl -sSL https://strix.ai/install | bash

      - name: Run the scan
        env:
          STRIX_LLM: ${{ secrets.STRIX_LLM }}
          LLM_API_KEY: ${{ secrets.LLM_API_KEY }}

        run: strix -n -t ./ --scan-mode quick
```

> [!TIP]
> In CI pull request runs, quick reviews are automatically scoped to changed files.
> If diff-scope cannot resolve, ensure checkout uses full history (`fetch-depth: 0`) or pass
> `--diff-base` explicitly.

### Configuration

```bash
export STRIX_LLM="openai/gpt-5.4"
export LLM_API_KEY="your-api-key"

# Optional
export LLM_API_BASE="your-api-base-url"  # if using a local model, e.g. Ollama, LMStudio
export PERPLEXITY_API_KEY="your-api-key"  # for search capabilities
export STRIX_REASONING_EFFORT="high"  # control thinking effort (default: high, quick scan: medium)
```

> [!NOTE]
> Configuration is saved automatically to `~/.strix/cli-config.json`, so you don't have to re-enter it on every run.

#### Sign in with a ChatGPT subscription

Instead of a metered API key, you can run a scan on your ChatGPT Plus/Pro subscription:

```bash
strix auth login chatgpt      # sign in with your ChatGPT account

export STRIX_LLM="chatgpt/gpt-5.4"   # chatgpt/<model> runs on the subscription
strix --target ./app-directory

strix auth status             # show the active sign-in
strix auth logout             # forget the sign-in
```

#### Run on your Claude Code subscription

New in this build: instead of routing through the usual OpenAI-Agents-SDK/litellm model layer, an agent's turn-by-turn reasoning and tool-calling can be driven by the real [Claude Code](https://claude.com/product/claude-code) CLI, reusing whatever Claude subscription (Pro/Max/Team/Enterprise) you're already logged into. Every tool call against the scan target still runs through the same sandboxed tool implementations as the default engine — only the reasoning loop changes.

```bash
claude /login                       # sign in once, if you haven't already

export STRIX_LLM="claude-code/sonnet"   # claude-code/<model> drives the agent via the claude CLI
strix --target ./app-directory
```

Requirements: the `claude` CLI installed and on `PATH`, and logged in via `claude /login`. This is opt-in per run and does not change the default engine's behavior. See [`docs/superpowers/specs/2026-08-30-claude-code-engine-design.md`](docs/superpowers/specs/2026-08-30-claude-code-engine-design.md) for the full design.

#### Connect your own MCP servers

You can connect to Model Context Protocol (MCP) servers you list and expose their tools to the agent during a run. Create `~/.strix/mcp-servers.json` with a JSON list of servers. Each entry is either a local `stdio` server launched as a subprocess, or a remote `http` server:

```json
[
  {
    "name": "local_fs",
    "transport": "stdio",
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/project"]
  },
  {
    "name": "github",
    "transport": "http",
    "url": "https://api.githubcopilot.com/mcp/",
    "auth": { "kind": "bearer", "token": "your-token" },
    "allowed_tools": ["list_issues"]
  }
]
```

Each server's tools are namespaced by `name` (for example `local_fs_read_file`). Omit `allowed_tools` to expose every tool the server offers, or set it to a list to restrict which tools the agent can call. The file is optional, and a server that fails to connect is skipped without failing the run. You can point at a different file with `STRIX_MCP_CONFIG`.

**Recommended models for best results:**

- [OpenAI GPT-5.4](https://openai.com/api/) - `openai/gpt-5.4`
- [Anthropic Claude Sonnet 4.6](https://claude.com/platform/api) - `anthropic/claude-sonnet-4-6`
- [Google Gemini 3 Pro Preview](https://cloud.google.com/vertex-ai) - `vertex_ai/gemini-3-pro-preview`

See the [LLM Providers documentation](https://docs.strix.ai/llm-providers/overview) for all supported providers including Vertex AI, Bedrock, Azure, and local models.

## Documentation

Since inovar-strix is a build on top of the open-source engine, the upstream documentation at **[docs.strix.ai](https://docs.strix.ai)** covers usage, CI/CD integrations, skills, and advanced configuration for everything except the Claude Code engine addition documented above.

## Acknowledgements

inovar-strix, built by Inovar +AZ, is powered by [**Strix**](https://strix.ai), the open-source autonomous AI penetration testing framework - all credit for the core agent engine, sandboxed toolset, and multi-agent orchestration goes to the Strix team and its contributors. Strix itself builds on the work of open-source projects like [LiteLLM](https://github.com/BerriAI/litellm), [Caido](https://github.com/caido/caido), [Nuclei](https://github.com/projectdiscovery/nuclei), [Playwright](https://github.com/microsoft/playwright), and [Bubble Tea](https://github.com/charmbracelet/bubbletea).

> [!WARNING]
> **Authorized use only.** This tool actively tests the targets you point it at, so only run it against systems you own or have **explicit, written permission** to test, and stay within the agreed scope. Unauthorized testing is illegal in most jurisdictions.
> You alone are responsible for obtaining authorization and complying with the law. Provided "as is" with no warranty or liability for misuse.
