from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json

from .watchdog import evaluate_heartbeat


@dataclass(frozen=True)
class SoakConfig:
    cycles: int = 2000
    step_seconds: float = 30.0
    max_heartbeat_age_seconds: float = 120.0
    max_future_seconds: float = 10.0


def run_soak(config: SoakConfig) -> dict:
    if config.cycles < 100:
        raise ValueError("soak cycles must be >= 100")
    if config.step_seconds <= 0:
        raise ValueError("step_seconds must be positive")

    start = datetime(2026, 1, 5, 0, 0, tzinfo=timezone.utc)
    outage_start = max(10, config.cycles // 5)
    outage_length = max(6, int(config.max_heartbeat_age_seconds // config.step_seconds) + 3)
    degraded_start = max(outage_start + outage_length + 10, config.cycles // 2)
    degraded_length = max(5, config.cycles // 50)
    clock_jump_cycle = min(config.cycles - 10, max(degraded_start + degraded_length + 10, (config.cycles * 4) // 5))

    last_heartbeat_time = start
    status_counts = {"OK": 0, "WARN": 0, "CRITICAL": 0}
    outage_detected = False
    recovered_after_outage = False
    degraded_warn_only = True
    clock_jump_detected = False
    healthy_after_clock_jump = False

    for cycle in range(config.cycles):
        now = start + timedelta(seconds=cycle * config.step_seconds)
        in_outage = outage_start <= cycle < outage_start + outage_length
        in_degraded = degraded_start <= cycle < degraded_start + degraded_length
        clock_jump = cycle == clock_jump_cycle

        if not in_outage:
            last_heartbeat_time = now

        heartbeat_time = last_heartbeat_time
        remote_status = "DEGRADED" if in_degraded else "OK"
        if clock_jump:
            heartbeat_time = now + timedelta(seconds=config.max_future_seconds + 5.0)

        heartbeat = {
            "updated_at": heartbeat_time.isoformat(),
            "status": remote_status,
            "incidents": [],
        }
        result = evaluate_heartbeat(
            heartbeat,
            max_age_seconds=config.max_heartbeat_age_seconds,
            max_future_seconds=config.max_future_seconds,
            now=now,
        )
        status = str(result["status"])
        status_counts[status] += 1

        if in_outage and result["code"] == "HEARTBEAT_TIMEOUT":
            outage_detected = True
        if cycle == outage_start + outage_length and status == "OK":
            recovered_after_outage = True
        if in_degraded and status != "WARN":
            degraded_warn_only = False
        if clock_jump and result["code"] == "HEARTBEAT_CLOCK_AHEAD":
            clock_jump_detected = True
        if cycle == clock_jump_cycle + 1 and status == "OK":
            healthy_after_clock_jump = True

    expectations = {
        "outage_detected": outage_detected,
        "recovered_after_outage": recovered_after_outage,
        "degraded_is_warn_not_critical": degraded_warn_only,
        "clock_jump_detected": clock_jump_detected,
        "healthy_after_clock_jump": healthy_after_clock_jump,
    }
    return {
        "ok": all(expectations.values()),
        "cycles": config.cycles,
        "step_seconds": config.step_seconds,
        "status_counts": status_counts,
        "scenario": {
            "outage_start": outage_start,
            "outage_length": outage_length,
            "degraded_start": degraded_start,
            "degraded_length": degraded_length,
            "clock_jump_cycle": clock_jump_cycle,
        },
        "expectations": expectations,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Deterministic operational failure soak harness")
    parser.add_argument("--cycles", type=int, default=2000)
    parser.add_argument("--step-seconds", type=float, default=30.0)
    parser.add_argument("--max-heartbeat-age-seconds", type=float, default=120.0)
    parser.add_argument("--max-future-seconds", type=float, default=10.0)
    args = parser.parse_args()

    result = run_soak(
        SoakConfig(
            cycles=args.cycles,
            step_seconds=args.step_seconds,
            max_heartbeat_age_seconds=args.max_heartbeat_age_seconds,
            max_future_seconds=args.max_future_seconds,
        )
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    raise SystemExit(0 if result["ok"] else 3)


if __name__ == "__main__":
    main()
