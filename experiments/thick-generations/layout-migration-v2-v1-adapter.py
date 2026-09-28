#!/usr/bin/env python3
"""Project one explicit role-aware v2 admission into the packaged v1 contract.

This module is pure: it does not create active.json, run dpkg, edit pmxcfs or
release a hold.  Its output separates the byte-exact v1 PREPARE manifest from
the v2 role/action sidecar.  A live writer must still durably journal and
publish those bytes under the common maintenance lock.
"""

import copy,hashlib,importlib.util,json,re
from pathlib import Path

HERE=Path(__file__).resolve().parent
def load(name,file):
 spec=importlib.util.spec_from_file_location(name,HERE/file);mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod);return mod
CTX=load("slt_adapter_context_v2","layout-migration-v2-context.py")
RESTART=load("slt_adapter_restart","layout-migration-restart-preflight.py")
Refusal=CTX.Refusal;SHA=CTX.SHA;HEX32=CTX.HEX32
def require(v,m):
 if not v:raise Refusal(m)
def exact(v,fields,label):require(type(v) is dict and set(v)==set(fields),f"{label} fields invalid")
def canonical(v):return json.dumps(v,sort_keys=True,separators=(",",":")).encode()
def digest(v):return hashlib.sha256(canonical(v)).hexdigest()
def posint(v):return type(v) is int and not isinstance(v,bool) and v>0

def validate_guard(guard,required,observed_at,now,max_age):
 if required:
  exact(guard,{"service_active_state","daemon_pid","daemon_starttime","socket_inode","samples"},"active guard")
  require(guard["service_active_state"]=="active" and all(posint(guard[k]) for k in ("daemon_pid","daemon_starttime","socket_inode"))
          and guard["daemon_pid"]>1 and type(guard["samples"]) is list and len(guard["samples"])==2,"active guard lifecycle invalid")
  requests=set();previous=None
  for sample in guard["samples"]:
   exact(sample,{"request_id","observed_at","state","action","watchdog","response_sha256"},"guard sample")
   require(type(sample["request_id"]) is str and sample["request_id"] not in requests
           and type(sample["observed_at"]) is int and not isinstance(sample["observed_at"],bool)
           and 0<=now-sample["observed_at"]<=max_age and sample["observed_at"]<=observed_at
           and (previous is None or previous<=sample["observed_at"])
           and sample["state"]=="IDLE" and sample["action"]=="NONE" and sample["watchdog"]=="DISARMED"
           and type(sample["response_sha256"]) is str and SHA.fullmatch(sample["response_sha256"]),"guard sample invalid")
   requests.add(sample["request_id"]);previous=sample["observed_at"]
 else:
  exact(guard,{"service_active_state","main_pid","jobs","daemon_processes","watchdog_registrations"},"inactive guard")
  require(guard["service_active_state"]=="inactive" and type(guard["main_pid"]) is int
          and not isinstance(guard["main_pid"],bool) and guard["main_pid"]==0
          and all(type(guard[k]) is list and guard[k]==[] for k in ("jobs","daemon_processes","watchdog_registrations")),
          "inactive guard evidence is incomplete")

