#!/usr/bin/env python3
"""Create the role-aware v2 release certificate without releasing any hold."""
import copy,hashlib,importlib.util,json
from pathlib import Path
HERE=Path(__file__).resolve().parent
def load(name,file):
 spec=importlib.util.spec_from_file_location(name,HERE/file);mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod);return mod
READY=load("slt_all_ready_v2","layout-migration-all-ready-v2.py")
STORE=load("slt_release_store_v1","layout-migration-release-certificate.py")
CTX=READY.CTX;Refusal=READY.Refusal;SHA=READY.SHA;HEX32=READY.HEX32
persist_create_only=STORE.persist_create_only
def require(v,m):
 if not v:raise Refusal(m)
def exact(v,fields,label):require(type(v) is dict and set(v)==set(fields),f"{label} fields invalid")
def canonical(v):return json.dumps(v,sort_keys=True,separators=(",",":")).encode()
def digest(v):return hashlib.sha256(canonical(v)).hexdigest()
def posint(v):return type(v) is int and not isinstance(v,bool) and v>0

def validate_release(value,context,ready,ready_sha,records,now):
 exact(value,{"schema","tx","generation","context_sha256","all_ready_plan_sha256","participant_boots","authorization_id","commit_id","coordinator","issued_at","expires_at","committed_at","release_not_after","allowed_effects"},"release authorization v2")
 names=[x["name"] for x in context["nodes"]];boots={x["name"]:x["boot_id"] for x in context["nodes"]};latest=max(x["observed_at"] for x in records)
 require(value["schema"]=="slt-layout-release-authorization/v2" and value["tx"]==context["tx"]
         and posint(value["generation"]) and value["generation"]==context["generation"]
         and value["context_sha256"]==context["context_sha256"] and value["all_ready_plan_sha256"]==ready_sha
         and value["participant_boots"]==boots and type(value["authorization_id"]) is str and HEX32.fullmatch(value["authorization_id"])
         and type(value["commit_id"]) is str and HEX32.fullmatch(value["commit_id"])
         and value["allowed_effects"]==["persist-release-certificate-v2"],"release authorization identity/effects invalid")
 exact(value["coordinator"],{"node","boot_id"},"release coordinator")
 require(value["coordinator"]["node"] in names and value["coordinator"]["boot_id"]==boots[value["coordinator"]["node"]],"release coordinator invalid")
 require(all(posint(value[k]) for k in ("issued_at","expires_at","committed_at","release_not_after"))
         and latest<=value["issued_at"]<=value["committed_at"]<=now<=value["release_not_after"]<=value["expires_at"]
         and 0<value["expires_at"]-value["issued_at"]<=900,"release authorization interval invalid")

def evaluate(context,topology_template,baseline_raw,target_raw,configured,refresh_authorization,stored_ready,release_authorization,records,now,max_age=300):
 context=CTX.validate_context(context,topology_template,baseline_raw,target_raw)
 computed=READY.evaluate(context,topology_template,baseline_raw,target_raw,configured,refresh_authorization,records,now,max_age)
 require(canonical(stored_ready)==canonical(computed),"stored ALL_READY-v2 plan differs byte-semantically from fresh evaluation")
 ready_sha=digest(computed);validate_release(release_authorization,context,computed,ready_sha,records,now);release_sha=digest(release_authorization)
 boots={x["name"]:x["boot_id"] for x in context["nodes"]}
 by_node={x["node"]:x for x in records}
 release_nodes=[node for node in computed["nodes"] if context["node_roles"][node]=="SAN_PARTICIPANT"]
 verify_only_nodes=[node for node in computed["nodes"] if context["node_roles"][node]=="CONTROL_ONLY"]
 require(len(release_nodes)==3 and len(verify_only_nodes)==1,"release role partition invalid")
 active_manifest_sha256_by_node={node:by_node[node]["hold"]["manifest_sha256"] for node in release_nodes}
 participants=[{"node":node,"boot_id":boots[node],"san_role":context["node_roles"][node],"all_ready_evidence_sha256":computed["node_evidence_sha256"][node],"active_hold":copy.deepcopy(by_node[node]["hold"])} for node in computed["nodes"]]
 certificate={"schema":"slt-layout-release-commit/v2","phase":"RELEASE_COMMITTED","commit_id":release_authorization["commit_id"],
              "tx":context["tx"],"generation":context["generation"],"context_sha256":context["context_sha256"],
              "all_configured_plan_sha256":computed["all_configured_plan_sha256"],"all_ready_plan_sha256":ready_sha,
              "refresh_authorization_sha256":computed["refresh_authorization_sha256"],"release_authorization_sha256":release_sha,
              "candidate":copy.deepcopy(context["candidate"]),"cluster_name":context["cluster_name"],
              "target_storage_cfg_sha256":context["target_storage_cfg_sha256"],"participants":participants,
              "release_nodes":release_nodes,"verify_only_nodes":verify_only_nodes,
              "active_manifest_sha256_by_node":active_manifest_sha256_by_node,
              "authorization_id":release_authorization["authorization_id"],"coordinator":copy.deepcopy(release_authorization["coordinator"]),
              "committed_at":release_authorization["committed_at"],"release_not_after":release_authorization["release_not_after"],
              "allowed_effects":["archive-exact-active-context-v2"]}
 certificate["certificate_sha256"]=digest(certificate)
 return certificate,canonical(certificate)+b"\n"
