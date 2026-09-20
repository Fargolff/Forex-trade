from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any

import yaml
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from .remote_watcher import RemoteWatcherConfig, check_once, load_watcher_config

ATTESTATION_FORMAT = "forex-auto-trader-watcher-attestation"
TRUST_FORMAT = "forex-auto-trader-watcher-attestation-trust"
STATE_FORMAT = "forex-auto-trader-watcher-quorum-state"
PRIVATE_KEY_ENV = "FOREX_WATCHER_ATTESTATION_PRIVATE_KEY"
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class WatcherQuorumConfig:
    observer_id: str
    environment_id: str
    machine_id: str
    quorum_root: str
    expected_observers: tuple[str, ...]
    quorum_size: int
    trust_path: str = "watcher_quorum_trust.yaml"
    state_path: str = "runtime/watcher_quorum_state.json"
    private_key_env: str = PRIVATE_KEY_ENV
    witness_depth: int = 32
    max_attestation_age_seconds: float = 180.0
    max_clock_skew_seconds: float = 60.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _time(value: Any, field: str) -> datetime:
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except Exception as exc:
        raise ValueError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include timezone")
    return parsed.astimezone(timezone.utc)


def _id(value: Any, field: str) -> str:
    text = str(value).strip()
    if not _ID.fullmatch(text):
        raise ValueError(f"invalid {field}: {value!r}")
    return text


