from __future__ import annotations
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import pytest, yaml
import src.watcher_quorum as quorum
from src.release import generate_keypair
from src.remote_watcher import RemoteWatcherConfig

NOW=datetime(2026,9,20,16,30,tzinfo=timezone.utc)
EXPECTED=("watcher-a","watcher-b","watcher-c")

def _json(p:Path,v:dict):
    p.parent.mkdir(parents=True,exist_ok=True); p.write_text(json.dumps(v,sort_keys=True)+"\n",encoding="utf-8")

def _setup(tmp_path:Path, shared=False):
    root=tmp_path/"project"; remote=tmp_path/"quorum"; pubs=root/"release"/"watcher_keys"; pubs.mkdir(parents=True)
    priv={}; pub={}; shared_pair=None
    for o in EXPECTED:
        if shared and o=="watcher-b": priv[o],pub[o]=shared_pair; continue
        pr=tmp_path/f"{o}.private.pem"; pu=pubs/f"{o}.pem"; generate_keypair(pr,pu); priv[o]=pr; pub[o]=pu
        if shared and o=="watcher-a": shared_pair=(pr,pu)
    keys=[]
    for o in EXPECTED:
        keys.append({"observer_id":o,"key_id":f"{o}-k1","public_key":pub[o].relative_to(root).as_posix(),"fingerprint":quorum._fingerprint(quorum._public(pub[o])),"valid_from":"2026-01-01T00:00:00Z","valid_until":"2099-01-01T00:00:00Z","revoked":False})
    (root/"watcher_quorum_trust.yaml").write_text(yaml.safe_dump({"version":1,"format":quorum.TRUST_FORMAT,"keys":keys},sort_keys=False),encoding="utf-8")
    return root,remote,priv

def _cfg(root,remote,observer="watcher-a",max_age=180):
    return quorum.WatcherQuorumConfig(observer_id=observer,environment_id="prod-bkk-01",machine_id="trader-pc-01",quorum_root=str(remote),expected_observers=EXPECTED,quorum_size=2,trust_path="watcher_quorum_trust.yaml",state_path="runtime/watcher_quorum_state.json",witness_depth=8,max_attestation_age_seconds=max_age)

def _view(base:Path, upto:int, *, boot="boot-1", fork_from=None, fork="main"):
    scope=base/"liveness"; entries=scope/"entries"; entries.mkdir(parents=True,exist_ok=True); latest=""
    for seq in range(1,upto+1):
        f=fork if fork_from is not None and seq>=fork_from else "main"
        doc={"version":1,"format":"test","sequence":seq,"boot_id":boot,"payload":f"{seq}-{f}"}
        p=entries/f"{seq:08d}-e"/"checkpoint.json"; _json(p,doc); latest=quorum._sha(p)
    return scope,{"sequence":upto,"checkpoint_sha256":latest,"boot_id":boot,"stage":"POST_CYCLE","status":"OK","observed_at":NOW.isoformat()}

def _result(o,scope,head,status="OK",code="WATCHER_OK"):
    return {"ok":status in {"OK","RECOVERED"},"status":status,"code":code,"detail":"fixture","observer_id":o,"environment_id":"prod-bkk-01","machine_id":"trader-pc-01","verification":{"ok":True,"scope":str(scope)},"head":head}

def _pub(root,remote,priv,o,result,monkeypatch,now=NOW):
    monkeypatch.setenv(quorum.PRIVATE_KEY_ENV,str(priv[o])); return quorum.publish_attestation(_cfg(root,remote,o),result,root=root,now=now)

def test_two_of_three_partial_quorum(tmp_path,monkeypatch):
    root,remote,priv=_setup(tmp_path); scope,head=_view(tmp_path/"v",5)
    for o in EXPECTED[:2]: _pub(root,remote,priv,o,_result(o,scope,head),monkeypatch)
    r=quorum.evaluate_quorum(_cfg(root,remote),root=root,now=NOW); assert (r["status"],r["code"],r["winning_witness"]["votes"])==("WARNING","WATCHER_QUORUM_PARTIAL",2)

