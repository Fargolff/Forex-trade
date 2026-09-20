from __future__ import annotations

import ast
import base64
import hashlib
import io
import tarfile
from pathlib import Path

EXPECTED_LENGTH = 15828
EXPECTED_SHA256 = "691b383d40978048011be324b7a8bfc907cc6950d07bfdd882d02d86b6326438"
PARTS = [Path(f"tools/phase35_payload/part{i:02d}.txt") for i in range(1, 6)]


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
        if isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == "PAYLOAD" for target in node.targets):
                payload = ast.literal_eval(node.value)
                if not isinstance(payload, str):
                    raise RuntimeError("Phase 35 v1 PAYLOAD is not a string")
                ok, digest = _valid(payload)
                return payload, f"v1:length={len(payload)} sha256={digest} ok={ok}"
    return "", "v1:PAYLOAD assignment missing"


parts_payload, parts_diag = _payload_from_parts()
if _valid(parts_payload)[0]:
    payload = parts_payload
    source = "parts"
    v1_diag = "v1:not-needed"
else:
    v1_payload, v1_diag = _payload_from_v1()
    if _valid(v1_payload)[0]:
        payload = v1_payload
        source = "v1"
    else:
        raise RuntimeError(
            "Phase 35 payload transport checksum mismatch: "
            f"expected_length={EXPECTED_LENGTH} expected_sha256={EXPECTED_SHA256}; "
            f"{parts_diag}; {v1_diag}"
        )

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
    f"source={source} length={len(payload)} sha256={digest}; {parts_diag}; {v1_diag}"
)
