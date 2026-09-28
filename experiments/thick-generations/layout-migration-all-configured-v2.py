#!/usr/bin/env python3
"""Offline role-aware ALL_CONFIGURED-v2 evaluator (authorization NONE)."""
import copy,hashlib,importlib.util,json,re,uuid
from pathlib import Path
HERE=Path(__file__).resolve().parent
def load(name,file):
 spec=importlib.util.spec_from_file_location(name,HERE/file);mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod);return mod
CTX=load("slt_context_v2","layout-migration-v2-context.py")
Refusal=CTX.Refusal;SHA=CTX.SHA;HEX32=CTX.HEX32
def require(v,m):
 if not v:raise Refusal(m)
def exact(v,fields,label):require(type(v) is dict and set(v)==set(fields),f"{label} fields invalid")
def canonical(v):return json.dumps(v,sort_keys=True,separators=(",",":")).encode()
def digest(v):return hashlib.sha256(canonical(v)).hexdigest()

def validate_workers(value):
 exact(value,{"inventory_complete","storage_processes","transient_units","pve_tasks"},"workers")
 require(value["inventory_complete"] is True and all(type(value[k]) is list and value[k]==[] for k in ("storage_processes","transient_units","pve_tasks")),"worker inventory is incomplete or busy")

def validate_receipt(value,record,context,role):
 if role=="CONTROL_ONLY":
  exact(value,{"schema","phase","tx","generation","node","boot_id","context_sha256","candidate","target_storage_cfg_sha256","recorded_at","dpkg_state","payload_verified"},"control payload receipt")
  require(value["schema"]=="slt-current-payload-verified/v2" and value["phase"]=="CURRENT_PAYLOAD_VERIFIED"
         and value["dpkg_state"]=="installed" and value["payload_verified"] is True,
         "control-only payload verification is invalid")
 else:
  exact(value,{"schema","phase","tx","generation","node","boot_id","context_sha256","candidate","target_storage_cfg_sha256","recorded_at"},"configured receipt")
  require(value["schema"]=="slt-package-maintenance-receipt/v2" and value["phase"]=="PACKAGE_CONFIGURED_DEFERRED",
          "SAN configured receipt type is invalid")
 require(value["tx"]==context["tx"] and type(value["generation"]) is int and not isinstance(value["generation"],bool)
         and value["generation"]>0 and value["generation"]==context["generation"]
         and value["node"]==record["node"] and value["boot_id"]==record["boot_id"]
         and value["context_sha256"]==context["context_sha256"] and value["candidate"]==context["candidate"]
         and value["target_storage_cfg_sha256"]==context["target_storage_cfg_sha256"]
         and type(value["recorded_at"]) is int and not isinstance(value["recorded_at"],bool)
         and 0<value["recorded_at"]<=record["observed_at"],"completion receipt identity invalid")

def validate_guard(record,role,expected_vgs,guard_required,now,max_age,not_before):
 guard=record["thinguard"]
 require(record["vg_identities"]==expected_vgs,"physical identities differ")
 if not guard_required:
  exact(guard,{"service_active_state","main_pid","jobs","daemon_processes","watchdog_registrations"},"control guard")
  require(guard["service_active_state"]=="inactive"
          and type(guard["main_pid"]) is int and not isinstance(guard["main_pid"],bool) and guard["main_pid"]==0
          and all(type(guard[k]) is list and guard[k]==[] for k in ("jobs","daemon_processes","watchdog_registrations")),"control-only SAN state is not explicitly inapplicable")
  return
 require(role=="SAN_PARTICIPANT","control-only node cannot require ThinGuard")
 exact(guard,{"service_active_state","daemon_pid","daemon_starttime","socket_inode","samples"},"SAN guard")
 require(guard["service_active_state"]=="active"
         and all(type(guard[k]) is int and not isinstance(guard[k],bool) and guard[k]>0 for k in ("daemon_pid","daemon_starttime","socket_inode"))
         and guard["daemon_pid"]>1 and type(guard["samples"]) is list and len(guard["samples"])==2,"SAN guard lifecycle invalid")
 ids=[];times=[]
 for sample in guard["samples"]:
  exact(sample,{"request_id","observed_at","state","action","watchdog","response_sha256"},"guard sample")
  try:rid=str(uuid.UUID(sample["request_id"]))
  except (ValueError,TypeError,AttributeError) as e:raise Refusal("guard request identity invalid") from e
  require(sample["request_id"]==rid and type(sample["observed_at"]) is int and not isinstance(sample["observed_at"],bool)
          and 0<=now-sample["observed_at"]<=max_age and sample["state"]=="IDLE" and sample["action"]=="NONE"
          and sample["watchdog"]=="DISARMED" and type(sample["response_sha256"]) is str and SHA.fullmatch(sample["response_sha256"]),"guard sample invalid")
  ids.append(rid);times.append(sample["observed_at"])
 require(len(set(ids))==2 and not_before<=times[0]<times[1]<=record["observed_at"],"guard samples are duplicated, unordered or precede the required boundary")