def test_staggered_heads_share_rolling_witness(tmp_path,monkeypatch):
    root,remote,priv=_setup(tmp_path); a,ha=_view(tmp_path/"a",5); b,hb=_view(tmp_path/"b",6)
    _pub(root,remote,priv,"watcher-a",_result("watcher-a",a,ha),monkeypatch); _pub(root,remote,priv,"watcher-b",_result("watcher-b",b,hb),monkeypatch)
    assert quorum.evaluate_quorum(_cfg(root,remote),root=root,now=NOW)["winning_witness"]["sequence"]==5

def test_all_three_healthy_is_ok(tmp_path,monkeypatch):
    root,remote,priv=_setup(tmp_path); s,h=_view(tmp_path/"v",3)
    for o in EXPECTED: _pub(root,remote,priv,o,_result(o,s,h),monkeypatch)
    r=quorum.evaluate_quorum(_cfg(root,remote),root=root,now=NOW); assert (r["status"],r["code"])==("OK","WATCHER_QUORUM_OK")

def test_same_sequence_different_heads_conflict(tmp_path,monkeypatch):
    root,remote,priv=_setup(tmp_path); a,ha=_view(tmp_path/"a",5); b,hb=_view(tmp_path/"b",5,fork_from=5,fork="evil")
    _pub(root,remote,priv,"watcher-a",_result("watcher-a",a,ha),monkeypatch); _pub(root,remote,priv,"watcher-b",_result("watcher-b",b,hb),monkeypatch)
    assert quorum.evaluate_quorum(_cfg(root,remote),root=root,now=NOW)["code"]=="WATCHER_QUORUM_HEAD_CONFLICT"

def test_no_common_witness(tmp_path,monkeypatch):
    root,remote,priv=_setup(tmp_path); a,ha=_view(tmp_path/"a",4,boot="ba",fork_from=1,fork="a"); b,hb=_view(tmp_path/"b",5,boot="bb",fork_from=1,fork="b")
    _pub(root,remote,priv,"watcher-a",_result("watcher-a",a,ha),monkeypatch); _pub(root,remote,priv,"watcher-b",_result("watcher-b",b,hb),monkeypatch)
    assert quorum.evaluate_quorum(_cfg(root,remote),root=root,now=NOW)["code"]=="WATCHER_QUORUM_NO_COMMON_WITNESS"

def test_stale_observer_excluded(tmp_path,monkeypatch):
    root,remote,priv=_setup(tmp_path); s,h=_view(tmp_path/"v",3)
    _pub(root,remote,priv,"watcher-a",_result("watcher-a",s,h),monkeypatch,now=NOW-timedelta(minutes=10)); _pub(root,remote,priv,"watcher-b",_result("watcher-b",s,h),monkeypatch)
    assert quorum.evaluate_quorum(_cfg(root,remote,max_age=120),root=root,now=NOW)["code"]=="WATCHER_QUORUM_INSUFFICIENT"

@pytest.mark.parametrize("status,code,expected",[("WARNING","WATCHER_HEARTBEAT_LATE","WATCHER_QUORUM_DEGRADED"),("CRITICAL","WATCHER_RUNTIME_HALTED","WATCHER_QUORUM_RUNTIME_CRITICAL")])
def test_runtime_status_propagates(tmp_path,monkeypatch,status,code,expected):
    root,remote,priv=_setup(tmp_path); s,h=_view(tmp_path/"v",3)
    _pub(root,remote,priv,"watcher-a",_result("watcher-a",s,h,status,code),monkeypatch)
    for o in EXPECTED[1:]: _pub(root,remote,priv,o,_result(o,s,h),monkeypatch)
    assert quorum.evaluate_quorum(_cfg(root,remote),root=root,now=NOW)["code"]==expected

def test_tamper_not_counted(tmp_path,monkeypatch):
    root,remote,priv=_setup(tmp_path); s,h=_view(tmp_path/"v",3)
    for o in EXPECTED[:2]: _pub(root,remote,priv,o,_result(o,s,h),monkeypatch)
    p=remote/"prod-bkk-01"/"trader-pc-01"/"observers"/"watcher-b"/"attestation.json"; d=json.loads(p.read_text()); d["watcher"]["status"]="TAMPER"; _json(p,d)
    assert quorum.evaluate_quorum(_cfg(root,remote),root=root,now=NOW)["code"]=="WATCHER_QUORUM_INSUFFICIENT"

