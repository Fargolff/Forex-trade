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


def preserve_recent_deals_legacy_cursor_semantics() -> None:
    path = ROOT / "src/ops.py"
    text = path.read_text(encoding="utf-8")
    old = '''    deals = broker.history_deals(start, current, symbol=symbol, magic=magic)\n    cursor = (int(after_time_msc), int(after_ticket))\n    return sorted(\n        [deal for deal in deals if (int(deal.time_msc), int(deal.ticket)) > cursor],\n        key=lambda item: (int(item.time_msc), int(item.ticket)),\n    )\n'''
    new = '''    deals = broker.history_deals(start, current, symbol=symbol, magic=magic)\n    if int(after_ticket) > 0:\n        cursor = (int(after_time_msc), int(after_ticket))\n        selected = [deal for deal in deals if (int(deal.time_msc), int(deal.ticket)) > cursor]\n    else:\n        # Backward compatibility: callers that only provide the historical\n        # time_msc cursor expect all deals at that exact millisecond to be\n        # considered already consumed. Phase 17 uses the full tuple whenever\n        # last_deal_ticket is available.\n        selected = [deal for deal in deals if int(deal.time_msc) > int(after_time_msc)]\n    return sorted(selected, key=lambda item: (int(item.time_msc), int(item.ticket)))\n'''
    if new in text:
        return
    if old not in text:
        raise RuntimeError("recent_managed_deals Phase 17 anchor not found")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def update_phase16_state_version_expectation() -> None:
    path = ROOT / "tests/test_phase16_execution.py"
    text = path.read_text(encoding="utf-8")
    old = '    assert loaded.version == 3\n'
    new = '    assert loaded.version == 4\n'
    if new in text:
        return
    if old not in text:
        raise RuntimeError("Phase 16 state version assertion not found")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def main() -> None:
    remove_first_early_halt_block()
    fix_phase17_test_timestamps()
    preserve_recent_deals_legacy_cursor_semantics()
    update_phase16_state_version_expectation()
    print("Phase 17 validation and compatibility fixes applied")


if __name__ == "__main__":
    main()
