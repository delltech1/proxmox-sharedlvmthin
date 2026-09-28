#!/usr/bin/env python3
"""Fail-closed model for supervised shutdown of an exact disposable allowlist."""

import copy, hashlib, json, re

HEX32=re.compile(r"^[0-9a-f]{32}$"); SHA=re.compile(r"^[0-9a-f]{64}$")
class Refusal(Exception): pass
def require(v,m):
 if not v: raise Refusal(m)
def exact(v,fields,label): require(type(v) is dict and set(v)==set(fields),f"{label} fields invalid")
def canonical(v): return json.dumps(v,sort_keys=True,separators=(",",":")).encode()
def digest(v): return hashlib.sha256(canonical(v)).hexdigest()
def parse_upid(value):
 require(type(value) is str,"shutdown UPID invalid")
 parts=value.split(":")
 require(len(parts)==9 and parts[0]=="UPID" and parts[-1]==""
         and parts[1] and all(re.fullmatch(r"[0-9A-Fa-f]+",x) for x in parts[2:5])
         and parts[5] and parts[6].isdigit() and int(parts[6])>0 and parts[7],"shutdown UPID invalid")
 return {"node":parts[1],"task_type":parts[5],"vmid":int(parts[6])}

def validate_plan(plan,now):
 exact(plan,{"schema","tx","barrier_plan_sha256","barrier_evidence_sha256","storage_cfg_sha256","workload_snapshot_sha256","participants","guests","issued_at","deadline","per_task_timeout","force_stop"},"shutdown plan")
 require(plan["schema"]=="slt-supervised-shutdown-model/v1" and HEX32.fullmatch(plan["tx"] or "")
         and all(SHA.fullmatch(plan[k] or "") for k in ("barrier_plan_sha256","barrier_evidence_sha256","storage_cfg_sha256","workload_snapshot_sha256")),"shutdown identity invalid")
 require(type(plan["issued_at"]) is int and type(plan["deadline"]) is int
         and plan["issued_at"]<=now<=plan["deadline"]
    and 0<plan["deadline"]-plan["issued_at"]<=7200
    and type(plan["per_task_timeout"]) is int and not isinstance(plan["per_task_timeout"],bool)
    and 1<=plan["per_task_timeout"]<=900 and plan["force_stop"] is False,"shutdown timing/policy invalid")
 require(type(plan["participants"]) is list and len(plan["participants"])==4,"participant set invalid")
 nodes=[]
 for row in plan["participants"]:
  exact(row,{"node","boot_id"},"participant");require(type(row["node"]) is str and row["node"] and type(row["boot_id"]) is str and row["boot_id"],"participant invalid");nodes.append(row["node"])
 require(nodes==sorted(set(nodes)),"participant set invalid")
 guests=plan["guests"]; require(type(guests) is list and guests,"guest allowlist empty")
 identities=[]; vmids=[]
 for guest in guests:
  exact(guest,{"vmid","type","node","config_sha256","managed_disks","ha_managed"},"guest")
  require(type(guest["vmid"]) is int and not isinstance(guest["vmid"],bool) and guest["vmid"]>0
          and guest["type"] in ("qemu","lxc") and guest["node"] in nodes and guest["ha_managed"] is False
          and SHA.fullmatch(guest["config_sha256"] or "")
          and type(guest["managed_disks"]) is list and guest["managed_disks"]
          and guest["managed_disks"]==sorted(set(guest["managed_disks"])),"guest identity invalid")
  identities.append((guest["node"],guest["type"],guest["vmid"]));vmids.append(guest["vmid"])
 require(identities==sorted(set(identities)) and len(vmids)==len(set(vmids)),"guest allowlist duplicated or unordered")
 return copy.deepcopy(plan)

def validate_snapshot(value,plan,expected):
 exact(value,{"participants","barrier_plan_sha256","barrier_evidence_sha256","storage_cfg_sha256","workload_snapshot_sha256","admission_closed","ha_idle_disarmed","tasks","workers","running_consumers"},"cluster snapshot")
 require(type(value["participants"]) is list and all(type(x) is dict and set(x)=={"node","boot_id"}
         and type(x["node"]) is str and type(x["boot_id"]) is str for x in value["participants"])
         and value["participants"]==plan["participants"] and value["barrier_plan_sha256"]==plan["barrier_plan_sha256"]
         and value["barrier_evidence_sha256"]==plan["barrier_evidence_sha256"] and value["storage_cfg_sha256"]==plan["storage_cfg_sha256"] and value["workload_snapshot_sha256"]==plan["workload_snapshot_sha256"]
         and value["admission_closed"] is True and value["ha_idle_disarmed"] is True,"shutdown prerequisite barrier invalid")
 require(type(value["tasks"]) is list and not value["tasks"] and type(value["workers"]) is list and not value["workers"],"cluster has active task or worker")
 require(type(value["running_consumers"]) is list,"running consumer inventory invalid")
 for row in value["running_consumers"]:
  exact(row,{"vmid","type","node","config_sha256","managed_disks"},"running consumer")
  require(type(row["vmid"]) is int and not isinstance(row["vmid"],bool)
          and type(row["type"]) is str and type(row["node"]) is str
          and type(row["config_sha256"]) is str and type(row["managed_disks"]) is list
          and all(type(x) is str for x in row["managed_disks"]),"running consumer types invalid")
 require(value["running_consumers"]==expected,"running managed consumer set differs from allowlist")
 return copy.deepcopy(value)