def test_reused_key_is_critical(tmp_path,monkeypatch):
    root,remote,priv=_setup(tmp_path,shared=True); s,h=_view(tmp_path/"v",3)
    for o in EXPECTED[:2]: _pub(root,remote,priv,o,_result(o,s,h),monkeypatch)
    assert quorum.evaluate_quorum(_cfg(root,remote),root=root,now=NOW)["code"]=="WATCHER_QUORUM_KEY_REUSE"

def test_high_water_rewind(tmp_path,monkeypatch):
    root,remote,priv=_setup(tmp_path); cfg=_cfg(root,remote); s5,h5=_view(tmp_path/"v5",5)
    for o in EXPECTED: _pub(root,remote,priv,o,_result(o,s5,h5),monkeypatch)
    assert quorum.evaluate_quorum(cfg,root=root,now=NOW)["status"]=="OK"
    s3,h3=_view(tmp_path/"v3",3)
    for o in EXPECTED: _pub(root,remote,priv,o,_result(o,s3,h3),monkeypatch,now=NOW+timedelta(seconds=30))
    assert quorum.evaluate_quorum(cfg,root=root,now=NOW+timedelta(seconds=30))["code"]=="WATCHER_QUORUM_REWIND"

def test_high_water_same_sequence_mutation(tmp_path,monkeypatch):
    root,remote,priv=_setup(tmp_path); cfg=_cfg(root,remote); s,h=_view(tmp_path/"main",5)
    for o in EXPECTED: _pub(root,remote,priv,o,_result(o,s,h),monkeypatch)
    assert quorum.evaluate_quorum(cfg,root=root,now=NOW)["status"]=="OK"
    s2,h2=_view(tmp_path/"alt",5,fork_from=5,fork="alt")
    for o in EXPECTED: _pub(root,remote,priv,o,_result(o,s2,h2),monkeypatch,now=NOW+timedelta(seconds=20))
    assert quorum.evaluate_quorum(cfg,root=root,now=NOW+timedelta(seconds=20))["code"]=="WATCHER_QUORUM_MUTATED"

def test_revoked_key_invalidates_attestation(tmp_path,monkeypatch):
    root,remote,priv=_setup(tmp_path); s,h=_view(tmp_path/"v",2); _pub(root,remote,priv,"watcher-a",_result("watcher-a",s,h),monkeypatch)
    p=root/"watcher_quorum_trust.yaml"; t=yaml.safe_load(p.read_text()); t["keys"][0]["revoked"]=True; p.write_text(yaml.safe_dump(t,sort_keys=False))
    r=quorum.verify_attestation(_cfg(root,remote),"watcher-a",root=root,now=NOW); assert not r["ok"] and any("REVOKED" in x for x in r["issues"])

def test_strict_majority_required(tmp_path):
    root=tmp_path/"project"; root.mkdir(); cfg=quorum.WatcherQuorumConfig(observer_id="a",environment_id="prod",machine_id="trader",quorum_root=str(tmp_path/"q"),expected_observers=("a","b","c","d"),quorum_size=2)
    with pytest.raises(ValueError,match="strict majority"): quorum._validate(cfg,root)

def test_private_key_inside_project_rejected(tmp_path,monkeypatch):
    root,remote,_=_setup(tmp_path); bad=root/"bad.pem"; pub=root/"release"/"watcher_keys"/"bad.pub.pem"; generate_keypair(bad,pub); s,h=_view(tmp_path/"v",2); monkeypatch.setenv(quorum.PRIVATE_KEY_ENV,str(bad))
    with pytest.raises(ValueError,match="outside the project root"): quorum.publish_attestation(_cfg(root,remote),_result("watcher-a",s,h),root=root,now=NOW)

def test_missing_private_key_rejected(tmp_path,monkeypatch):
    root,remote,_=_setup(tmp_path); s,h=_view(tmp_path/"v",2); monkeypatch.delenv(quorum.PRIVATE_KEY_ENV,raising=False)
    with pytest.raises(ValueError,match="required"): quorum.publish_attestation(_cfg(root,remote),_result("watcher-a",s,h),root=root,now=NOW)

def test_phase34_phase35_identity_mismatch(tmp_path):
    root,remote,_=_setup(tmp_path); wc=RemoteWatcherConfig(observer_id="watcher-b",environment_id="prod-bkk-01",machine_id="trader-pc-01",replica_root=str(tmp_path/"replica"))
    with pytest.raises(ValueError,match="identities must match"): quorum.run_observer_cycle(_cfg(root,remote),wc,root=root,now=NOW)
