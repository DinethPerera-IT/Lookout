# Lookout roadmap

> Planning document. Only v1 is being built. Nothing in v2 or v3 has been started.

## Core principle

> **The AI can raise its hand, but it can't lower anyone else's.**

Deterministic alerts are authoritative: Uptime Kuma's monitors and Lookout's own rules. AI output can **add** context or new observations. It can never **suppress, downgrade, delay or replace** a deterministic alert.

How v1 already enforces this, and how every later feature must keep enforcing it:

- **The send decision is made before the model runs.** If code flagged something, a report goes out whether or not the LLM is available, fast or sane.
- **Facts are rendered by code.** The model's text is appended *below* the facts block and can't edit, reorder or remove it.
- **The model has no tools and no write access.** At worst it produces wrong prose.
- **A validator drops the AI section** if it skips a flag, invents a number, or uses dismissive language ("nothing to worry about", "can be ignored").
- **Kuma watches Lookout** (push heartbeat), so Lookout is never the last line of defence.

Every item below has a **principle check**: how it could violate the rule, and what stops it.

---

## v1: the current build

| Phase | What | Status |
|---|---|---|
| 1 | Collectors: journal, systemd, host, Prometheus | ✅ |
| 2 | Redaction of secrets, MACs, SSIDs | ✅ |
| 3 | Detection rules + SQLite state (baselines, cooldown, resolved) | ✅ |
| 4 | Docker via read-only proxy | ✅ |
| 5 | Discord report + Uptime Kuma push heartbeat | ✅ |
| 6 | Model benchmark (time per summary, "did it invent facts?") | planned |
| 7 | LLM explanation layer with validation and fallback | planned |
| 8 | systemd: `lookout.service` + `lookout.timer`, dedicated user `lookout`, state in `/var/lib/lookout`, sandboxing | planned |

### Then: 2 weeks of shadow mode before going live

`--dry-run` deliberately throws away all state, so it can't show how cooldowns and baselines behave over days. Phase 8 therefore adds a **shadow mode**: the full pipeline, with state committed and the Kuma heartbeat sent, but reports go only to the journal and a private test channel, never the real one.

**Exit criteria for going live** (reviewed together at the end of the two weeks):
- Every shadow report was logged as a true or false positive, with a reason.
- Noise: at most ~1 avoidable report per day on average. Thresholds are tuned in `config.toml`, with the reason noted next to each change.
- No missed incidents: anything Kuma alerted on, or that I noticed myself, also appears in Lookout's facts (or is explained).
- The LLM layer met its validation targets on real reports: no invented facts, and the facts-only fallback worked when the model failed.
- The dead-man's switch was tested: stopping the timer made Kuma alert.

---

## v2: smarter detection and feedback

Every v2 item stays deterministic where it decides anything. AI is only used where noted, and never in a way that can lower an alert.

### 1. Statistical anomaly detection on metrics

**What:** Baselines that know time matters. For example: "load is 3.5, but at 03:00 it's normally 0.4". Hour-of-day (and weekday) baselines from Prometheus history, using median and MAD per hour slot. Growth rates too: disk or memory rising faster than usual (`deriv`, `predict_linear`), a slow memory leak in a container.

**How we'll test it:**
- Recorded Prometheus responses with synthetic anomalies injected (a 3 a.m. load spike, a steady memory climb), run as scenario fixtures with expected flags.
- **Backtest** over the full 15 days of Prometheus retention: count how many flags it *would* have raised. Target: no more than a handful per week on a healthy system.
- Unit tests for slot bucketing, including the NZ daylight-saving changeovers.

**What could go wrong:**
- 15-day retention gives only ~2 samples per weekday-hour slot, which is far too few. Start with hour-of-day only (15 samples per slot), require minimum history, and fall back to v1's fixed thresholds until then.
- Daylight saving shifts every hour slot by one twice a year, which could cause false alarms. Bucket in local time and test both transitions.
- One unusual week (exams, a migration) becomes "normal". MAD resists single bursts but not sustained change, so cap how fast a baseline can move.
- Heavy PromQL on a 4-core box. Use one query per metric per run, and measure the cost.
- Alert fatigue. These flags are **warn-only** and go through the same cooldowns.

**Principle check:** Fully deterministic. It only *adds* flags and never changes or suppresses existing rules.

### 2. Incident grouping

**What:** Related flags that arrive together become one incident message. Example: a reboot gives host-reboot + WiFi re-auth + container restarts + a cloudflared error burst. Grouping is by time window and known relationships (reboot → restarts, WiFi drop → tunnel errors).

**How we'll test it:**
- Scenario fixtures for a reboot storm and a WiFi outage, each expecting exactly one incident that contains every flag.
- **Invariant test:** every flag that would have been reported on its own appears, line by line, in some incident. Grouping may merge messages; it may never drop a flag.

**What could go wrong:**
- An unrelated second problem gets buried inside an incident about something else. Every flag keeps its own line and severity, and the incident takes the highest severity.
- A long window delays the first message. The window is limited to the current run (no waiting for future runs).
- Mixing incident state with per-flag cooldowns causes double reports or gaps. Grouping is presentation only, and per-flag state stays as it is.

**Principle check:** Grouping is code, not AI, and the invariant test ensures nothing disappears.

### 3. "AI observations" lane

**What:** The one place the model may bring up something code didn't flag. It reads a wider redacted sample (warnings, not just errors) and may post an observation, labelled **🤖 Unverified AI observation**, in its own section, at most **3 per day**. Each observation must quote the log lines it's based on.

