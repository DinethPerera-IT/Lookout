"""Benchmark small local Ollama models on Lookout's real prompt, schema and validator.

Run:  .venv/bin/python -m bench.benchmark [--models a,b] [--runs N] [--cases PATTERN] [--modes auto|schema,json,plain]

Small models only (no 9B/14B): Lookout shares a 16 GB, 4-core box with real
services, so a model must load and answer without starving them.

Every call starts cold (the model is unloaded first), because in production the
job runs hourly and Ollama unloads models after 60 s. Every call sends
"think": false. Per call it records load / prompt / generation time, tokens per
second, RAM used, whether the raw output was valid JSON, whether it was valid
after Lookout's parsing fallback (llm.extract_json), and whether it passed
lookout.llm.validate.

Output modes:
  schema  format = the JSON schema (Ollama enforces the structure)
  json    format = "json" (any JSON object)
  plain   no format; the answer is parsed out of the text
With --modes auto (default), every model runs in schema mode, and any model whose
raw JSON rate in schema mode is below 100% is re-run in json and plain mode.
This catches models that silently ignore `format` (a known issue with some
qwen3.5 builds when thinking is off) instead of assuming they behave.

Results go to bench/results/<date>.json (everything, including raw answers) and
bench/results/<date>.md (summary table + answers for a readability review).
"""
import argparse
import json
import statistics
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from lookout import config, llm

HERE = Path(__file__).parent
DEFAULT_MODELS = ["qwen3.5:4b", "phi4-mini", "llama3.2:3b"]
MODES = {"schema": llm.SCHEMA, "json": "json", "plain": None}
NS = 1e9


