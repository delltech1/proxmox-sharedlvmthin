#!/usr/bin/env python3
"""Build the immutable, non-authorizing role-aware migration context.

This is an unpackaged composition boundary.  It re-derives both topology
records from the exact baseline/target storage.cfg bytes and refuses any
target other than the deterministic legacy-layout change.  It performs no I/O
and grants no package, maintenance, refresh, release or storage authority.
"""

import copy,hashlib,importlib.util,json,re
from pathlib import Path

HERE=Path(__file__).resolve().parent
def load(name,file):
 spec=importlib.util.spec_from_file_location(name,HERE/file);mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod);return mod
TOPO=load("slt_topology_v2","layout-migration-topology-v2.py")
PLAN=load("slt_layout_plan_v1_leaf","layout-migration-plan.py")
NODE=load("slt_layout_node_v1_leaf","layout-migration-node-evidence.py")

Refusal=TOPO.Refusal
SAFE=TOPO.SAFE;SHA=TOPO.SHA
HEX32=re.compile(r"^[0-9a-f]{32}$")
VERSION=re.compile(r"^[A-Za-z0-9.+:~_-]+$")
def require(v,m):
 if not v:raise Refusal(m)
def exact(v,fields,label):require(type(v) is dict and set(v)==set(fields),f"{label} fields invalid")
def canonical(v):return json.dumps(v,sort_keys=True,separators=(",",":")).encode()
def digest(v):return hashlib.sha256(canonical(v)).hexdigest()

def topology_for(template,raw):
 exact(template,{"schema","cluster_name","nodes"},"topology template")
 value={"schema":"slt-layout-topology/v2","cluster_name":template["cluster_name"],
        "nodes":copy.deepcopy(template["nodes"]),"storage_cfg_sha256":hashlib.sha256(raw).hexdigest()}
 return value,TOPO.validate(value,raw)

def validate_candidate(candidate):
 exact(candidate,{"package","version","flavor","architecture","deb_sha256","artifact_sha256"},"candidate")
 require(candidate["package"]=="pve-sharedlvmthin" and candidate["flavor"]=="dual"
         and candidate["architecture"]=="all" and type(candidate["version"]) is str
         and VERSION.fullmatch(candidate["version"])
         and all(type(candidate[k]) is str and SHA.fullmatch(candidate[k]) for k in ("deb_sha256","artifact_sha256")),"candidate invalid")

def build_context(transaction,candidate,topology_template,baseline_raw,target_raw):
 exact(transaction,{"tx","generation"},"transaction")
 require(type(transaction["tx"]) is str and HEX32.fullmatch(transaction["tx"])
         and type(transaction["generation"]) is int and not isinstance(transaction["generation"],bool)
         and transaction["generation"]>0,"transaction invalid")
 validate_candidate(candidate)
 require(type(baseline_raw) is bytes and type(target_raw) is bytes and baseline_raw!=target_raw,"configuration pair invalid")
 try:changes=PLAN.validate_target_layout(baseline_raw,target_raw)
 except PLAN.Refusal as error:raise Refusal(str(error)) from error
 baseline_topology,baseline_evidence=topology_for(topology_template,baseline_raw)
 target_topology,target_evidence=topology_for(topology_template,target_raw)
 require(baseline_topology["nodes"]==target_topology["nodes"]
         and baseline_evidence["managed_storage_scopes"]==target_evidence["managed_storage_scopes"],"target changed topology or storage scopes")
 san=sorted(row["name"] for row in target_topology["nodes"] if row["san_role"]=="SAN_PARTICIPANT")
 by_vg={}
 for block in PLAN.parse_storage_config(target_raw):
  if block["kind"]!="sharedlvmthin":continue
  props=block["properties"];vg=props.get("slt-vgname",props.get("vgname",""));mode=props.get("slt-allocation-mode","thin")
  if mode!="thin" and mode not in PLAN.THICK_MODES:continue
  scope=sorted(x.strip() for x in props.get("nodes","").split(",") if x.strip())
  by_vg.setdefault(vg,[]).append((mode,scope))
 for vg,aliases in by_vg.items():
  if not (any(mode=="thin" for mode,_ in aliases) and any(mode in PLAN.THICK_MODES for mode,_ in aliases)):continue
  require(len(aliases)==2 and aliases[0][1]==aliases[1][1]==san,
          f"mixed VG '{vg}' aliases must share the exact complete SAN scope")
 identities=NODE.expected_mixed_vgs(target_raw)
 names=[row["name"] for row in target_topology["nodes"]]
 roles={row["name"]:row["san_role"] for row in target_topology["nodes"]}
 per_node={name:(copy.deepcopy(identities) if roles[name]=="SAN_PARTICIPANT" else []) for name in names}
 body={"schema":"slt-layout-v2-context/v2","tx":transaction["tx"],"generation":transaction["generation"],
       "candidate":copy.deepcopy(candidate),"cluster_name":target_topology["cluster_name"],
       "nodes":copy.deepcopy(target_topology["nodes"]),"node_roles":roles,
       "baseline_storage_cfg_sha256":baseline_topology["storage_cfg_sha256"],
       "target_storage_cfg_sha256":target_topology["storage_cfg_sha256"],
       "baseline_topology_sha256":baseline_evidence["topology_sha256"],
       "target_topology_sha256":target_evidence["topology_sha256"],
       "baseline_topology_evidence_sha256":baseline_evidence["evidence_sha256"],
       "target_topology_evidence_sha256":target_evidence["evidence_sha256"],
       "managed_storage_scopes":copy.deepcopy(target_evidence["managed_storage_scopes"]),
       "thinguard_required_by_node":copy.deepcopy(target_evidence["thinguard_required_by_node"]),
       "expected_vg_identities_by_node":per_node,"changes":copy.deepcopy(changes),
       "authorization":"NONE","mutation_performed":False}
 body["context_sha256"]=digest(body);return body

def validate_context(value,topology_template,baseline_raw,target_raw):
 exact(value,{"schema","tx","generation","candidate","cluster_name","nodes","node_roles",
              "baseline_storage_cfg_sha256","target_storage_cfg_sha256","baseline_topology_sha256",
              "target_topology_sha256","baseline_topology_evidence_sha256","target_topology_evidence_sha256",
              "managed_storage_scopes","thinguard_required_by_node","expected_vg_identities_by_node","changes","authorization",
              "mutation_performed","context_sha256"},"v2 context")
 claimed=value["context_sha256"];body=copy.deepcopy(value);body.pop("context_sha256")
 require(type(claimed) is str and SHA.fullmatch(claimed) and claimed==digest(body),"context self-identity invalid")
 rebuilt=build_context({"tx":value["tx"],"generation":value["generation"]},value["candidate"],topology_template,baseline_raw,target_raw)
 require(value==rebuilt,"context differs from exact configuration-derived context")
 return copy.deepcopy(value)
