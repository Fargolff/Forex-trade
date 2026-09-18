from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def remove_first_early_halt_block() -> None:
    path = ROOT / "src/restart_reconcile.py"
    text = path.read_text(encoding="utf-8")
    block = '''    if state and bool(state.get("halted", False)):\n        incidents.append(\n            ReconcileIncident(\n                "LOCAL_LIVE_STATE_HALTED",\n                "CRITICAL",\n                f"local live state is halted: {state.get('halt_reason') or 'unspecified'}",\n            )\n        )\n\n'''
    count = text.count(block)
    if count >= 2:
        text = text.replace(block, "", 1)
        path.write_text(text, encoding="utf-8")
        return
    if count == 1:
        # Already fixed: only the post-resolution fail-closed guard remains.
        return
    raise RuntimeError("expected LOCAL_LIVE_STATE_HALTED guard not found")


def fix_phase17_test_timestamps() -> None:
    path = ROOT / "tests/test_phase17_pending_intent.py"
    text = path.read_text(encoding="utf-8")
    old = "1_789_718_405_000"
    new = "1_789_722_005_000"
    if old in text:
        text = text.replace(old, new)
        path.write_text(text, encoding="utf-8")
        return
    if new in text:
        return
    raise RuntimeError("Phase 17 timestamp fixture anchor not found")


def main() -> None:
    remove_first_early_halt_block()
    fix_phase17_test_timestamps()
    print("Phase 17 validation fixes applied")


if __name__ == "__main__":
    main()
