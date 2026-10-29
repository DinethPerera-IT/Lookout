# ops-summary

An hourly, read-only job for my homelab. It collects logs and metrics, uses **deterministic rules** to decide whether anything is unusual, and posts the flagged facts to Discord. A **local** LLM (Ollama) adds a plain-English explanation on top. When nothing is unusual it stays silent.

> Status: **Phase 3 of 8 (detection + state) complete.** Nothing is sent anywhere yet.

## What is it?

A second opinion layered on top of my real alerting. [Uptime Kuma](https://github.com/louislam/uptime-kuma) answers "is the service up?". ops-summary answers "is anything *odd*?": a disk filling up, an error burst in one service, a container that restarted, WiFi re-authenticating more than usual. It never replaces Kuma. Kuma also watches this job through a push-monitor heartbeat, so if ops-summary itself dies, I still get alerted.

## How does it work?

```
systemd timer (hourly)
  1. COLLECT   journal · systemctl --failed · Prometheus API · /proc   (read-only)
  2. REDACT    strip tokens, passwords, MACs, SSIDs before anything leaves memory
  3. DETECT    thresholds + baselines + "never seen before" errors     (code decides)
  4. EXPLAIN   local Ollama model explains what code flagged            (optional)
  5. REPORT    Discord: raw facts always, AI explanation below them
  6. HEARTBEAT ping Uptime Kuma so it knows the job ran
```

Core rules:
- **Code decides, the LLM explains.** Thresholds are testable and repeatable. An LLM is probabilistic. The model never decides whether something is normal, never chooses what to report, and never assigns severity.
- **Facts always ship.** The report's facts section is rendered by code. If the model times out or returns rubbish, the report goes out with facts only.
- **Logs are untrusted.** Log lines can contain text from the internet. They are only counted, templated and quoted, never acted on.
- **Read-only.** The job never restarts or changes anything.

### Phase 1: collectors

Each collector returns `{"ok": true, ...}` or `{"ok": false, "error": "..."}`. A broken source doesn't stop the others, and its failure is recorded as a fact. That matters: "Prometheus is down" is itself something worth reporting (a monitoring blind spot).

| Collector | Source | Collects |
|---|---|---|
| `host` | `/proc/meminfo`, `shutil.disk_usage`, `os.getloadavg` | point-in-time disk, memory, swap, load. Works when Prometheus is down. |
| `prometheus` | Prometheus HTTP API (node-exporter) | targets up/down, disk % and a 7-day fill prediction (`predict_linear`), **lowest** memory and **highest** load over the last hour |
| `systemd` | `systemctl --failed --output=json` | failed units |
| `journal` | `journalctl -o json` | per-unit entry and error (priority ≤ 3) counts, WiFi re-authentications in 24h |

**Why Prometheus *and* /proc?** A snapshot at 11:00 misses a memory spike at 10:20. `min_over_time`/`max_over_time` over the window doesn't. `/proc` is the fallback.

### Phase 2: redaction

**What is it?** Before any log line is stored, printed, sent to the local LLM, or posted to Discord, `ops_summary/redact.py` replaces secrets and identifying data with placeholders:

```
login password=hunter2                    →  login password=<redacted>
Authorization: Bearer abc123…             →  Authorization: <redacted>
postgres://app:pa55@db/hr                 →  postgres://<redacted>@db/hr
Trying to associate with aa:bb:… (SSID='…') → Trying to associate with <mac> (SSID=<ssid>)
```

**How does it work?** An ordered list of regular expressions, most specific first:

1. Known formats: private keys, Discord webhooks, Kuma push tokens, credentials in URLs, auth headers, bearer tokens, JWTs, GitHub/Slack/AWS/Tailscale tokens
2. `key=value` pairs where the key sounds secret (`password`, `token`, `api_key`, `secret`…). The key name and quoting are kept, so lines stay readable and JSON stays valid.
3. Personal data: email addresses, MAC addresses, WiFi network names (an SSID can contain a home address)
4. Catch-alls for unknown token formats: long runs mixing letters and digits, and base64 blobs

**Design choices**
- **Redact once, at the entry point.** The journal collector redacts every message as it's parsed, so no later stage ever holds raw log text. "Local" doesn't mean "private": output ends up in Discord, the systemd journal and GitHub fixtures.
- **Err on the side of redacting.** A false positive (a container ID becomes `<token>`) costs nothing. A false negative leaks a secret. In 14 days of real logs, 1,261 of the 1,263 catch-all hits were hex IDs and hashes, which is acceptable noise.
- **IP addresses are kept.** "Failed SSH login from 203.0.113.7" is useful context and not a secret.
- **The tests contain no realistic secrets.** Fake tokens are assembled from fragments at runtime, so GitHub secret scanning doesn't flag the repo.

**Proof on real logs:** `tests/scan_real_logs.py` runs the redactor over real journal and container logs and prints **counts only**. A second, independent set of "does this still look like a secret?" patterns (`find_leaks`) checks the result.

| Window | Lines scanned | Possible leaks before | after |
|---|---|---|---|
| 24 h | ~2,800 | 0 | 0 |
| 14 days | ~55,000 | 1 | 0 |

### Phase 3: detection and state

**What is it?** The part that decides. `ops_summary/detect.py` holds plain, deterministic rules. Each one reads the collected facts and returns *flags*. `ops_summary/state.py` keeps a small SQLite database, so the job remembers what's normal and what it has already told me.

**The rules** (thresholds in `config.toml`, chosen from 14 days of this server's history):

| Rule | Flags when | Normal here |
|---|---|---|
| Disk | ≥ 85% (warn), ≥ 95% (crit); or the 6 h trend says it fills within 7 days | 32% |
| Memory | lowest available in the hour < 10% (warn) / < 5% (crit); swap > 1 GiB | ~75% available, no swap |
| Load | highest 15-min load in the hour ≥ 6 (warn) / ≥ 8 (crit) on 4 cores | median 1.95, max 4.37 |
| Failed units | any `systemctl --failed` unit (crit) | none |
| WiFi | ≥ 4 re-authentications in 24 h | 0–2 a day |
| Error spike | a unit logs more errors this hour than `max(5, median + 4 × MAD)` of its last 7 days | ≤ 1 error/hour per unit |
| Novel error | an error *template* never seen before | — |
| Blind spot | a collector failed, or a Prometheus target is down | — |

(Docker container rules arrive in phase 4.)

**How does it work?**

1. **Templates.** `templates.py` turns `Failed to connect to 10.0.0.5 after 3021ms` into `Failed to connect to <ip> after <n>ms`, so 50 variants of one message count as one. Units that change name every session (`session-648.scope`) are collapsed too.
2. **Baseline: median + MAD.** For each unit, the job stores errors per hour. The spike threshold is the median plus 4 × MAD (median absolute deviation: the typical distance from the median). Both are *medians*, so one freak burst last Tuesday doesn't make the threshold useless the way a mean and standard deviation would. A floor of 5 stops "1 error instead of 0" from being called a spike.
3. **First run (warm-up).** A brand-new database has no history. Instead of guessing, the first run **seeds** the baseline from the last 7 days of journal errors and learns every existing error template silently, so run 1 doesn't call everything "novel". If less than 7 days of history exists, spike detection uses a conservative fixed threshold (20) and says so in the flag: `baseline warming up: 11/168 h`. Absolute rules (disk, memory, failed units…) work from run 1.
4. **Known noise.** A short, commented list in `config.toml` (currently one entry: apt-daily's network timeout at boot) is counted separately and never triggers spikes or novel errors. On this server, 20 of the 21 errors in the last 7 days were that one line.
5. **No repeated alerts (dedup + cooldown).** Every flag has a stable key (`disk:/`, `error_spike:cloudflared.service`). `decide()` compares this run's flags with what's stored and puts each one in a bucket:

   | Situation | Outcome |
   |---|---|
   | Never seen before | **report** (new) |
   | Comes back after a "resolved" notice was sent | **report** (reappeared), always, even minutes later |
   | Severity went up (warn → crit) | **report** (worse), always, even while flapping |
   | Still active, last reported ≥ 24 h ago | **report** (reminder) |
   | Still active, reported recently | suppressed |
   | Clears | **"resolved" notice**, unless one was already sent for it in the last 6 h. Then it's held back, and sent once 6 h have passed since the previous one if the issue is still clear. |
   | Comes back while its "resolved" notice is held back | suppressed (from the reader's side it never stopped being broken) |

   **Invariant:** the last message about an issue never says "resolved" while it is broken. Flap protection only ever holds back *resolved* notices (at most one per 6 h), never a failure. A test replays every scenario and checks this after every run.

   Spikes and novel errors are *events*: they don't "resolve", they just expire after a 6 h cooldown.
6. **Dry runs change nothing.** SQLite runs with autocommit off. A real run commits at the end. A dry run closes without committing, so every change is rolled back, and if no database exists yet it uses an in-memory one. A dry run can therefore never swallow an alert that a later real run should send.

**Trust boundary.** A flag's `summary` is written by code from numbers and system metadata only (`cloudflared.service: 30 errors in the last 60 min (normal: median 0/h, threshold 5)`). Log text, which can contain anything an internet visitor typed, only ever goes in `evidence`. A test feeds in `IGNORE PREVIOUS INSTRUCTIONS … @everyone` and checks it never reaches a summary.

**Proving the rules: scenario fixtures.** `tests/fixtures/scenarios/*.json` are small stories, run by run, with the expected outcome of each run: a disk filling up, an error burst, a novel error, warm-up, Prometheus down, several failures at once, flapping load, a unit that fails again after recovering, and a quiet hour. pytest checks every expectation. The CLI replays any of them in memory without touching the server:

```
$ python3 -m ops_summary --fixture tests/fixtures/scenarios/disk.json
  +   0 min  disk:/  new       warn
  +  60 min  disk:/  suppressed
  + 120 min  disk:/  worse     crit
  +1600 min  disk:/  reminder  crit
  +1660 min  disk:/  resolved
```

On the live server, a dry run seeds a full 168 h baseline and reports **0 flags** in about 0.3 s.

## Usage

```bash
python3 -m ops_summary --dry-run            # collect + detect, print flags as JSON
python3 -m ops_summary --dry-run --stage collect   # just the collected facts
python3 -m ops_summary --fixture tests/fixtures/scenarios/flapping.json   # replay a scenario
echo 'password=hunter2' | python3 -m ops_summary.redact   # try the redactor
```

No third-party runtime dependencies (stdlib only). Tests need pytest:

```bash
python3 -m venv .venv && .venv/bin/pip install pytest
.venv/bin/pytest -q
.venv/bin/python -m tests.record_prometheus_fixtures   # re-record Prometheus fixtures
.venv/bin/python -m tests.scan_real_logs 24            # redaction check on real logs (counts only)
```



## Secrets

The Discord webhook and Kuma push URLs live in `.env` (gitignored, `chmod 600`, created by hand from `.env.example`). systemd loads it with `EnvironmentFile=`, and the code only reads environment variables. Not used until phase 5.

## Roadmap

| # | Phase | Status |
|---|---|---|
| 1 | Skeleton + collectors (journal, systemd, host, Prometheus) | ✅ |
| 2 | Redaction of secrets/MACs/SSIDs | ✅ |
| 3 | Detection rules + SQLite state (baselines, cooldown, resolved) | ✅ |
| 4 | Docker via read-only socket proxy | |
| 5 | Discord report + Kuma heartbeat | |
| 6 | Model benchmark (speed + "did it invent facts?") | |
| 7 | LLM explanation layer with validation and fallback | |
| 8 | systemd timer, dedicated user, sandboxing | |
