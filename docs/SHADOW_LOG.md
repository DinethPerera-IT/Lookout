# Shadow-mode log

Two weeks of `--shadow` before going live (see [ROADMAP.md](../ROADMAP.md), exit criteria).
One row per shadow report (journal: `journalctl -u lookout -g "shadow report"`), plus any
incident Lookout missed. Verdict: **TP** (true positive, worth a message), **FP** (false
positive), **MISS** (something happened that Lookout didn't report).

Started: _(date the timer was enabled)_

| Date / time | Flag key(s) | Verdict | Why / what was going on | AI section ok? | Action (threshold change, noise pattern…) |
|---|---|---|---|---|---|
| | | | | | |

## End-of-shadow review
- Reports per day (avoidable ones): 
- Missed incidents (Kuma alerts or noticed by me, not in Lookout): 
- AI sections: accepted / rejected by validator / wrong but accepted: 
- Dead-man's switch tested (timer stopped → Kuma alerted): 
- Decision: go live / extend shadow
