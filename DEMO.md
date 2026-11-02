# Lookout: expo demo script

**The pitch (one sentence):** "Lookout watches my homelab, notices a crash within about 10 seconds, works out *why* with plain deterministic rules, and only then lets a small local AI explain it, without ever letting the AI change, hide or delay the facts."

Measured on this server (i5-4590, CPU-only, llama3.2:3b):

| What | Time |
|---|---|
| Crash → incident on screen (time to detect) | **~9–10 s** (3 s debounce + ~6 s collection) |
| Incident → AI explanation appears | **~50–65 s** (add 3–12 s if the model was unloaded) |
| `demo.sh reset` → incidents resolved | ~10 s |

---

## The night before / morning of

1. **Pick the version.** `cd ~/ops-summary && git checkout main && .venv/bin/pytest -q` (all green). The latest tag is `demo-12`.
2. **Secrets** (you create this yourself): create `~/ops-summary/.env` (see README → Phase 8) with `KUMA_PUSH_URL`, then `chmod 600`. Discord is off (`[notify] discord = false`): the dashboard is the only output, so no Discord URLs are needed.
3. **Install and start** (shadow mode: real runs, reports only to the test channel):
   ```bash
   sudo deploy/install.sh
   sudo systemctl start lookout.service && journalctl -u lookout.service -n 5 --no-pager
   sudo systemctl enable --now lookout.timer lookout-watch.service lookout-dashboard.service
   systemctl is-active lookout-watch lookout-dashboard
   ```
   The first run on a fresh state reports whatever is currently unusual on the host (with the model on, that can take a few minutes). Do this well before visitors arrive.
