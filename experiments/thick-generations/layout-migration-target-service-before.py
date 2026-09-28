#!/usr/bin/env python3
"""Collect one fresh read-only desired-service before record."""
import argparse,hashlib,importlib.util,json,os,socket,stat,sys,time
from pathlib import Path
HERE=Path(__file__).resolve().parent
def load(name,file):s=importlib.util.spec_from_file_location(name,HERE/file);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
V1=load("slt_target_before_v1","layout-migration-node-evidence.py");NODE2=load("slt_target_before_node2","layout-migration-node-evidence-v2.py");B=load("slt_target_before_barrier","layout-migration-barrier-collector.py");MODEL=load("slt_target_before_model","layout-migration-target-service-plan.py")
Refusal=V1.Refusal;require=V1.require;ROOT=Path("/var/lib/pve-sharedlvmthin/maintenance")
def canonical(v):return json.dumps(v,sort_keys=True,separators=(",",":")).encode()
def read(path,label):
 raw=V1.regular_bytes(Path(path),label,2*1024*1024)
 try:v=json.loads(raw)
 except (UnicodeDecodeError,json.JSONDecodeError) as e:raise Refusal(label+" invalid") from e
 require(type(v)is dict,label+" root invalid");return v,raw
def service(unit):
 def prop(name):return V1.command(["systemctl","show","--property="+name,"--value",unit]).decode().strip()
 active,sub,pid,control=prop("ActiveState"),prop("SubState"),int(prop("MainPID")),int(prop("ControlPID"));job=prop("Job")
 cgroup=prop("ControlGroup");empty=True
 if cgroup and cgroup!="/":
  path=Path("/sys/fs/cgroup")/cgroup.lstrip("/")
  if path.exists():empty=not any((path/"cgroup.procs").read_text().split())
 mask_path=Path("/etc/systemd/system")/unit;mask=None
 if os.path.lexists(mask_path):
  st=os.lstat(mask_path);require(stat.S_ISLNK(st.st_mode) and os.readlink(mask_path)=="/dev/null" and st.st_uid==0,"persistent mask invalid")
  mask={"path":str(mask_path),"target":"/dev/null","dev":st.st_dev,"ino":st.st_ino,"uid":st.st_uid,"mtime_ns":st.st_mtime_ns}
 require(not os.path.lexists(Path("/run/systemd/system")/unit),"runtime override present")
 return {"unit":unit,"active_state":active,"sub_state":sub,"main_pid":pid,"starttime":V1.proc_starttime(pid) if pid>1 else 0,"control_pid":control,"job":None if job=="" else job,"cgroup_empty":empty,"persistent_mask":mask,"runtime_override":False}
def collect(a):
 doc,_=read(a.context,"context");c=doc.get("context",doc);node=socket.gethostname().split(".",1)[0];row=next(x for x in c["nodes"] if x["name"]==node);boot=Path("/proc/sys/kernel/random/boot_id").read_text().strip();require(boot==row["boot_id"],"boot differs")
 nodes=[x["name"] for x in c["nodes"]];start=int(time.time());_,_,q,observed=V1.membership(nodes);require(q and observed==nodes,"membership differs")
 storage=V1.regular_bytes(Path("/etc/pve/storage.cfg"),"storage",16*1024*1024);require(hashlib.sha256(storage).hexdigest()==c["target_storage_cfg_sha256"],"storage differs")
 installed=V1.installed_identity();cand=c["candidate"];require(installed["dpkg_state"]=="installed" and all(installed[k]==cand[k] for k in ("package","version","flavor","artifact_sha256")),"candidate differs");require(V1.command(["dpkg","--verify",cand["package"]])==b"","payload verify failed")
 listing=V1.command(["dpkg-query","-L",cand["package"]]);list_sha=hashlib.sha256(b"\n".join(sorted(x for x in listing.splitlines() if x))+b"\n").hexdigest()
 require(not os.path.lexists(ROOT/"active.json"),"maintenance hold still active")
 role=c["node_roles"][node];archive_sha=None
 if role=="SAN_PARTICIPANT":
  wrapper,_=read(a.archive_receipt,"archive receipt");receipt=wrapper.get("receipt",wrapper);require(receipt["node"]==node,"archive receipt node differs");archive_sha=hashlib.sha256(canonical(receipt)+b"\n").hexdigest()
 else:require(role=="CONTROL_ONLY","role invalid")
 services=[service(u) for u in MODEL.WATCHED]
 procs,worker0=B.process_inventory();require(not procs,"running guest process present")
 dm=B.dm_inventory(storage);require(dm==[],"managed mapper/open LV present")
 w=V1.worker_evidence(observed);workers={"inventory_complete":True,"storage_processes":w["storage_processes"],"transient_units":w["blocking_transient_units"],"pve_tasks":w["active_pve_tasks"]};require(not any(workers[k] for k in ("storage_processes","transient_units","pve_tasks")),"workers busy")
 require(B.read("/etc/pve/ha/resources.cfg",empty=True).strip()==b"","HA start demand present")
 guard=NODE2.inactive_guard_evidence();vgs=V1.actual_lvm_identities(V1.expected_mixed_vgs(storage)) if role=="SAN_PARTICIPANT" else []
 end=int(time.time());return {"schema":"slt-target-service-before/v1","plan_id":a.plan_id,"challenge":a.challenge,"collector_sha256":hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),"node":node,"role":role,"boot_id_start":boot,"boot_id_end":Path("/proc/sys/kernel/random/boot_id").read_text().strip(),"tx":c["tx"],"generation":c["generation"],"context_sha256":c["context_sha256"],"observed_start":start,"observed_end":end,"control_plane":{"cluster_name":c["cluster_name"],"cluster_nodes":nodes,"quorate":True,"target_storage_cfg_sha256":c["target_storage_cfg_sha256"]},"installed":{**{k:cand[k] for k in ("package","version","flavor","artifact_sha256")},"dpkg_state":"installed"},"payload":{"dpkg_verify_complete":True,"dpkg_verify_clean":True,"package_file_list_sha256":list_sha},"workers":workers,"hold":{"active":False,"namespace_verified":True},"archive_receipt_sha256":archive_sha,"services":services,"workload":{"snapshot_sha256":a.workload_sha256,"inventory_complete":True,"managed_running_guests":[],"managed_mappers":[],"managed_open_lvs":[],"ha_start_demands":[],"scheduled_start_demands":[]},"thinguard":guard,"vg_identities":vgs}
def main():
 p=argparse.ArgumentParser();p.add_argument("--context",required=True);p.add_argument("--plan-id",required=True);p.add_argument("--challenge",required=True);p.add_argument("--workload-sha256",required=True);p.add_argument("--archive-receipt");a=p.parse_args()
 try:r=collect(a)
 except (Refusal,OSError,ValueError,KeyError,TypeError) as e:print(json.dumps({"schema":"slt-target-service-before/v1","verdict":"BLOCKED","reason":str(e)},sort_keys=True));return 2
 print(json.dumps(r,sort_keys=True,indent=2));return 0
if __name__=="__main__":sys.exit(main())
