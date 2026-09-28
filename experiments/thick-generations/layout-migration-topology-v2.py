#!/usr/bin/env python3
"""Role-aware four-node topology contract for migration schema v2.

This evaluator is read-only and unpackaged.  It does not upgrade v1 evidence or
authorize maintenance.  It binds explicit SAN/control roles to storage.cfg.
"""

import copy,hashlib,json,re,uuid

SAFE=re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
SHA=re.compile(r"^[0-9a-f]{64}$")
ROLES={"SAN_PARTICIPANT","CONTROL_ONLY"}
class Refusal(Exception):pass
def require(v,m):
 if not v:raise Refusal(m)
def exact(v,f,l):require(type(v) is dict and set(v)==set(f),f"{l} fields invalid")
def canonical(v):return json.dumps(v,sort_keys=True,separators=(",",":")).encode()
def digest(v):return hashlib.sha256(canonical(v)).hexdigest()

def validate_identity(cluster_name,storage_cfg_sha256):
 require(type(cluster_name) is str and SAFE.fullmatch(cluster_name)
         and type(storage_cfg_sha256) is str and SHA.fullmatch(storage_cfg_sha256),"topology identity invalid")

def validate_nodes(nodes,label="topology"):
 require(type(nodes) is list and len(nodes)==4,f"{label} requires four nodes")
 names=[];roles={}
 for row in nodes:
  exact(row,{"name","boot_id","san_role"},f"{label} node")
  try:boot=str(uuid.UUID(row["boot_id"]))
  except (ValueError,TypeError,AttributeError) as e:raise Refusal("node boot identity invalid") from e
  require(type(row["name"]) is str and SAFE.fullmatch(row["name"]) and row["boot_id"]==boot
          and row["san_role"] in ROLES,f"{label} node invalid")
  names.append(row["name"]);roles[row["name"]]=row["san_role"]
 require(names==sorted(set(names)),f"{label} nodes duplicated or unordered")
 san={n for n,r in roles.items() if r=="SAN_PARTICIPANT"};control={n for n,r in roles.items() if r=="CONTROL_ONLY"}
 require(len(san)==3 and len(control)==1,"topology must contain three SAN and one control-only node")
 return roles,san,control

def validate_scopes(scopes,san,control):
 require(type(scopes) is list and scopes,"managed storage scopes missing")
 ids=[]
 for item in scopes:
  exact(item,{"storage_id","nodes","thinguard_mode"},"storage scope")
  require(type(item["storage_id"]) is str and SAFE.fullmatch(item["storage_id"]),"storage identity invalid")
  nodes=item["nodes"]
  require(type(nodes) is list and nodes and all(type(x) is str and SAFE.fullmatch(x) for x in nodes)
          and nodes==sorted(set(nodes)) and set(nodes)<=san and not set(nodes)&control
          and item["thinguard_mode"] in {"disabled","remote-audit","runtime-guard"},"evidence storage scope invalid")
  ids.append(item["storage_id"])
 require(ids==sorted(set(ids)),"storage scopes duplicated or unordered")

def parse_storage(raw):
 text=raw.decode("utf-8");require(text.endswith("\n") and "\0" not in text,"storage config framing invalid")
 blocks=[];current=None
 for line in text.splitlines():
  if line and not line[0].isspace() and ":" in line:
   if current:blocks.append(current)
   kind,sid=line.split(":",1);current={"kind":kind,"sid":sid.strip(),"properties":{}}
  elif current and line[:1].isspace():
   item=line.strip()
   if not item or item.startswith("#"):continue
   fields=item.split(None,1);require(fields[0] not in current["properties"],"storage property duplicated")
   current["properties"][fields[0]]=fields[1].strip() if len(fields)==2 else ""
 if current:blocks.append(current)
 return blocks

def validate(topology,storage_raw):
 exact(topology,{"schema","cluster_name","nodes","storage_cfg_sha256"},"topology")
 validate_identity(topology["cluster_name"],topology["storage_cfg_sha256"])
 require(topology["schema"]=="slt-layout-topology/v2"
         and topology["storage_cfg_sha256"]==hashlib.sha256(storage_raw).hexdigest(),"topology identity invalid")
 nodes=topology["nodes"];roles,san,control=validate_nodes(nodes)
 managed=[]
 for block in parse_storage(storage_raw):
  if block["kind"]!="sharedlvmthin":continue
  require(SAFE.fullmatch(block["sid"] or ""),"managed storage identity invalid")
  raw_nodes=block["properties"].get("nodes")
  require(type(raw_nodes) is str and raw_nodes.strip(),f"managed storage '{block['sid']}' is global")
  listed=[x.strip() for x in raw_nodes.split(",") if x.strip()];scoped=set(listed)
  require(listed and len(listed)==len(scoped) and scoped<=san and not scoped&control,f"managed storage '{block['sid']}' scope violates topology")
  guard_mode=block["properties"].get("slt-thin-leaseguard","disabled")
  require(guard_mode in {"disabled","remote-audit","runtime-guard"},f"managed storage '{block['sid']}' has invalid ThinGuard mode")
  managed.append({"storage_id":block["sid"],"nodes":sorted(scoped),"thinguard_mode":guard_mode})
 require(managed,"no managed storage definitions found")
 validate_scopes(sorted(managed,key=lambda x:x["storage_id"]),san,control)
 guard_required={name:any(item["thinguard_mode"]=="runtime-guard" and name in item["nodes"] for item in managed) for name in sorted(roles)}
 body={"schema":"slt-layout-topology-evidence/v2","cluster_name":topology["cluster_name"],
       "nodes":copy.deepcopy(nodes),"managed_storage_scopes":copy.deepcopy(sorted(managed,key=lambda x:x["storage_id"])),
       "thinguard_required_by_node":guard_required,
       "storage_cfg_sha256":topology["storage_cfg_sha256"],"topology_sha256":digest(topology),
       "verdict":"TOPOLOGY_BOUND","authorization":"NONE","mutation_performed":False}
 body["evidence_sha256"]=digest(body);return body

