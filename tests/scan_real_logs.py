"""Check the redactor against real logs. Prints counts only, never log lines.

Run: .venv/bin/python -m tests.scan_real_logs [hours]
Uses `journalctl` and `docker logs` (read-only) with the caller's permissions.
"""
import subprocess
import sys
from collections import Counter

from ops_summary.redact import find_leaks, redact_counts


def read(cmd):
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=120)
    return proc.stdout + proc.stderr  # docker logs writes container stderr to our stderr


def sources(hours):
    yield "journal", read(["journalctl", f"--since=-{hours}h", "-o", "cat", "--no-pager", "-q"])
    names = read(["docker", "ps", "--format", "{{.Names}}"]).split()
    for name in names:
        yield f"docker:{name}", read(["docker", "logs", "--since", f"{hours}h", name])


def main(hours=24):
    total_after = 0
    print(f"{'source':28} {'lines':>6} {'leaks before':>12} {'leaks after':>11}  rules fired")
    for name, text in sources(hours):
        redacted, counts = redact_counts(text)
        after = find_leaks(redacted)
        total_after += after
        rules = ", ".join(f"{rule}={n}" for rule, n in Counter(counts).most_common())
        print(f"{name:28} {len(text.splitlines()):>6} {find_leaks(text):>12} {after:>11}  {rules}")
    print(f"\npossible leaks after redaction: {total_after}")
    return 1 if total_after else 0


if __name__ == "__main__":
    sys.exit(main(int(sys.argv[1]) if len(sys.argv) > 1 else 24))