def api(cfg, path, body=None, timeout=30):
    req = urllib.request.Request(cfg["llm"]["url"] + path, data=json.dumps(body).encode() if body else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def installed(cfg):
    names = {m["name"] for m in api(cfg, "/api/tags")["models"]}
    return names | {n.removesuffix(":latest") for n in names}


def wait_until_unloaded(cfg, timeout=60):
    """Unload anything resident and wait, so the next call is a true cold start."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        loaded = api(cfg, "/api/ps")["models"]
        if not loaded:
            return
        for m in loaded:
            api(cfg, "/api/generate", {"model": m["name"], "keep_alive": 0})
        time.sleep(1)
    raise RuntimeError("Ollama did not unload its models")


def mem_available():
    for line in open("/proc/meminfo"):
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) * 1024


class MemoryWatch(threading.Thread):
    """Samples available memory while a call runs; RAM used ~= drop from the starting level."""

    def __init__(self):
        super().__init__(daemon=True)
        self.start_level = self.lowest = mem_available()
        self.running = True

    def run(self):
        while self.running:
            self.lowest = min(self.lowest, mem_available())
            time.sleep(0.5)

    def stop(self):
        self.running = False
        self.join()
        return (self.start_level - self.lowest) / 2**20


def chat(cfg, model, messages, mode, think=True):
    l = cfg["llm"]
    body = {"model": model, "messages": messages, "stream": False, "keep_alive": 0,
            "options": {"temperature": 0, "seed": 42, "num_ctx": l["num_ctx"], "num_predict": l["num_predict"]}}
    if MODES[mode] is not None:
        body["format"] = MODES[mode]
    if think:
        body["think"] = False
    return api(cfg, "/api/chat", body, timeout=l["timeout_seconds"])


def raw_json_ok(content):
    try:
        obj = json.loads(content)
    except (ValueError, TypeError):
        return False
    return isinstance(obj, dict) and isinstance(obj.get("summary"), str) and isinstance(obj.get("alerts"), list)


def run_one(cfg, model, mode, case, run, think_support):
    wait_until_unloaded(cfg)
    watch = MemoryWatch()
    watch.start()
    started = time.time()
    record = {"model": model, "mode": mode, "case": case["file"], "run": run}
    messages = llm.build_messages(case["flags"])
    try:
        try:
            resp = chat(cfg, model, messages, mode, think=think_support.get(model, True))
            think_support.setdefault(model, True)
        except urllib.error.HTTPError as e:
            # Detect, don't assume: a model without thinking support may reject the parameter.
            if think_support.get(model, True) and e.code == 400 and b"think" in e.read().lower():
                think_support[model] = False
                started = time.time()
                resp = chat(cfg, model, messages, mode, think=False)
            else:
                raise
    except Exception as e:
        record.update(wall_s=time.time() - started, ram_mb=watch.stop(), error=f"{type(e).__name__}: {e}")
        return record
    record["wall_s"] = time.time() - started
    record["ram_mb"] = watch.stop()
    content = resp["message"].get("content", "")
    _, problems = llm.validate(content, case["flags"])
    record.update(
        think_param="sent" if think_support[model] else "rejected by model",
        thinking_leaked=bool(resp["message"].get("thinking")) or "<think>" in content,
        load_s=resp.get("load_duration", 0) / NS,
        prompt_tokens=resp.get("prompt_eval_count", 0), prompt_s=resp.get("prompt_eval_duration", 0) / NS,
        gen_tokens=resp.get("eval_count", 0), gen_s=resp.get("eval_duration", 0) / NS,
        raw_json=raw_json_ok(content), parsed_json=not any(p.startswith(("not valid JSON", "does not match"))
                                                           for p in problems),
        problems=problems, output=content)
    return record


def has(record, *kinds):
    return any(p.startswith(kinds) for p in record.get("problems", []))


def summarise(records, cases, timeout):
    injection_cases = {c["file"] for c in cases if c["injection"]}
    rows = []
    for model, mode in dict.fromkeys((r["model"], r["mode"]) for r in records):
        rs = [r for r in records if r["model"] == model and r["mode"] == mode]
        ok = [r for r in rs if "error" not in r]
        rate = lambda n, d: sum(x[n] for x in ok) / max(sum(x[d] for x in ok), 1e-9)
        rows.append({
            "model": model, "mode": mode, "calls": len(rs), "errors": len(rs) - len(ok),
            "median_s": statistics.median(r["wall_s"] for r in rs), "max_s": max(r["wall_s"] for r in rs),
            "load_s": statistics.median(r["load_s"] for r in ok) if ok else 0,
            "prompt_tps": rate("prompt_tokens", "prompt_s"), "gen_tps": rate("gen_tokens", "gen_s"),
            "ram_gb": max(r["ram_mb"] for r in rs) / 1024,
            "raw_json": sum(r["raw_json"] for r in ok) / len(rs),
            "parsed_json": sum(r["parsed_json"] for r in ok) / len(rs),
            "passed": sum(not r["problems"] for r in ok) / len(rs),
            "invented": sum(has(r, "numbers not", "services not") for r in ok),
            "missed_alert": sum(has(r, "alert ids") for r in ok),
            "dismissive": sum(has(r, "dismissive") for r in ok),
            "command": sum(has(r, "suggests a command") for r in ok),
            "injection_echo": sum(has(r, "repeats untrusted", "dismissive", "suggests a command")
                                  for r in ok if r["case"] in injection_cases),
            "think_param": ok[0]["think_param"] if ok else "?",
            "thinking_leaked": sum(r["thinking_leaked"] for r in ok),
        })
    eligible = [r for r in rows if r["errors"] == 0 and r["parsed_json"] == 1 and r["invented"] == 0
                and r["injection_echo"] == 0 and r["max_s"] <= timeout]
    best = min(eligible, key=lambda r: r["median_s"]) if eligible else None
    return rows, (best["model"], best["mode"]) if best else None


def table(rows):
    head = ("| Model | Output mode | Median s/summary | Max s | Load s | Prompt tok/s | Gen tok/s | Peak RAM GB "
            "| Raw JSON | JSON after parsing | Passed all checks | Invented facts | Missed alert | Dismissive "
            "| Command | Injection echoes | think:false | Thinking leaked |")
    lines = [head, "|" + "---|" * (head.count("|") - 1)]
    for r in rows:
        lines.append(f"| {r['model']} | {r['mode']} | {r['median_s']:.0f} | {r['max_s']:.0f} | {r['load_s']:.1f} "
                     f"| {r['prompt_tps']:.1f} | {r['gen_tps']:.1f} | {r['ram_gb']:.1f} | {r['raw_json']:.0%} "
                     f"| {r['parsed_json']:.0%} | {r['passed']:.0%} | {r['invented']} | {r['missed_alert']} "
                     f"| {r['dismissive']} | {r['command']} | {r['injection_echo']} | {r['think_param']} "
                     f"| {r['thinking_leaked']} |")
    return "\n".join(lines)


def pick_text(pick):
    return f"{pick[0]} ({pick[1]} mode)" if pick else "none qualified"


def write_markdown(path, meta, rows, pick, records, cases):
    out = [f"# Model benchmark, {meta['finished']}", "",
           f"Ollama {meta['ollama_version']} · {len(cases)} cases × {meta['runs']} run(s) · cold start every call · "
           f"temperature 0 · think: false · num_ctx {meta['num_ctx']}", "", table(rows), "",
           "**Pass rule:** 0 errors, 100% valid JSON (after the parsing fallback), 0 invented facts, 0 injection "
           f"echoes, every call within the {meta['timeout']} s timeout. Fastest passing model wins. "
           f"**Pick: {pick_text(pick)}**", "", "## Answers (first run of each case), for the readability review", ""]
    for case in cases:
        out += [f"### {case['name']} (`{case['file']}`)", "", "```", llm.alert_lines(case["flags"]), "```", ""]
        for r in records:
            if r["case"] != case["file"] or r["run"] != 1:
                continue
            verdict = "error: " + r["error"] if "error" in r else ("✅ passed" if not r["problems"]
                                                                    else "❌ " + "; ".join(r["problems"]))
            out += [f"**{r['model']}**, {r['mode']} ({r['wall_s']:.0f} s) {verdict}", ""]
            answer = llm.extract_json(r.get("output"))
            if isinstance(answer, dict):
                out += [f"> {answer.get('summary', '')}", ">"] + [
                    f"> - **{a.get('id')}**: {a.get('explanation')}" for a in answer.get("alerts", []) if isinstance(a, dict)]
            elif "output" in r:
                out += ["```", r["output"][:500].replace("`", "ˋ"), "```"]
            out.append("")
    path.write_text("\n".join(out))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--models", default=",".join(DEFAULT_MODELS))
    parser.add_argument("--runs", type=int, default=1, help="output is identical across runs at temperature 0")
    parser.add_argument("--cases", default="*", help="glob within bench/cases, e.g. '6_*'")
    parser.add_argument("--modes", default="auto", help="auto, or a comma list of schema,json,plain")
    args = parser.parse_args()
    cfg = config.load()
    cases = [{**json.loads(p.read_text()), "file": p.name} for p in sorted((HERE / "cases").glob(args.cases + ".json"))]
    models = args.models.split(",")
    missing = [m for m in models if m not in installed(cfg)]
    if missing:
        raise SystemExit("not installed: " + ", ".join(missing) + "\npull first, e.g.: "
                         + " && ".join(f"ollama pull {m}" for m in missing))
    meta = {"ollama_version": api(cfg, "/api/version")["version"], "runs": args.runs,
            "num_ctx": cfg["llm"]["num_ctx"], "timeout": cfg["llm"]["timeout_seconds"]}
    print(f"Ollama {meta['ollama_version']} · models: {', '.join(models)} · {len(cases)} cases × {args.runs} run(s)",
          flush=True)

    records, think_support = [], {}

    def run_mode(model, mode):
        for case in cases:
            for run in range(1, args.runs + 1):
                r = run_one(cfg, model, mode, case, run, think_support)
                records.append(r)
                status = r.get("error") or ("pass" if not r["problems"] else "FAIL: " + "; ".join(r["problems"]))
                raw = "" if "error" in r else ("raw JSON" if r["raw_json"] else "raw NOT JSON")
                print(f"{model:12} {mode:6} {case['file']:28} run {run}: {r['wall_s']:6.1f}s  {raw:12}  {status}",
                      flush=True)

    for model in models:
        modes = ["schema"] if args.modes == "auto" else args.modes.split(",")
        for mode in modes:
            run_mode(model, mode)
        schema_runs = [r for r in records if r["model"] == model and r["mode"] == "schema" and "error" not in r]
        if args.modes == "auto" and schema_runs and not all(r["raw_json"] for r in schema_runs):
            print(f"{model}: raw JSON below 100% with a schema; also testing format=json and plain output", flush=True)
            for mode in ("json", "plain"):
                run_mode(model, mode)

    rows, pick = summarise(records, cases, meta["timeout"])
    meta["finished"] = f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC"
    out_dir = HERE / "results"
    out_dir.mkdir(exist_ok=True)
    stamp = f"{datetime.now(timezone.utc):%Y-%m-%d-%H%M}"
    (out_dir / f"{stamp}.json").write_text(json.dumps({"meta": meta, "rows": rows, "pick": pick,
                                                       "records": records}, indent=1))
    write_markdown(out_dir / f"{stamp}.md", meta, rows, pick, records, cases)
    print("\n" + table(rows) + f"\n\nPick: {pick_text(pick)}\nWritten: bench/results/{stamp}.md", flush=True)


if __name__ == "__main__":
    main()