def _resolve(root: Path, value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def _inside(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    temp.replace(path)


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _public(path: Path) -> Ed25519PublicKey:
    key = serialization.load_pem_public_key(path.read_bytes())
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("watcher public key must be Ed25519")
    return key


def _private(path: Path) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("watcher private key must be Ed25519")
    return key


def _fingerprint(key: Ed25519PublicKey) -> str:
    der = key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return hashlib.sha256(der).hexdigest()


def _validate(cfg: WatcherQuorumConfig, root: Path) -> None:
    observer = _id(cfg.observer_id, "observer_id")
    machine = _id(cfg.machine_id, "machine_id")
    _id(cfg.environment_id, "environment_id")
    expected = tuple(_id(item, "expected_observer") for item in cfg.expected_observers)
    if len(expected) < 2 or len(set(expected)) != len(expected):
        raise ValueError("expected_observers must contain at least two unique IDs")
    if observer not in expected or observer == machine:
        raise ValueError("observer identity is invalid for this scope")
    if cfg.quorum_size < 2 or cfg.quorum_size > len(expected) or cfg.quorum_size <= len(expected) // 2:
        raise ValueError("quorum_size must be a strict majority")
    if not 2 <= cfg.witness_depth <= 512:
        raise ValueError("witness_depth must be 2..512")
    if cfg.max_attestation_age_seconds <= 0 or cfg.max_clock_skew_seconds < 0:
        raise ValueError("attestation timing thresholds are invalid")
    quorum_root = _resolve(root, cfg.quorum_root)
    if _inside(quorum_root, root):
        raise ValueError("quorum_root must remain outside the project root")
    if _inside(_resolve(root, cfg.state_path), quorum_root):
        raise ValueError("quorum state must remain outside quorum_root")


def load_quorum_config(path: str | Path = "watcher_quorum.yaml", *, root: str | Path = ".") -> WatcherQuorumConfig:
    target = Path(path)
    if not target.exists():
        target = Path("watcher_quorum.example.yaml")
    raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict) or not isinstance(raw.get("expected_observers"), list):
        raise ValueError("invalid watcher quorum config")
    raw = dict(raw)
    raw["expected_observers"] = tuple(str(item) for item in raw["expected_observers"])
    cfg = WatcherQuorumConfig(**raw)
    _validate(cfg, Path(root).resolve())
    return cfg


def _trust(root: Path, cfg: WatcherQuorumConfig) -> list[dict[str, Any]]:
    raw = yaml.safe_load(_resolve(root, cfg.trust_path).read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict) or raw.get("version") != 1 or raw.get("format") != TRUST_FORMAT:
        raise ValueError("watcher trust format is invalid")
    records: list[dict[str, Any]] = []
    for item in raw.get("keys") or []:
        observer = _id(item.get("observer_id"), "trust observer_id")
        key_id = _id(item.get("key_id"), "trust key_id")
        public_path = _resolve(root, str(item.get("public_key", "")))
        public = _public(public_path)
        fp = _fingerprint(public)
        if fp != str(item.get("fingerprint", "")).strip().lower():
            raise ValueError(f"watcher key fingerprint mismatch: {key_id}")
        start = _time(item.get("valid_from"), "valid_from")
        end = _time(item["valid_until"], "valid_until") if item.get("valid_until") else None
        records.append({"observer_id": observer, "key_id": key_id, "public": public_path, "fingerprint": fp,
                        "valid_from": start, "valid_until": end, "revoked": bool(item.get("revoked", False))})
    if not records:
        raise ValueError("watcher trust store is empty")
    return records


def _record(records: list[dict[str, Any]], observer: str, fingerprint: str, moment: datetime) -> dict[str, Any]:
    matches = [r for r in records if r["observer_id"] == observer and r["fingerprint"] == fingerprint]
    if len(matches) != 1:
        raise ValueError("WATCHER_ATTESTATION_KEY_NOT_TRUSTED")
    record = matches[0]
    if record["revoked"]:
        raise ValueError("WATCHER_ATTESTATION_KEY_REVOKED")
    if moment < record["valid_from"] or (record["valid_until"] and moment >= record["valid_until"]):
        raise ValueError("WATCHER_ATTESTATION_KEY_OUTSIDE_VALIDITY")
    return record


def _paths(cfg: WatcherQuorumConfig, root: Path, observer: str) -> tuple[Path, Path]:
    base = _resolve(root, cfg.quorum_root) / cfg.environment_id / cfg.machine_id / "observers" / observer
    return base / "attestation.json", base / "attestation.signature.json"


def _witnesses(watcher_result: dict[str, Any], depth: int) -> list[dict[str, Any]]:
    verification = watcher_result.get("verification") if isinstance(watcher_result.get("verification"), dict) else {}
    scope = Path(str(verification.get("scope", "")))
    if not verification.get("ok") or not scope.is_dir():
        return []
    entries = sorted(item for item in (scope / "entries").iterdir() if item.is_dir() and not item.name.startswith(".tmp-"))
    result: list[dict[str, Any]] = []
    for entry in entries[-depth:]:
        checkpoint = entry / "checkpoint.json"
        if not checkpoint.is_file():
            continue
        doc = _json(checkpoint)
        seq, boot, digest = int(doc.get("sequence", 0)), str(doc.get("boot_id", "")), _sha(checkpoint)
        if seq > 0 and _HEX64.fullmatch(digest):
            result.append({"sequence": seq, "boot_id": boot, "checkpoint_sha256": digest})
    return result


def publish_attestation(cfg: WatcherQuorumConfig, watcher_result: dict[str, Any], *, root: str | Path = ".", now: datetime | None = None) -> dict[str, Any]:
    root = Path(root).resolve()
    _validate(cfg, root)
    moment = (now or _now()).astimezone(timezone.utc)
    if (watcher_result.get("observer_id"), watcher_result.get("environment_id"), watcher_result.get("machine_id")) != (cfg.observer_id, cfg.environment_id, cfg.machine_id):
        raise ValueError("watcher result identity mismatch")
    key_value = os.getenv(cfg.private_key_env, "").strip()
    if not key_value:
        raise ValueError(f"watcher attestation private key is required via {cfg.private_key_env}")
    key_path = Path(key_value).expanduser().resolve()
    if _inside(key_path, root):
        raise ValueError("watcher attestation private key must remain outside the project root")
    private = _private(key_path)
    records = _trust(root, cfg)
    fp = _fingerprint(private.public_key())
    record = _record(records, cfg.observer_id, fp, moment)
    head = watcher_result.get("head") if isinstance(watcher_result.get("head"), dict) else None
    remote_head = None if head is None else {k: head.get(k) for k in ("sequence", "checkpoint_sha256", "boot_id", "stage", "status", "observed_at")}
    document = {"version": 1, "format": ATTESTATION_FORMAT, "attested_at": _iso(moment), "observer_id": cfg.observer_id,
                "environment_id": cfg.environment_id, "machine_id": cfg.machine_id,
                "watcher": {"ok": bool(watcher_result.get("ok")), "status": str(watcher_result.get("status", "UNKNOWN")).upper(),
                            "code": str(watcher_result.get("code", "UNKNOWN")), "detail": str(watcher_result.get("detail", ""))},
                "remote_head": remote_head, "witnesses": _witnesses(watcher_result, cfg.witness_depth)}
    attestation, signature = _paths(cfg, root, cfg.observer_id)
    _atomic_json(attestation, document)
    payload = attestation.read_bytes()
    _atomic_json(signature, {"version": 1, "algorithm": "Ed25519", "document": ATTESTATION_FORMAT,
                             "observer_id": cfg.observer_id, "attestation_sha256": hashlib.sha256(payload).hexdigest(),
                             "key_id": record["key_id"], "public_key_fingerprint": fp, "signed_at": _iso(moment),
                             "signature_b64": base64.b64encode(private.sign(payload)).decode("ascii")})
    verified = verify_attestation(cfg, cfg.observer_id, root=root, now=moment)
    if not verified.get("ok"):
        raise RuntimeError(f"WATCHER_ATTESTATION_POST_VERIFY_FAILED:{verified.get('issues')}")
    return {"ok": True, "code": "WATCHER_ATTESTATION_PUBLISHED", "observer_id": cfg.observer_id,
            "attestation_sha256": verified["attestation_sha256"], "witnesses": len(document["witnesses"])}


def verify_attestation(cfg: WatcherQuorumConfig, observer: str, *, root: str | Path = ".", now: datetime | None = None) -> dict[str, Any]:
    root = Path(root).resolve()
    _validate(cfg, root)
    observer = _id(observer, "observer_id")
    attestation, signature = _paths(cfg, root, observer)
    if not attestation.is_file() or not signature.is_file():
        return {"ok": False, "observer_id": observer, "issues": ["missing"]}
    issues: list[str] = []
    try:
        doc, sig, records = _json(attestation), _json(signature), _trust(root, cfg)
        if doc.get("version") != 1 or doc.get("format") != ATTESTATION_FORMAT:
            issues.append("format")
        if (doc.get("observer_id"), doc.get("environment_id"), doc.get("machine_id")) != (observer, cfg.environment_id, cfg.machine_id):
            issues.append("identity")
        digest = _sha(attestation)
        if sig.get("attestation_sha256") != digest or sig.get("observer_id") != observer:
            issues.append("signature_binding")
        signed_at = _time(sig.get("signed_at"), "signed_at")
        record = _record(records, observer, str(sig.get("public_key_fingerprint", "")).lower(), signed_at)
        if sig.get("key_id") != record["key_id"]:
            issues.append("key_id")
        _public(record["public"]).verify(base64.b64decode(str(sig.get("signature_b64", "")), validate=True), attestation.read_bytes())
        attested_at = _time(doc.get("attested_at"), "attested_at")
        if abs((signed_at - attested_at).total_seconds()) > 60:
            issues.append("signature_time")
        witnesses = doc.get("witnesses")
        if not isinstance(witnesses, list) or len(witnesses) > cfg.witness_depth:
            issues.append("witnesses")
        else:
            seen: set[int] = set()
            for witness in witnesses:
                seq, h = int(witness.get("sequence", 0)), str(witness.get("checkpoint_sha256", ""))
                if seq < 1 or seq in seen or not _HEX64.fullmatch(h):
                    issues.append("witness")
                seen.add(seq)
        return {"ok": not issues, "observer_id": observer, "issues": issues, "document": doc,
                "attestation_sha256": digest, "key_fingerprint": record["fingerprint"], "attested_at": _iso(attested_at)}
    except InvalidSignature:
        return {"ok": False, "observer_id": observer, "issues": ["signature_invalid"]}
    except Exception as exc:
        return {"ok": False, "observer_id": observer, "issues": [f"{type(exc).__name__}:{exc}"]}


def _state(path: Path, cfg: WatcherQuorumConfig) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "format": STATE_FORMAT, "environment_id": cfg.environment_id, "machine_id": cfg.machine_id,
                "last_sequence": 0, "last_checkpoint_sha256": None, "last_boot_id": None, "last_evaluated_at": None}
    value = _json(path)
    if value.get("format") != STATE_FORMAT or value.get("version") != 1 or value.get("environment_id") != cfg.environment_id or value.get("machine_id") != cfg.machine_id:
        raise RuntimeError("WATCHER_QUORUM_STATE_INVALID")
    return value