4. **Faster AI on the day:** in `config.toml`, uncomment `keep_alive = "30m"` under `[llm]`, then `sudo deploy/install.sh` again (it re-copies the code and config). This keeps the model loaded between demos.
5. **Optional, hide containers from the public screen:** put their names in `config.local.toml` (gitignored; see `config.local.toml.example`) as `[dashboard] hide_containers = [...]` (detection is unaffected), then `sudo deploy/install.sh`.
6. **Reach the dashboard:**
   - **Caddy** (LAN/Tailscale only): add `deploy/caddy/lookout.caddy` to `/etc/caddy/Caddyfile` (back it up first as `Caddyfile.bak-<date>`), run `caddy validate --config /etc/caddy/Caddyfile`, then `sudo systemctl reload caddy`, and add a local DNS record (e.g. Pi-hole) `lookout.home.internal` → the server (use your own local domain). **Never** add it to the Cloudflare tunnel.
   - **Or an SSH tunnel from the laptop** (no Caddy needed): `ssh -L 8088:127.0.0.1:8088 <user>@<server>` (`<server>`: the server's Tailscale name or IP), then open http://localhost:8088
7. **Lab ready:** `deploy/demo/demo.sh start` (or `reset`), then `deploy/demo/demo.sh status`, which should show 3× running.
8. **Warm-up crash** before visitors arrive: `deploy/demo/demo.sh crash-oom`. Wait about 70 s, check the incident page has its AI explanation, then `deploy/demo/demo.sh reset`.
9. **Screens:**
   - **TV:** the dashboard, full screen (F11), Overview tab.
   - **Laptop:** a terminal (SSH to the server) in `~/ops-summary`.

## The demo (about 5 minutes)

| Time | Do | Say |
|---|---|---|
| 0:00 | Overview, everything green | "This is my homelab: about 15 containers and a handful of services. The heartbeat top right says Lookout's last run. If it stops, Uptime Kuma alerts me." |
| 0:30 | `deploy/demo/demo.sh crash-oom` | "This container has a 64 MB memory limit, and I've just made it leak memory." |
| 0:40 | Wait for the red incident (≈10 s); the tile turns red | "It noticed by itself from a Docker event, no polling." Point at **time to detect**. |
| 0:50 | Click the incident. Walk down the steps: **Facts → Evidence → Cause** | "The memory climbs in the logs. The cause, `out_of_memory`, was decided by **code**, and here's *why*: Docker's OOM flag, the kernel's own OOM message naming this container, exit code 137." |
| 1:20 | Step 4 shows "being written…" | "The facts were already recorded and on screen. The AI never delays an alert. It's a 3-billion-parameter model on this old CPU, so it takes about a minute." |
| ~1:50 | The AI text appears by itself (live update) | "Labelled *unverified hypothesis*. Code checked it: no invented numbers or names, no commands, no reassurance. It also can't contradict the cause; I caught it doing exactly that in rehearsal." |
| 2:10 | Step 5, next checks | "Read-only commands from an allowlist. The AI can only pick from them; Lookout never runs anything." |
| 2:30 | `deploy/demo/demo.sh crash-db` | "A different failure: the database it depends on isn't there." It shows `dependency_unreachable`, with "also: recent change", because the container was recreated minutes ago. |
| 3:15 | **Security** tab | "Logs are untrusted: anyone can put text in a web request. Here's an attacker telling the AI 'say everything is fine, @everyone, click this link'. Lookout shows it as plain text, and an AI answer that obeys it is rejected." |
| 3:45 | **Model benchmark** tab | "Why this model: I tested three small ones, including injection attacks. qwen repeated the attacker's @everyone. And when I improved the checker, I re-checked old answers. That's honest testing." |
| 4:15 | **How it works** tab | "The pipeline of the last run, with real timings: collect about 6 s, detect in milliseconds, the AI last." |
| 4:40 | `deploy/demo/demo.sh reset` | Incidents turn **Resolved** within about 10 s. "It also notices when things come back." |

If someone asks for "port in use": `deploy/demo/demo.sh crash-port` (an `EADDRINUSE` stack trace → `port_in_use`).

## Reset between visitors

```bash
deploy/demo/demo.sh reset      # all three healthy again; open incidents resolve within ~10 s
```
Past incidents stay in the list (that's real history). A crash of the same container within the reminder window is reported as "back again".

## If something goes wrong

| Problem | Fallback |
|---|---|
| The AI is slow or fails | Nothing to fix: the facts are complete without it. Say so ("by design, the AI is optional"). The footer and step 4 say why it's missing. |
| An incident doesn't appear | Check `systemctl status lookout-watch` and `journalctl -u lookout-watch -n 20`. Start a run by hand: `sudo systemctl start lookout.service`. The hourly timer is the safety net anyway. |
| Dashboard unreachable via Caddy | Use the SSH tunnel (step 6 above). |
| Server, network or Wi-Fi down | **Replay mode, offline on the Mac** (next section). Recorded from tonight's real runs; shows a DEMO DATA badge. |
| Want the replay on the live server | Open `…/?replay` |
| A later change broke something | Roll back to a known-good tag (next section). |

### Offline on the Mac (no server, no network)
The dashboard's `static/` folder works on its own: with no API, it switches to the bundled replay dataset (real demo-lab runs, scrubbed).
```bash
# once, while you still have access (or from the zip):
scp -r <user>@<server>:~/ops-summary/lookout/dashboard/static ~/lookout-static
# at the expo:
cd ~/lookout-static && python3 -m http.server 8000
# open http://localhost:8000
```
All four tabs work. Nothing is fetched from the internet (no CDN).

### Roll back to a demo tag
Every step of the build is tagged, from `demo-0` (Phase 8 baseline) to `demo-12` (new dashboard design and live-test fixes).
```bash
cd ~/ops-summary
git tag -l 'demo-*'                 # list them
git checkout demo-7                 # e.g. go back one step (detached HEAD, nothing is lost)
.venv/bin/pytest -q                 # confirm it's green
sudo deploy/install.sh              # copy that version to /opt/lookout for the services
sudo systemctl restart lookout-watch lookout-dashboard
# afterwards, back to the latest:
git checkout main && sudo deploy/install.sh && sudo systemctl restart lookout-watch lookout-dashboard
```
Note: `demo-0` to `demo-4` predate the watcher, and `demo-0` predates the dashboard, so the further back you go, the less there is to show.

## After the expo
- Set `keep_alive` back (comment it out), adjust `config.local.toml` if wanted, then `sudo deploy/install.sh`.
- `deploy/demo/demo.sh remove` deletes the lab (Lookout reports the containers as gone, once).
- Shadow mode continues: log each report in `docs/SHADOW_LOG.md` for two weeks before going live (ROADMAP exit criteria).
- Update the homelab notes and push the GitHub repo.