def evaluate(context,topology_template,baseline_raw,target_raw,records,now,max_age=300):
 context=CTX.validate_context(context,topology_template,baseline_raw,target_raw)
 require(type(now) is int and not isinstance(now,bool) and type(max_age) is int and not isinstance(max_age,bool) and 0<max_age<=900,"evaluation clock invalid")
 names=[row["name"] for row in context["nodes"]];boots={row["name"]:row["boot_id"] for row in context["nodes"]}
 require(type(records) is list and len(records)==len(names),"exactly one record per participant required")
 seen={};hashes={}
 fields={"schema","challenge","observed_at","tx","generation","cluster_name","node","boot_id","cluster_nodes","quorate","target_storage_cfg_sha256","context_sha256","candidate","receipt","workers","thinguard","vg_identities"}
 for source in records:
  record=copy.deepcopy(source);exact(record,fields,"ALL_CONFIGURED-v2 node")
  node=record["node"]
  require(record["schema"]=="slt-layout-all-configured-node/v2" and node in names and node not in seen
          and type(record["challenge"]) is str and HEX32.fullmatch(record["challenge"])
          and type(record["observed_at"]) is int and not isinstance(record["observed_at"],bool) and 0<=now-record["observed_at"]<=max_age
          and record["tx"]==context["tx"] and type(record["generation"]) is int and not isinstance(record["generation"],bool)
          and record["generation"]>0 and record["generation"]==context["generation"]
          and record["cluster_name"]==context["cluster_name"] and record["boot_id"]==boots[node]
          and record["cluster_nodes"]==names and record["quorate"] is True
          and record["target_storage_cfg_sha256"]==context["target_storage_cfg_sha256"]
          and record["context_sha256"]==context["context_sha256"] and record["candidate"]==context["candidate"],"node transaction/control-plane identity invalid")
  role=context["node_roles"][node];validate_receipt(record["receipt"],record,context,role);validate_workers(record["workers"])
  validate_guard(record,role,context["expected_vg_identities_by_node"][node],context["thinguard_required_by_node"][node],now,max_age,record["receipt"]["recorded_at"])
  seen[node]=record;hashes[node]=digest(record)
 require(sorted(seen)==names and len(set(item["challenge"] for item in seen.values()))==len(names),"participant evidence incomplete or challenges duplicated")
 body={"schema":"slt-layout-all-configured-plan/v2","phase":"ALL_CONFIGURED","verdict":"READY_FOR_REFRESH_PLAN",
       "authorization":"NONE","mutation_performed":False,"tx":context["tx"],"generation":context["generation"],
       "context_sha256":context["context_sha256"],"target_storage_cfg_sha256":context["target_storage_cfg_sha256"],
       "nodes":names,"node_roles":copy.deepcopy(context["node_roles"]),"node_evidence_sha256":hashes,
       "next_phase":"EXPLICIT_REFRESH_AUTHORIZATION_V2_REQUIRED"}
 body["plan_sha256"]=digest(body);return body