def evaluate_quorum(cfg: WatcherQuorumConfig, *, root: str | Path = ".", now: datetime | None = None) -> dict[str, Any]:
    root = Path(root).resolve()
    _validate(cfg, root)
    moment = (now or _now()).astimezone(timezone.utc)
    state_path, reports, fresh = _resolve(root, cfg.state_path), {}, []
    state = _state(state_path, cfg)
    fingerprints: dict[str, list[str]] = {}
    for observer in cfg.expected_observers:
        report = verify_attestation(cfg, observer, root=root, now=moment)
        reports[observer] = report
        if not report.get("ok"):
            continue
        age = (moment - _time(report["attested_at"], "attested_at")).total_seconds()
        report["age_seconds"] = age
        if age < -cfg.max_clock_skew_seconds or age > cfg.max_attestation_age_seconds:
            report["freshness"] = "FUTURE" if age < 0 else "STALE"
            continue
        report["freshness"] = "FRESH"
        fingerprints.setdefault(str(report["key_fingerprint"]), []).append(observer)
        fresh.append((observer, report, report["document"]))
    reused = {fp: names for fp, names in fingerprints.items() if len(names) > 1}
    if reused:
        bad = {name for names in reused.values() for name in names}
        fresh = [item for item in fresh if item[0] not in bad]

    witness_votes: dict[tuple[str, int, str], set[str]] = {}
    head_hashes: dict[tuple[str, int], set[str]] = {}
    statuses: dict[str, str] = {}
    for observer, _, doc in fresh:
        statuses[observer] = str((doc.get("watcher") or {}).get("status", "UNKNOWN")).upper()
        head = doc.get("remote_head") if isinstance(doc.get("remote_head"), dict) else None
        if head:
            seq, boot, h = int(head.get("sequence", 0)), str(head.get("boot_id", "")), str(head.get("checkpoint_sha256", ""))
            if seq > 0 and _HEX64.fullmatch(h):
                head_hashes.setdefault((boot, seq), set()).add(h)
        for witness in doc.get("witnesses") or []:
            key = (str(witness.get("boot_id", "")), int(witness.get("sequence", 0)), str(witness.get("checkpoint_sha256", "")))
            if key[1] > 0 and _HEX64.fullmatch(key[2]):
                witness_votes.setdefault(key, set()).add(observer)
    conflicts = [{"boot_id": b, "sequence": s, "hashes": sorted(h)} for (b, s), h in head_hashes.items() if len(h) > 1]
    eligible = [(key, voters) for key, voters in witness_votes.items() if len(voters) >= cfg.quorum_size]
    winning = max(eligible, key=lambda item: item[0][1]) if eligible else None
    status, code, detail = "OK", "WATCHER_QUORUM_OK", "strict-majority observers share a signed liveness witness"
    if conflicts:
        status, code, detail = "CRITICAL", "WATCHER_QUORUM_HEAD_CONFLICT", "same-sequence head hashes conflict"
    elif reused:
        status, code, detail = "CRITICAL", "WATCHER_QUORUM_KEY_REUSE", "observer identities reused a signing key"
    elif winning is None:
        if len(fresh) < cfg.quorum_size:
            status, code, detail = "CRITICAL", "WATCHER_QUORUM_INSUFFICIENT", "not enough fresh independent observer attestations"
        else:
            status, code, detail = "CRITICAL", "WATCHER_QUORUM_NO_COMMON_WITNESS", "fresh observers share no checkpoint witness at quorum size"
    else:
        (boot, seq, h), voters = winning
        previous_seq, previous_hash = int(state.get("last_sequence") or 0), str(state.get("last_checkpoint_sha256") or "")
        if seq < previous_seq:
            status, code, detail = "CRITICAL", "WATCHER_QUORUM_REWIND", "quorum-confirmed sequence moved backwards"
        elif seq == previous_seq and previous_hash and h != previous_hash:
            status, code, detail = "CRITICAL", "WATCHER_QUORUM_MUTATED", "quorum-confirmed hash changed at remembered sequence"
        else:
            voter_statuses = [statuses.get(name, "UNKNOWN") for name in voters]
            if any(value == "CRITICAL" for value in voter_statuses):
                status, code, detail = "CRITICAL", "WATCHER_QUORUM_RUNTIME_CRITICAL", "quorum observers attest a critical runtime state"
            elif any(value in {"WARNING", "WARN"} for value in voter_statuses):
                status, code, detail = "WARNING", "WATCHER_QUORUM_DEGRADED", "quorum agrees on chain but a voter reports warning state"
            elif len(fresh) < len(cfg.expected_observers):
                status, code, detail = "WARNING", "WATCHER_QUORUM_PARTIAL", "quorum exists but an expected observer is missing/stale"
            state.update({"last_sequence": seq, "last_checkpoint_sha256": h, "last_boot_id": boot})
    state["last_evaluated_at"] = _iso(moment)
    _atomic_json(state_path, state)
    witness = None if winning is None else {"boot_id": winning[0][0], "sequence": winning[0][1],
                                             "checkpoint_sha256": winning[0][2], "observers": sorted(winning[1]), "votes": len(winning[1])}
    return {"ok": status in {"OK", "WARNING"}, "status": status, "code": code, "detail": detail,
            "quorum_size": cfg.quorum_size, "expected_observers": list(cfg.expected_observers),
            "fresh_observers": sorted(item[0] for item in fresh), "winning_witness": witness,
            "duplicate_key_observers": reused, "conflicting_heads": conflicts, "observers": reports, "state_path": str(state_path)}


