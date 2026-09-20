from __future__ import annotations

import ast
import base64
import hashlib
import io
import string
import tarfile
import zlib
from pathlib import Path

EXPECTED_LENGTH = 15828
EXPECTED_SHA256 = "691b383d40978048011be324b7a8bfc907cc6950d07bfdd882d02d86b6326438"
PARTS = [Path(f"tools/phase35_payload/part{i:02d}.txt") for i in range(1, 6)]
BASE64_ALPHABET = (string.ascii_uppercase + string.ascii_lowercase + string.digits + "+/=").encode("ascii")


def _valid(payload: str) -> tuple[bool, str]:
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return len(payload) == EXPECTED_LENGTH and digest == EXPECTED_SHA256, digest


def _payload_from_parts() -> tuple[str, str]:
    missing = [str(path) for path in PARTS if not path.is_file()]
    if missing:
        return "", f"missing={missing}"
    payload = "".join(path.read_text(encoding="utf-8").strip() for path in PARTS)
    ok, digest = _valid(payload)
    return payload, f"parts:length={len(payload)} sha256={digest} ok={ok}"


def _payload_from_v1() -> tuple[str, str]:
    path = Path("tools/phase35_apply.py")
    if not path.is_file():
        return "", "v1:missing"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "PAYLOAD" for target in node.targets
        ):
            payload = ast.literal_eval(node.value)
            if not isinstance(payload, str):
                raise RuntimeError("Phase 35 v1 PAYLOAD is not a string")
            ok, digest = _valid(payload)
            return payload, f"v1:length={len(payload)} sha256={digest} ok={ok}"
    return "", "v1:PAYLOAD assignment missing"


def _gzip_failure_hint(payload: str) -> int | None:
    padding = "=" * ((4 - len(payload) % 4) % 4)
    try:
        compressed = base64.b64decode(payload + padding, validate=True)
    except Exception:
        return None
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    for index, value in enumerate(compressed):
        try:
            decoder.decompress(bytes([value]))
        except zlib.error:
            return max(0, (index * 4) // 3)
    return None


def _repair_one_missing_character(payload: str) -> tuple[str | None, str]:
    if len(payload) != EXPECTED_LENGTH - 1:
        return None, f"repair:not-applicable length={len(payload)}"

    raw = payload.encode("ascii")
    hint = _gzip_failure_hint(payload)
    windows: list[range] = []
    if hint is not None:
        windows.append(range(max(0, hint - 1024), min(len(raw) + 1, hint + 1025)))
    windows.append(range(0, len(raw) + 1))
    seen: set[int] = set()

    for window_index, positions in enumerate(windows, start=1):
        for position in positions:
            if position in seen:
                continue
            seen.add(position)
            prefix = raw[:position]
            suffix = raw[position:]
            for char in BASE64_ALPHABET:
                candidate = prefix + bytes([char]) + suffix
                if hashlib.sha256(candidate).hexdigest() == EXPECTED_SHA256:
                    repaired = candidate.decode("ascii")
                    ok, digest = _valid(repaired)
                    if not ok:
                        raise RuntimeError("Phase 35 repair matched hash but failed final validation")
                    return repaired, (
                        f"repair:success position={position} char={chr(char)!r} "
                        f"hint={hint} pass={window_index} sha256={digest}"
                    )
    return None, f"repair:failed hint={hint} tried_positions={len(seen)}"


parts_payload, parts_diag = _payload_from_parts()
if _valid(parts_payload)[0]:
    payload = parts_payload
    source = "parts"
    v1_diag = "v1:not-needed"
    repair_diag = "repair:not-needed"
else:
    v1_payload, v1_diag = _payload_from_v1()
    if _valid(v1_payload)[0]:
        payload = v1_payload
        source = "v1"
        repair_diag = "repair:not-needed"
    else:
        repaired, repair_diag = _repair_one_missing_character(v1_payload)
        if repaired is None:
            raise RuntimeError(
                "Phase 35 payload transport checksum mismatch: "
                f"expected_length={EXPECTED_LENGTH} expected_sha256={EXPECTED_SHA256}; "
                f"{parts_diag}; {v1_diag}; {repair_diag}"
            )
        payload = repaired
        source = "v1-repaired"

digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
archive = base64.b64decode(payload, validate=True)
root = Path(".").resolve()
with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tf:
    for member in tf.getmembers():
        if member.islnk() or member.issym():
            raise RuntimeError(f"Phase 35 payload links are not allowed: {member.name}")
        target = (root / member.name).resolve()
        if target == root or root not in target.parents:
            raise RuntimeError(f"Phase 35 payload path escapes repository root: {member.name}")
    tf.extractall(root)

print(
    "Phase 35 payload verified and extracted: "
    f"source={source} length={len(payload)} sha256={digest}; "
    f"{parts_diag}; {v1_diag}; {repair_diag}"
)
