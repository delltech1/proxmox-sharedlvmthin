#!/usr/bin/env python3
"""Collect a read-only ALL_READY-v2 record for a preserve-only refresh plan."""
import argparse, hashlib, importlib.util, json, os, re, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
def load(name, file):
    spec=importlib.util.spec_from_file_location(name,HERE/file);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
V1=load("slt_ready_collector_v1","layout-migration-node-evidence.py")
NODE2=load("slt_ready_collector_node2","layout-migration-node-evidence-v2.py")
Refusal=V1.Refusal; require=V1.require
ROOT=Path("/var/lib/pve-sharedlvmthin/maintenance")

def read(path,label):
 raw=V1.regular_bytes(Path(path),label,2*1024*1024)
 try:value=json.loads(raw)
 except (UnicodeDecodeError,json.JSONDecodeError) as e:raise Refusal(label+" invalid") from e
 require(type(value) is dict,label+" root invalid");return value,raw
def canonical(v):return json.dumps(v,sort_keys=True,separators=(",",":")).encode()
def lifecycle(unit):
 state=V1.command(["systemctl","show","--property=ActiveState","--value",unit]).decode().strip()
 raw_pid=V1.command(["systemctl","show","--property=MainPID","--value",unit]).decode().strip()
 require(state in {"active","inactive"} and raw_pid.isdigit(),"systemd lifecycle output invalid")
 active=state=="active";pid=int(raw_pid);require((active and pid>1) or (not active and pid==0),"service lifecycle invalid")
 return {"active":active,"pid":pid,"starttime":V1.proc_starttime(pid) if active else 0}

def collect(args):
 doc,_=read(args.context,"context");context=doc.get("context",doc)
 auth,_=read(args.authorization,"refresh authorization");manifest,manifest_raw=read(args.manifest,"manifest")
 nodes=[x["name"] for x in context["nodes"]];local,_,q,observed=V1.membership(nodes)
 require(q and observed==nodes,"membership differs");row=next(x for x in context["nodes"] if x["name"]==local)
 boot=V1.pseudo_bytes(Path("/proc/sys/kernel/random/boot_id"),"boot",128).decode().strip();require(boot==row["boot_id"],"boot differs")
 require(auth["tx"]==context["tx"] and auth["generation"]==context["generation"]
         and auth["context_sha256"]==context["context_sha256"] and local in auth["challenges"],"authorization identity differs")
 now=int(time.time());require(auth["issued_at"]<=now<=auth["expires_at"],"authorization expired")
 storage=V1.regular_bytes(Path("/etc/pve/storage.cfg"),"storage",16*1024*1024)
 require(hashlib.sha256(storage).hexdigest()==context["target_storage_cfg_sha256"],"storage differs")
 installed=V1.installed_identity();candidate=context["candidate"]
 require(installed["dpkg_state"]=="installed" and all(installed[k]==candidate[k] for k in ("package","version","flavor","artifact_sha256")),"candidate differs")
 plan=next((x for x in auth["service_plan"] if x["node"]==local),None);require(plan is not None,"local service plan absent")
 before={x["unit"]:lifecycle(x["unit"]) for x in plan["units"]}
 require(all(before[x["unit"]]==x["before"] and x["was_active"] is before[x["unit"]]["active"] for x in plan["units"]),"authorized before lifecycle differs")
 after={name:lifecycle(name) for name in before};require(after==before,"service changed during preserve-only refresh")
 role=context["node_roles"][local]
 results=[]
 for item in plan["units"]:
  result="VERIFIED_UNCHANGED" if role=="CONTROL_ONLY" else ("RESTARTED_ACTIVE" if item["was_active"] else "PRESERVED_INACTIVE")
  # This collector is intentionally preserve-only; it cannot claim a restart.
  require(result!="RESTARTED_ACTIVE","active restart requires a separate mutating executor")
  results.append({"unit":item["unit"],"operation":item["operation"],"before":before[item["unit"]],"after":after[item["unit"]],"result":result})
 if role=="SAN_PARTICIPANT":
  active=V1.regular_bytes(ROOT/"active.json","active hold",1024*1024);require(active==manifest_raw,"active hold differs")
  st=os.stat(ROOT/"active.json",follow_symlinks=False)
  hold={"active":True,"context_sha256":context["context_sha256"],"manifest_sha256":hashlib.sha256(active).hexdigest(),"device":st.st_dev,"inode":st.st_ino}
  vgs=V1.actual_lvm_identities(V1.expected_mixed_vgs(storage))
 else:
  require(not os.path.lexists(ROOT/"active.json"),"control node has hold")
  hold={"active":False,"context_sha256":context["context_sha256"],"manifest_sha256":"0"*64,"device":0,"inode":0};vgs=[]
 guard=NODE2.inactive_guard_evidence();_,_,q2,observed2=V1.membership(nodes);w=V1.worker_evidence(observed2)
 workers={"inventory_complete":True,"storage_processes":w["storage_processes"],"transient_units":w["blocking_transient_units"],"pve_tasks":w["active_pve_tasks"]}
 require(q2 and observed2==nodes and not any(workers[k] for k in ("storage_processes","transient_units","pve_tasks")),"workers busy")
 finished=int(time.time())
 return {"schema":"slt-layout-all-ready-node/v2","challenge":auth["challenges"][local],"authorization_sha256":hashlib.sha256(canonical(auth)).hexdigest(),"observed_at":finished,"tx":context["tx"],"generation":context["generation"],"cluster_name":context["cluster_name"],"node":local,"boot_id":boot,"cluster_nodes":nodes,"quorate":True,"target_storage_cfg_sha256":context["target_storage_cfg_sha256"],"context_sha256":context["context_sha256"],"candidate":candidate,"workers":workers,"hold":hold,"refresh":{"completed_at":finished,"units":results},"thinguard":guard,"vg_identities":vgs}

def main():
 p=argparse.ArgumentParser();p.add_argument("--context",required=True);p.add_argument("--authorization",required=True);p.add_argument("--manifest",required=True);a=p.parse_args()
 try:r=collect(a)
 except (Refusal,OSError,ValueError,KeyError,TypeError) as e:print(json.dumps({"schema":"slt-layout-all-ready-node/v2","verdict":"BLOCKED","authorization":"NONE","mutation_performed":False,"reason":str(e)},sort_keys=True));return 2
 print(json.dumps(r,sort_keys=True,indent=2));return 0
if __name__=="__main__":sys.exit(main())