class Controller:
 def __init__(self,plan,backend,now):
  self.plan=validate_plan(plan,now); self.plan_sha=digest(self.plan); self.backend=backend
  self.last_clock=now; self.running=False; self.poisoned=False
 def authority(self): require(not self.poisoned and digest(self.plan)==self.plan_sha,"shutdown controller authority changed")
 def clock(self):
  old=self.last_clock; value=self.backend.now(); self.authority()
  require(self.last_clock==old and type(value) is int and not isinstance(value,bool) and old<=value<=self.plan["deadline"],"shutdown clock invalid or expired")
  self.last_clock=value
 def persist(self,kind,index,guest,payload=None):
  event={"tx":self.plan["tx"],"plan_sha256":self.plan_sha,"kind":kind,"index":index,
         "vmid":guest["vmid"],"type":guest["type"],"node":guest["node"]}
  if payload is not None:event["payload"]=copy.deepcopy(payload)
  self.backend.persist_event(event); self.authority(); self.clock()
 def run(self):
  if self.running:self.poisoned=True; raise Refusal("shutdown controller reentered")
  require(not self.poisoned,"shutdown controller poisoned"); self.running=True
  try:
   self.clock(); history=self.backend.load_events(self.plan["tx"]); self.authority(); self.clock()
   require(type(history) is list and not history,"nonempty shutdown history requires explicit recovery")
   expected=[{k:g[k] for k in ("vmid","type","node","config_sha256","managed_disks")} for g in self.plan["guests"]]
   snapshot=self.backend.observe_cluster(); validate_snapshot(snapshot,self.plan,expected); self.authority(); self.clock()
   for index,guest in enumerate(self.plan["guests"]):
    validate_snapshot(self.backend.observe_cluster(),self.plan,expected[index:]);self.authority();self.clock()
    before=copy.deepcopy(self.backend.observe_guest(copy.deepcopy(guest)))
    exact(before,{"vmid","type","node","config_sha256","managed_disks","running","ha_managed","task_refs","process_refs","cgroup_populated","cleanup_tasks"},"guest preflight")
    require(type(before["vmid"]) is int and not isinstance(before["vmid"],bool)
            and type(before["running"]) is bool and type(before["ha_managed"]) is bool
            and type(before["cgroup_populated"]) is bool
            and all(type(before[k]) is list for k in ("managed_disks","task_refs","process_refs","cleanup_tasks"))
            and before=={"vmid":guest["vmid"],"type":guest["type"],"node":guest["node"],"config_sha256":guest["config_sha256"],"managed_disks":guest["managed_disks"],"running":True,"ha_managed":False,"task_refs":[],"process_refs":["guest-runtime"],"cgroup_populated":True,"cleanup_tasks":[]},"guest preflight drifted")
    self.authority();self.clock()
    self.clock(); self.persist("INTENT_DURABLE",index,guest); self.persist("ATTEMPTED",index,guest)
    accepted=copy.deepcopy(self.backend.submit_graceful_shutdown(copy.deepcopy(guest)))
    exact(accepted,{"upid","node","vmid","type","task_type"},"shutdown acceptance"); parsed=parse_upid(accepted["upid"]); require(type(accepted["vmid"]) is int and not isinstance(accepted["vmid"],bool)
     and all(type(accepted[k]) is str for k in ("upid","node","type","task_type"))
     and parsed=={"node":guest["node"],"task_type":("qmshutdown" if guest["type"]=="qemu" else "vzshutdown"),"vmid":guest["vmid"]}
     and accepted["node"]==guest["node"] and accepted["vmid"]==guest["vmid"] and accepted["type"]==guest["type"]
     and accepted["task_type"]==("qmshutdown" if guest["type"]=="qemu" else "vzshutdown"),"shutdown UPID binding invalid")
    self.authority()
    self.persist("TASK_ACCEPTED",index,guest,accepted)
    accepted_upid=accepted["upid"]
    terminal=copy.deepcopy(self.backend.wait_task(accepted_upid,self.plan["per_task_timeout"],self.plan["deadline"]))
    exact(terminal,{"upid","node","vmid","type","task_type","terminal","exitstatus"},"shutdown terminal receipt")
    require(type(terminal["vmid"]) is int and not isinstance(terminal["vmid"],bool)
            and type(terminal["terminal"]) is bool and all(type(terminal[k]) is str for k in ("upid","node","type","task_type","exitstatus"))
            and terminal=={**accepted,"terminal":True,"exitstatus":"OK"},"shutdown task is not a confirmed success")
    self.authority(); self.clock()
    self.persist("TASK_TERMINAL",index,guest,terminal)
    absent=copy.deepcopy(self.backend.observe_guest(copy.deepcopy(guest)))
    exact(absent,{"vmid","type","node","config_sha256","managed_disks","running","ha_managed","task_refs","process_refs","cgroup_populated","cleanup_tasks"},"guest absence receipt")
    require(type(absent["vmid"]) is int and not isinstance(absent["vmid"],bool)
            and type(absent["running"]) is bool and type(absent["ha_managed"]) is bool
            and type(absent["cgroup_populated"]) is bool
            and all(type(absent[k]) is list for k in ("managed_disks","task_refs","process_refs","cleanup_tasks"))
            and absent=={"vmid":guest["vmid"],"type":guest["type"],"node":guest["node"],
                    "config_sha256":guest["config_sha256"],"managed_disks":guest["managed_disks"],"running":False,"ha_managed":False,"task_refs":[],"process_refs":[],"cgroup_populated":False,"cleanup_tasks":[]},"guest absence is not proven")
    self.authority(); self.clock()
    self.persist("OBSERVED_ABSENT",index,guest,absent)
   validate_snapshot(self.backend.observe_cluster(),self.plan,[]); self.authority(); self.clock()
   return {"state":"MODEL_ALLOWLIST_STOPPED","runtime_authorized":False,"rollout_authorized":False,
           "release_authorized":False,"force_used":False,"mutation_performed":False}
  except Exception:
   self.poisoned=True; raise
  finally:self.running=False