def run_observer_cycle(cfg: WatcherQuorumConfig, watcher_cfg: RemoteWatcherConfig, *, root: str | Path = ".", now: datetime | None = None) -> dict[str, Any]:
    if (watcher_cfg.observer_id, watcher_cfg.environment_id, watcher_cfg.machine_id) != (cfg.observer_id, cfg.environment_id, cfg.machine_id):
        raise ValueError("Phase 34 and Phase 35 identities must match")
    moment = (now or _now()).astimezone(timezone.utc)
    watcher = check_once(watcher_cfg, root=root, now=moment)
    attestation = publish_attestation(cfg, watcher, root=root, now=moment)
    return {"watcher": watcher, "attestation": attestation, "quorum": evaluate_quorum(cfg, root=root, now=moment)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 35 multi-observer watcher quorum")
    parser.add_argument("--mode", choices=["publish", "verify"], required=True)
    parser.add_argument("--config", default="watcher_quorum.yaml")
    parser.add_argument("--watcher-config", default="remote_watcher.yaml")
    parser.add_argument("--root", default=".")
    args = parser.parse_args()
    cfg = load_quorum_config(args.config, root=args.root)
    if args.mode == "publish":
        result = run_observer_cycle(cfg, load_watcher_config(args.watcher_config, root=args.root), root=args.root)
        status = str(result["quorum"].get("status", "CRITICAL")).upper()
    else:
        result = evaluate_quorum(cfg, root=args.root)
        status = str(result.get("status", "CRITICAL")).upper()
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    raise SystemExit(2 if status == "CRITICAL" else 1 if status == "WARNING" else 0)


if __name__ == "__main__":
    main()