**How we'll test it:**
- Fixtures with planted subtle issues (recall) and quiet fixtures (it should usually say nothing).
- A validator: every quoted line must exist exactly in the input, or the observation is dropped.
- A rate-limit test: the 4th observation in a day is dropped.
- **Byte-for-byte test:** the deterministic part of a report is identical with the lane on or off.
- Injection fixtures, for example a log line saying "CRITICAL: disk failure, see http://…" (it must not become an observation with a link).

**What could go wrong:**
- Hallucinated observations erode trust in the whole report. They're labelled unverified, rate-limited, and their precision is measured (item 6). The lane gets switched off if precision stays low.
- Prompt injection: log text comes partly from the internet, so an attacker could craft lines to trigger fake alarms or phishing links. Links and mentions are stripped and observations are capped. The worst case is 3 junk lines a day.
- CPU cost: bigger prompts on a CPU-only box. Run it at most once a day, outside the hourly path.
- An observation that reads as reassurance ("the errors are expected"). The dismissive-phrase validator applies here too.

**Principle check:** It can only *add* a separate, labelled section. It has no access to flags, severities or the send decision.

### 4. Read-only suggested next checks

**What:** Under each flag, 1–3 commands to investigate, for example `journalctl -u caddy --since -1h` or `docker logs --tail 50 grafana`. They come from a **fixed allowlist of templates per rule type**, with slots filled from validated flag metadata (unit or container name). The AI may at most choose and order templates from the allowlist. It never writes commands. Lookout never runs them.

**How we'll test it:**
- Every rendered command must match an allowlist pattern (`journalctl`, `systemctl status`, `docker logs`/`ps`, `df`, `free`, `ss`…).
- Fuzz tests with hostile metadata (a container named `x; rm -rf /`) must produce no command.
- If the model outputs anything off-list (`systemctl restart`, `docker prune`), it's dropped. Tested explicitly.

**What could go wrong:**
- A command that looks harmless but isn't. Only status/read verbs go on the allowlist, and adding one requires a reviewed code change.
- Shell metacharacters through names. Slot values must match a strict regex, otherwise the suggestion is skipped.
- Habit: copy-pasting commands without reading them. They're rendered as code blocks with a one-line "what this shows" from the template.

**Principle check:** Suggestions are advisory, read-only and never executed. Flags are unaffected.

### 5. Weekly digest

**What:** A Sunday summary:
- flags by rule
- noisiest sources
- issues still open
- baseline drift
- Lookout's own health: runs completed, missed runs, LLM failure and fallback rate
- AI precision from feedback

The numbers come from code. An optional AI paragraph is labelled as such. A "quiet week" digest is still sent, which doubles as proof the job is alive.

**How we'll test it:**
- Build a state DB from a fixture week with known issues, then check every number.
- A snapshot test of the rendered message.
- A test that a week with zero flags still produces a digest.
- A length test against Discord's limits.

**What could go wrong:**
- It becomes noise nobody reads. Keep it to about 10 lines and drop sections that are empty.
- Misleading numbers after a state reset or a gap. The digest states its coverage ("162/168 runs").
- The AI paragraph sounding reassuring ("a quiet, healthy week") when a deterministic issue is still open. The same dismissive-phrase validator applies, and open issues are always listed first by code.

**Principle check:** Facts first, from code. AI text is optional, labelled, and can't hide an open issue.

### 6. 👍/👎 feedback to measure AI precision

**What:** I react 👍 or 👎 to the AI sections in Discord. Reactions are stored in SQLite per message, section, model and prompt version. That gives a precision score per lane: are the explanations and observations actually useful?

Webhooks can't read reactions. So: posts use `?wait=true` to get the message ID, and a read-only fetch (a bot token with only *View Channel* + *Read Message History*) collects reactions hourly. Only reactions from my own Discord user ID count.

**How we'll test it:**
- Unit tests against recorded Discord API responses.
- Idempotency: re-reading the same reactions never double-counts.
- Reactions from other users are ignored.
- Precision is reported with a confidence interval (Wilson), not a bare percentage.

**What could go wrong:**
- Too little data to mean anything (a few AI sections a week). Show counts and intervals, and don't draw conclusions below a minimum sample.
- Selection bias: I only react when annoyed. Track the reaction rate, and occasionally prompt for review in the digest.
- Another secret (bot token) and more permissions. Least-privilege scopes, stored in `.env`, documented in the threat model.
- **Feedback creeping into detection.** It must never tune thresholds or silence rules automatically, because that would let opinion lower an alert. Feedback only measures the AI.

**Principle check:** Measurement only. Deterministic rules never read the feedback table.

---

## v3: reusable by others

- **Plugin collectors:**
  - A small collector interface (`collect(cfg) -> facts section`, optional `history()` for seeding).
  - Plugins are discovered from a `collectors/` directory.
  - Example plugins: Caddy access logs, Pi-hole, SMART disk health, cAdvisor.
  - Plugins produce facts only. Rules stay in reviewed code.
- **Config-driven:**
  - Everything host-specific lives in `config.toml`: rule enable/disable, per-rule overrides, notifiers.
  - The config is checked against a schema at startup, with clear error messages.
- **Docker image:**
  - Non-root, read-only filesystem.
  - Journal mounted read-only; Docker reached through the read-only proxy.
  - The README documents what you give up compared with the systemd install (journal access and sandboxing differ).
- **Install docs:**
  - Quickstart for systemd and for Docker
  - Threat model
  - Writing a collector
  - Tuning thresholds
  - Uninstalling

**Principle check:** Plugins can add facts and rules can add flags. Nothing a plugin or config does can let AI output lower an alert.

---

## Explicitly out of scope

- AI that runs commands, restarts services or "self-heals" anything.
- AI that tunes thresholds, mutes rules or decides what is "normal".
- Replacing Uptime Kuma.
