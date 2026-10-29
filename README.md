# ops-summary

An hourly, read-only job for my homelab. It collects logs and metrics, uses **deterministic rules** to decide whether anything is unusual, and posts the flagged facts to Discord. A **local** LLM (Ollama) adds a plain-English explanation on top. When nothing is unusual it stays silent.

> Status: **Phase 2 of 8 (redaction) complete.** Nothing is sent anywhere yet.

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

## Usage

```bash
python3 -m ops_summary --dry-run            # collect and print facts as JSON
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
| 3 | Detection rules + SQLite state (baselines, cooldown, resolved) | |
| 4 | Docker via read-only socket proxy | |
| 5 | Discord report + Kuma heartbeat | |
| 6 | Model benchmark (speed + "did it invent facts?") | |
| 7 | LLM explanation layer with validation and fallback | |
| 8 | systemd timer, dedicated user, sandboxing | |