def project(context,topology_template,baseline_raw,target_raw,preflights,barrier,authorization,now,max_age=300):
 context=CTX.validate_context(context,topology_template,baseline_raw,target_raw)
 require(posint(now) and type(max_age) is int and not isinstance(max_age,bool) and 0<max_age<=900,"clock invalid")
 names=[x["name"] for x in context["nodes"]];boots={x["name"]:x["boot_id"] for x in context["nodes"]}
 exact(authorization,{"schema","tx","generation","context_sha256","authorization_id","issued_at","expires_at","effect"},"adapter authorization")
 require(authorization["schema"]=="slt-v1-package-adapter-authorization/v2"
         and authorization["tx"]==context["tx"] and posint(authorization["generation"])
         and authorization["generation"]==context["generation"]
         and authorization["context_sha256"]==context["context_sha256"]
         and type(authorization["authorization_id"]) is str and HEX32.fullmatch(authorization["authorization_id"])
         and posint(authorization["issued_at"]) and posint(authorization["expires_at"])
         and authorization["issued_at"]<=now<=authorization["expires_at"]
         and 0<authorization["expires_at"]-authorization["issued_at"]<=1800
         and authorization["effect"]=="project-v1-prepare-manifest","adapter authorization invalid")

 exact(barrier,{"schema","tx","generation","context_sha256","observed_at","nodes","authorization","mutation_performed"},"barrier")
 require(barrier["schema"]=="slt-live-maintenance-barrier/v2" and barrier["tx"]==context["tx"]
         and posint(barrier["generation"]) and barrier["generation"]==context["generation"]
         and barrier["context_sha256"]==context["context_sha256"]
         and type(barrier["observed_at"]) is int and not isinstance(barrier["observed_at"],bool)
         and 0<=now-barrier["observed_at"]<=max_age and barrier["authorization"]=="HOLD_ACTIVE"
         and barrier["mutation_performed"] is True,"live barrier invalid")
 require(type(barrier["nodes"]) is list and len(barrier["nodes"])==len(names),"barrier coverage invalid")
 barrier_by={}
 for row in barrier["nodes"]:
  exact(row,{"node","boot_id","evidence_sha256","barrier","workers_clear","guard_quiescent","old_consumers_absent"},"barrier node")
  require(row["node"] in names and row["node"] not in barrier_by and row["boot_id"]==boots[row["node"]]
          and type(row["evidence_sha256"]) is str and SHA.fullmatch(row["evidence_sha256"])
          and all(row[x] is True for x in ("barrier","workers_clear","guard_quiescent","old_consumers_absent")),"barrier node is not positive")
  barrier_by[row["node"]]=row
 require(sorted(barrier_by)==names,"barrier participants incomplete")

 require(type(preflights) is list and len(preflights)==len(names),"preflight coverage invalid")
 seen={};corosync=set();preflight_hashes={};recovery_by_node={}
 fields={"schema","challenge","node","nodeid","cluster_name","cluster_nodes","observed_at","corosync_conf_sha256","role","action","candidate","installed","role_evidence","workers","verdict","authorization","authorizes_mutation","mutation_performed"}
 for source in preflights:
  row=copy.deepcopy(source);restart=type(row) is dict and row.get("schema")==RESTART.SCHEMA
  exact(row,fields|({"recovery"} if restart else set()),"preflight");node=row["node"]
  expected_role=context["node_roles"].get(node);expected_action="UNPACK_CONFIGURE" if expected_role=="SAN_PARTICIPANT" else "VERIFY_CURRENT_ONLY"
  installed=row["installed"];role_evidence=row["role_evidence"];workers=row["workers"]
  require(node in names and node not in seen and row["schema"] in ("slt-layout-node-maintenance-evidence/v2",RESTART.SCHEMA)
          and type(row["challenge"]) is str and HEX32.fullmatch(row["challenge"])
          and row["cluster_name"]==context["cluster_name"] and row["cluster_nodes"]==names
          and type(row["observed_at"]) is int and not isinstance(row["observed_at"],bool) and 0<=now-row["observed_at"]<=max_age
          and type(row["corosync_conf_sha256"]) is str and SHA.fullmatch(row["corosync_conf_sha256"])
          and row["role"]==expected_role and row["action"]==expected_action and row["candidate"]==context["candidate"]
          and type(role_evidence) is dict and set(role_evidence)=={"schema","node","boot_id","san_role","topology_sha256","vg_identities","thinguard"}
          and role_evidence["schema"]=="slt-layout-node-role-evidence/v2" and role_evidence["node"]==node
          and role_evidence["boot_id"]==boots[node] and role_evidence["san_role"]==expected_role
          and role_evidence["topology_sha256"]==context["baseline_topology_sha256"]
          and type(workers) is dict and set(workers)=={"proc_inventory_complete","systemd_inventory_complete","storage_processes","blocking_transient_units","active_pve_tasks"}
          and workers["proc_inventory_complete"] is True and workers["systemd_inventory_complete"] is True
          and workers["storage_processes"]==[] and workers["blocking_transient_units"]==[]
          and workers["active_pve_tasks"]==[] and row["verdict"]=="PREFLIGHT_ONLY"
          and row["authorization"]=="NONE" and row["authorizes_mutation"] is False
          and row["mutation_performed"] is False,"preflight identity or state invalid")
  if restart:
   try:recovery_by_node[node]=RESTART.validate(row,tx=context["tx"],generation=context["generation"],baseline_sha256=context["baseline_storage_cfg_sha256"],target_sha256=context["target_storage_cfg_sha256"],now=now,max_age=max_age)
   except RESTART.Refusal as error:raise Refusal(str(error)) from error
  require(type(installed) is dict and set(installed)==({"package","version","flavor","artifact_sha256","dpkg_state"}|({"config_version"} if restart else set()))
          and installed["package"]=="pve-sharedlvmthin" and installed["flavor"]=="dual"
          and installed["dpkg_state"]==("unpacked" if restart else "installed") and type(installed["version"]) is str
          and CTX.VERSION.fullmatch(installed["version"]) and type(installed["artifact_sha256"]) is str
          and SHA.fullmatch(installed["artifact_sha256"]),"installed package identity invalid")
  validate_guard(role_evidence["thinguard"],context["thinguard_required_by_node"][node],row["observed_at"],now,max_age)
  if expected_role=="CONTROL_ONLY":
   require(role_evidence["vg_identities"]==[] and installed["version"]==context["candidate"]["version"]
           and installed["artifact_sha256"]==context["candidate"]["artifact_sha256"],"control-only payload is not exact")
  else:require(role_evidence["vg_identities"]==context["expected_vg_identities_by_node"][node],"SAN VG identities differ")
  seen[node]=row;corosync.add(row["corosync_conf_sha256"]);preflight_hashes[node]=digest(row)
 require(sorted(seen)==names and len(corosync)==1 and len({x["challenge"] for x in seen.values()})==len(names),"preflight set is inconsistent")
 if recovery_by_node:
  require(sorted(recovery_by_node)==[name for name in names if context["node_roles"][name]=="SAN_PARTICIPANT"]
          and len({(row["predecessor_tx"],row["predecessor_generation"]) for row in recovery_by_node.values()})==1,
          "restart must cover all SAN nodes from one exact predecessor transaction")

 action_plan=[{"node":name,"role":context["node_roles"][name],"action":("UNPACK_CONFIGURE" if context["node_roles"][name]=="SAN_PARTICIPANT" else "VERIFY_CURRENT_ONLY")} for name in names]
 require(sum(x["action"]=="VERIFY_CURRENT_ONLY" for x in action_plan)==1,"verify-only action count invalid")
 plan={"schema":"slt-v2-v1-package-plan/v2","tx":context["tx"],"generation":context["generation"],
       "context_sha256":context["context_sha256"],"actions":action_plan,
       "preflight_sha256":preflight_hashes,"barrier_sha256":digest(barrier),
       "authorization_sha256":digest(authorization)}
 if recovery_by_node:plan["recovery_by_node"]=recovery_by_node
 v1_candidate={k:context["candidate"][k] for k in ("package","version","flavor","artifact_sha256","deb_sha256")}
 evidence=[{"node":name,"challenge":seen[name]["challenge"],"evidence_sha256":barrier_by[name]["evidence_sha256"],
            "barrier":True,"workers_clear":True,"guard_idle_disarmed":True,"old_consumers_absent":True} for name in names]
 manifest={"schema":"slt-package-maintenance/v1","tx":context["tx"],"phase":"PREPARE_READY","generation":context["generation"],
           "issued_at":authorization["issued_at"],"expires_at":authorization["expires_at"],"cluster_name":context["cluster_name"],
           "corosync_conf_sha256":next(iter(corosync)),"nodes":[{"name":name,"boot_id":boots[name]} for name in names],
           "candidate":v1_candidate,"baseline_storage_cfg_sha256":context["baseline_storage_cfg_sha256"],
           "target_storage_cfg_sha256":context["target_storage_cfg_sha256"],"allowed_effects":["package-unpack","package-configure-deferred"],
           "plan_sha256":digest(plan),"node_evidence":evidence}
 sidecar={"schema":"slt-v2-v1-package-adapter/v2","tx":context["tx"],"generation":context["generation"],
          "context_sha256":context["context_sha256"],"manifest_sha256":hashlib.sha256(canonical(manifest)).hexdigest(),
          "plan":plan,"actions":action_plan,"authorization":"PROJECTED_ONLY","mutation_performed":False}
 return {"manifest":manifest,"sidecar":sidecar}