def validate_topology_evidence(value):
 exact(value,{"schema","cluster_name","nodes","managed_storage_scopes","thinguard_required_by_node","storage_cfg_sha256","topology_sha256","verdict","authorization","mutation_performed","evidence_sha256"},"topology evidence")
 body=copy.deepcopy(value);claimed=body.pop("evidence_sha256")
 require(SHA.fullmatch(claimed or "") and claimed==digest(body)
         and value["schema"]=="slt-layout-topology-evidence/v2" and value["verdict"]=="TOPOLOGY_BOUND"
         and value["authorization"]=="NONE" and value["mutation_performed"] is False,"topology evidence identity invalid")
 validate_identity(value["cluster_name"],value["storage_cfg_sha256"])
 topology={"schema":"slt-layout-topology/v2","cluster_name":value["cluster_name"],"nodes":value["nodes"],"storage_cfg_sha256":value["storage_cfg_sha256"]}
 require(value["topology_sha256"]==digest(topology),"topology digest is not reproducible")
 roles,san,control=validate_nodes(value["nodes"],"evidence topology")
 validate_scopes(value["managed_storage_scopes"],san,control)
 expected_guard={name:any(item["thinguard_mode"]=="runtime-guard" and name in item["nodes"] for item in value["managed_storage_scopes"]) for name in sorted(roles)}
 require(value["thinguard_required_by_node"]==expected_guard and all(value["thinguard_required_by_node"][name] is False for name in control),"ThinGuard policy differs from storage scopes")
 return copy.deepcopy(value)

def bind_node_evidence(record,topology_evidence):
 topology_evidence=validate_topology_evidence(topology_evidence)
 exact(record,{"schema","node","boot_id","san_role","topology_sha256","vg_identities","thinguard"},"node role evidence")
 require(record["schema"]=="slt-layout-node-role-evidence/v2" and record["topology_sha256"]==topology_evidence["topology_sha256"],"node evidence topology mismatch")
 expected=next((x for x in topology_evidence["nodes"] if x["name"]==record["node"]),None)
 require(expected is not None and record["boot_id"]==expected["boot_id"] and record["san_role"]==expected["san_role"],"node role or boot differs")
 guard_required=topology_evidence["thinguard_required_by_node"][record["node"]]
 if record["san_role"]=="SAN_PARTICIPANT" and guard_required:
  guard=record["thinguard"]
  require(type(record["vg_identities"]) is list and record["vg_identities"]
          and type(guard) is dict and set(guard)=={"service_active_state","daemon_pid","daemon_starttime","socket_inode","samples"}
          and guard["service_active_state"]=="active" and type(guard["daemon_pid"]) is int and guard["daemon_pid"]>1
          and type(guard["daemon_starttime"]) is int and guard["daemon_starttime"]>0
          and type(guard["socket_inode"]) is int and guard["socket_inode"]>0
          and type(guard["samples"]) is list and len(guard["samples"])==2
          and all(type(x) is dict and x.get("state")=="IDLE" and x.get("watchdog")=="DISARMED" for x in guard["samples"]),"SAN evidence incomplete")
 else:
  guard=record["thinguard"]
  vg_shape=(type(record["vg_identities"]) is list and bool(record["vg_identities"])) if record["san_role"]=="SAN_PARTICIPANT" else record["vg_identities"]==[]
  require(vg_shape and type(guard) is dict
          and set(guard)=={"service_active_state","main_pid","jobs","daemon_processes","watchdog_registrations"}
          and guard["service_active_state"]=="inactive" and type(guard["main_pid"]) is int and not isinstance(guard["main_pid"],bool) and guard["main_pid"]==0
          and all(type(guard[k]) is list and guard[k]==[] for k in ("jobs","daemon_processes","watchdog_registrations")),"control-only SAN evidence is not explicitly inapplicable")
 return record
