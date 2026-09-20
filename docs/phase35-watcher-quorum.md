# Phase 35 — Multi-Observer Quorum & Watcher Attestation

Phase 35 extends the independent Phase 34 observer into a multi-observer trust model. It does **not** create a remote trading control plane. The Trading PC remains governed by the existing live arming, pending-intent, reconciliation, production-halt, signed-release, deployment-approval, boot-attestation and runtime-liveness gates.

## Goal

A single Phase 34 watcher can remember a previously observed remote head and detect rollback. It is still one failure domain: its host, state or local view can disappear or be compromised. Phase 35 lets multiple independent watcher hosts sign what they observed and requires a strict-majority quorum before the observation is treated as corroborated.

A recommended topology is 2-of-3:

```text
Trading PC -> off-device Phase 32/33 ledger
                 |       |       |
                 v       v       v
             watcher A watcher B watcher C
                 |       |       |
                 +-- signed observer attestations --+
                                      |
                                      v
                              Phase 35 quorum verify
```

Use different hosts, different watcher signing keys and preferably different failure domains. Running three processes on one machine is not equivalent to three independent observers.

## Separate watcher-attestation keys

Each observer uses its own Ed25519 private key supplied only through the environment variable named by `private_key_env` (default `FOREX_WATCHER_ATTESTATION_PRIVATE_KEY`). The private key must be outside the project tree. Public keys and lifecycle metadata are listed in `watcher_quorum_trust.yaml`.

The Phase 35 trust store is intentionally separate from the release, CI, deployment-approval and runtime-boot signing roles. A watcher key therefore does not gain authority to approve a release, attest a Trading PC boot or arm/send an order.

Trust records support:
- `observer_id`
- stable `key_id`
- Ed25519 public-key path
- SHA-256 public-key fingerprint
- `valid_from` / optional `valid_until`
- hard `revoked` state

Hard revocation invalidates historical observer signatures from that key. Keep previous public keys in the trust store during planned rotations when historical verification is required.

## Rolling checkpoint witnesses

Watchers do not need to observe the exact same latest Phase 33 sequence at the same instant. Every signed observer attestation carries a rolling set of the last `witness_depth` checkpoint hashes that the Phase 34 full verifier accepted.

Example:

```text
watcher A latest: 104  witnesses: 73..104
watcher B latest: 105  witnesses: 74..105
watcher C latest: 103  witnesses: 72..103
```

All three can still corroborate checkpoint 103 even though their latest heads are staggered. The verifier selects the **highest checkpoint witness with at least `quorum_size` independent votes**.

This avoids an exact-head equality requirement that would generate false split-brain alarms during normal heartbeat progression.

## Strict-majority rule

`quorum_size` must be a strict majority of `expected_observers`:
- 3 observers -> at least 2
- 4 observers -> at least 3
- 5 observers -> at least 3

This prevents two disjoint groups from both satisfying quorum at the same time.

## Independent-key rule

Two observer identities using the same public-key fingerprint are not two votes. Key reuse produces `WATCHER_QUORUM_KEY_REUSE` and is CRITICAL. This prevents cloning one private key across machines and calling the copies independent observers.

## Quorum high-water memory

The quorum verifier keeps its own local high-water state in `state_path`. If a later strict-majority witness moves behind the previously corroborated sequence, it raises `WATCHER_QUORUM_REWIND`. If the same corroborated sequence changes hash, it raises `WATCHER_QUORUM_MUTATED`.

This is deliberately independent of each Phase 34 watcher's own high-water state.

## Important outcomes

Typical status codes include:
- `WATCHER_QUORUM_OK` — all expected observers are fresh and a strict-majority common witness exists.
- `WATCHER_QUORUM_PARTIAL` — quorum exists, but at least one expected observer is missing/stale.
- `WATCHER_QUORUM_DEGRADED` — chain quorum exists but a quorum member reports warning state.
- `WATCHER_QUORUM_INSUFFICIENT` — fewer than quorum-size fresh, valid, independently keyed attestations.
- `WATCHER_QUORUM_NO_COMMON_WITNESS` — enough fresh observers exist but no checkpoint hash reaches quorum.
- `WATCHER_QUORUM_HEAD_CONFLICT` — observers report different hashes for the exact same latest sequence.
- `WATCHER_QUORUM_KEY_REUSE` — observer identities reuse the same signing key.
- `WATCHER_QUORUM_REWIND` — corroborated sequence moved backwards.
- `WATCHER_QUORUM_MUTATED` — corroborated hash changed at an already remembered sequence.
- `WATCHER_QUORUM_RUNTIME_CRITICAL` — quorum members attest a CRITICAL Phase 34 runtime result.

## Configuration

Copy the examples and edit them per observer:

```powershell
Copy-Item watcher_quorum.example.yaml watcher_quorum.yaml
Copy-Item watcher_quorum_trust.example.yaml watcher_quorum_trust.yaml
```

All observers monitoring the same Trading PC must use the same `environment_id`, `machine_id`, expected-observer list, quorum size and trust anchors. Each machine uses its own `observer_id` and private key.

The `quorum_root` must be outside the source project. Prefer a storage location independent of the Trading PC's Phase 32 ledger. Protect it with ACLs and, where practical, immutable snapshots/WORM semantics.

## Publish one observer attestation

`run-remote-watcher.ps1` automatically switches to Phase 35 publish mode when `watcher_quorum.yaml` exists. Equivalent command:

```bash
python -m src.watcher_quorum \
  --mode publish \
  --root . \
  --config watcher_quorum.yaml \
  --watcher-config remote_watcher.yaml
```

Publish mode first runs the complete Phase 34 check, signs the observation into that observer's latest attestation slot, verifies the signature, then evaluates current quorum.

One observer identity should have one active publisher. Shared-filesystem append is not a distributed lock service; do not run multiple concurrent publishers under the same `observer_id`.

## Verify quorum without a private key

A central monitor can verify quorum using public keys only:

```bash
python -m src.watcher_quorum --mode verify --root . --config watcher_quorum.yaml
```

On Windows:

```powershell
deploy\windows\run-watcher-quorum-verify.ps1
```

## Storage layout

```text
<quorum_root>/
  <environment_id>/
    <machine_id>/
      observers/
        watcher-a/
          attestation.json
          attestation.signature.json
        watcher-b/
          attestation.json
          attestation.signature.json
        watcher-c/
          ...
```

Each observer publishes one atomically replaced **latest signed attestation**. Replay/rollback resistance comes from attestation freshness, rolling checkpoint witnesses, independent watcher keys, and the quorum verifier's persistent high-water mark. The quorum store itself should still use durable/immutable storage where practical.

## Threat-model boundary

Phase 35 improves detection of:
- one watcher host failing or disappearing,
- one stale observer,
- one observer seeing a different remote storage view,
- storage rollback seen by a quorum,
- signature tamper,
- watcher-key cloning across observer identities,
- a single observer's local-state loss.

It does not make a compromised majority trustworthy. If an attacker controls a strict majority of watcher private keys and their local state, they can sign a false majority view. Keep watcher keys on independent hosts and protect them separately.

Phase 35 also does not stop, flatten, resize, cancel or submit broker orders. Detection and execution authority remain separate on purpose.
