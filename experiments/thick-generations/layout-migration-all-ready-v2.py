#!/usr/bin/env python3
"""Offline role-aware ALL_READY-v2 evaluator; never releases the hold."""
import copy,hashlib,importlib.util,json,re,uuid
from pathlib import Path
HERE=Path(__file__).resolve().parent
def load(name,file):
 spec=importlib.util.spec_from_file_location(name,HERE/file);mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod);return mod
AC=load("slt_all_configured_v2","layout-migration-all-configured-v2.py")
CTX=AC.CTX;Refusal=AC.Refusal;SHA=AC.SHA;HEX32=AC.HEX32
UNITS={"pve-sharedlvmthin-thin-guard.service","pvedaemon.service","pvestatd.service","pveproxy.service","pve-ha-lrm.service"}
def require(v,m):
 if not v:raise Refusal(m)
def exact(v,fields,label):require(type(v) is dict and set(v)==set(fields),f"{label} fields invalid")
def canonical(v):return json.dumps(v,sort_keys=True,separators=(",",":")).encode()
def digest(v):return hashlib.sha256(canonical(v)).hexdigest()
def posint(v):return type(v) is int and not isinstance(v,bool) and v>0

def validate_configured(plan,context):
 exact(plan,{"schema","phase","verdict","authorization","mutation_performed","tx","generation","context_sha256","target_storage_cfg_sha256","nodes","node_roles","node_evidence_sha256","next_phase","plan_sha256"},"ALL_CONFIGURED-v2 plan")
 body=copy.deepcopy(plan);claimed=body.pop("plan_sha256")
 names=[x["name"] for x in context["nodes"]]
 require(plan["schema"]=="slt-layout-all-configured-plan/v2" and plan["phase"]=="ALL_CONFIGURED"
         and plan["verdict"]=="READY_FOR_REFRESH_PLAN" and plan["authorization"]=="NONE" and plan["mutation_performed"] is False
         and plan["tx"]==context["tx"] and posint(plan["generation"]) and plan["generation"]==context["generation"]
         and plan["context_sha256"]==context["context_sha256"] and plan["target_storage_cfg_sha256"]==context["target_storage_cfg_sha256"]
         and plan["nodes"]==names and plan["node_roles"]==context["node_roles"]
         and type(plan["node_evidence_sha256"]) is dict and sorted(plan["node_evidence_sha256"])==names
         and all(type(v) is str and SHA.fullmatch(v) for v in plan["node_evidence_sha256"].values())
         and plan["next_phase"]=="EXPLICIT_REFRESH_AUTHORIZATION_V2_REQUIRED"
         and type(claimed) is str and SHA.fullmatch(claimed) and claimed==digest(body),"ALL_CONFIGURED-v2 plan invalid")

def lifecycle(value,active,label):
 exact(value,{"active","pid","starttime"},label)
 require(value["active"] is active and type(value["pid"]) is int and not isinstance(value["pid"],bool)
         and type(value["starttime"]) is int and not isinstance(value["starttime"],bool),f"{label} type/activity invalid")
 require((value["pid"]>1 and value["starttime"]>0) if active else (value["pid"]==0 and value["starttime"]==0),f"{label} lifecycle invalid")

def validate_authorization(value,context,configured,now):
 exact(value,{"schema","tx","generation","context_sha256","all_configured_plan_sha256","authorization_id","issued_at","expires_at","node_order","node_roles","challenges","service_plan"},"refresh authorization v2")
 names=[x["name"] for x in context["nodes"]]
 require(value["schema"]=="slt-layout-refresh-authorization/v2" and value["tx"]==context["tx"]
         and posint(value["generation"]) and value["generation"]==context["generation"]
         and value["context_sha256"]==context["context_sha256"] and value["all_configured_plan_sha256"]==configured["plan_sha256"]
         and type(value["authorization_id"]) is str and HEX32.fullmatch(value["authorization_id"])
         and posint(value["issued_at"]) and posint(value["expires_at"]) and value["issued_at"]<=now<=value["expires_at"]
         and 0<value["expires_at"]-value["issued_at"]<=1800 and value["node_order"]==names and value["node_roles"]==context["node_roles"],"refresh authorization identity/time invalid")
 require(type(value["challenges"]) is dict and sorted(value["challenges"])==names and len(set(value["challenges"].values()))==len(names)
         and all(type(x) is str and HEX32.fullmatch(x) for x in value["challenges"].values()),"refresh challenges invalid")
 require(type(value["service_plan"]) is list and len(value["service_plan"])==len(names),"service plan incomplete")
 plans={}
 for row in value["service_plan"]:
  exact(row,{"node","units"},"node service plan");node=row["node"]
  require(node in names and node not in plans and type(row["units"]) is list and len(row["units"])==len(UNITS),"node service plan invalid")
  units={}
  for unit in row["units"]:
   exact(unit,{"unit","operation","was_active","before"},"service intent");name=unit["unit"]
   require(name in UNITS and name not in units and type(unit["was_active"]) is bool,"service intent invalid")
   lifecycle(unit["before"],unit["was_active"],"authorized before lifecycle")
   expected="verify-current-only" if context["node_roles"][node]=="CONTROL_ONLY" else ("restart-active" if name=="pve-sharedlvmthin-thin-guard.service" and context["thinguard_required_by_node"][node] else ("preserve-inactive" if name=="pve-sharedlvmthin-thin-guard.service" else "try-restart-active"))
   role_active=True if expected=="verify-current-only" else ((unit["was_active"] is True) if expected=="restart-active" else ((unit["was_active"] is False) if expected=="preserve-inactive" else True))
   require(unit["operation"]==expected and role_active,"service intent contradicts node role")
   units[name]=unit
  require(set(units)==UNITS,"service plan units differ");plans[node]=units
 require(list(plans)==names,"service plan order differs")
 return plans

def validate_post_guard(record,context,now,max_age,not_before):
 role=context["node_roles"][record["node"]]
 if role=="CONTROL_ONLY":
  AC.validate_guard(record,role,[],False,now,max_age,not_before);return
 AC.validate_guard(record,role,context["expected_vg_identities_by_node"][record["node"]],context["thinguard_required_by_node"][record["node"]],now,max_age,not_before)

def evaluate(context,topology_template,baseline_raw,target_raw,configured,authorization,records,now,max_age=300):
 context=CTX.validate_context(context,topology_template,baseline_raw,target_raw);validate_configured(configured,context)
 require(posint(now) and type(max_age) is int and not isinstance(max_age,bool) and 0<max_age<=900,"evaluation clock invalid")
 plans=validate_authorization(authorization,context,configured,now);auth_sha=digest(authorization)
 names=[x["name"] for x in context["nodes"]];boots={x["name"]:x["boot_id"] for x in context["nodes"]}
 require(type(records) is list and len(records)==len(names),"exactly one ALL_READY record per participant required")
 seen={};hashes={};fields={"schema","challenge","authorization_sha256","observed_at","tx","generation","cluster_name","node","boot_id","cluster_nodes","quorate","target_storage_cfg_sha256","context_sha256","candidate","workers","hold","refresh","thinguard","vg_identities"}
 for source in records:
  record=copy.deepcopy(source);exact(record,fields,"ALL_READY-v2 node");node=record["node"]
  require(record["schema"]=="slt-layout-all-ready-node/v2" and node in names and node not in seen
          and record["challenge"]==authorization["challenges"][node] and record["authorization_sha256"]==auth_sha
          and type(record["observed_at"]) is int and not isinstance(record["observed_at"],bool) and 0<=now-record["observed_at"]<=max_age
          and record["tx"]==context["tx"] and posint(record["generation"]) and record["generation"]==context["generation"]
          and record["cluster_name"]==context["cluster_name"] and record["boot_id"]==boots[node]
          and record["cluster_nodes"]==names and record["quorate"] is True
          and record["target_storage_cfg_sha256"]==context["target_storage_cfg_sha256"]
          and record["context_sha256"]==context["context_sha256"] and record["candidate"]==context["candidate"],"ALL_READY node identity invalid")
  AC.validate_workers(record["workers"]);exact(record["hold"],{"active","context_sha256","manifest_sha256","device","inode"},"hold")
  hold=record["hold"]
  require(hold["context_sha256"]==context["context_sha256"],"maintenance hold context changed")
  if context["node_roles"][node]=="CONTROL_ONLY":
   require(hold=={"active":False,"context_sha256":context["context_sha256"],"manifest_sha256":"0"*64,"device":0,"inode":0},"control-only node fabricated a maintenance hold")
  else:
   require(hold["active"] is True and type(hold["manifest_sha256"]) is str and SHA.fullmatch(hold["manifest_sha256"])
           and posint(hold["device"]) and posint(hold["inode"]),"SAN maintenance hold identity is absent or invalid")
  refresh=record["refresh"];exact(refresh,{"completed_at","units"},"refresh result")
  require(posint(refresh["completed_at"]) and authorization["issued_at"]<=refresh["completed_at"]<=record["observed_at"]
          and type(refresh["units"]) is list and len(refresh["units"])==len(UNITS),"refresh completion invalid")
  results={}
  for item in refresh["units"]:
   exact(item,{"unit","operation","before","after","result"},"unit refresh result");name=item["unit"]
   require(name in plans[node] and name not in results and item["operation"]==plans[node][name]["operation"]
           and item["before"]==plans[node][name]["before"],"unit result differs from authorization")
   was=plans[node][name]["was_active"];lifecycle(item["before"],was,"refresh before");lifecycle(item["after"],was,"refresh after")
   if context["node_roles"][node]=="CONTROL_ONLY":require(item["after"]==item["before"] and item["result"]=="VERIFIED_UNCHANGED","control-only service was changed or not positively verified")
   elif was:require(item["after"]!=item["before"] and item["result"]=="RESTARTED_ACTIVE","active unit was not positively restarted")
   else:require(item["after"]==item["before"] and item["result"]=="PRESERVED_INACTIVE","inactive unit was not preserved")
   results[name]=item
  require(set(results)==UNITS,"refresh results incomplete")
  if context["thinguard_required_by_node"][node]:
   after=results["pve-sharedlvmthin-thin-guard.service"]["after"]
   require(record["thinguard"].get("daemon_pid")==after["pid"]
           and record["thinguard"].get("daemon_starttime")==after["starttime"],"post-refresh ThinGuard identity differs from restarted lifecycle")
  validate_post_guard(record,context,now,max_age,refresh["completed_at"])
  seen[node]=record;hashes[node]=digest(record)
 require(sorted(seen)==names,"ALL_READY participants incomplete")
 body={"schema":"slt-layout-all-ready-plan/v2","phase":"ALL_READY","verdict":"READY_FOR_RELEASE_PLAN","authorization":"NONE","mutation_performed":False,
       "tx":context["tx"],"generation":context["generation"],"context_sha256":context["context_sha256"],"all_configured_plan_sha256":configured["plan_sha256"],
       "refresh_authorization_sha256":auth_sha,"nodes":names,"node_roles":copy.deepcopy(context["node_roles"]),"node_evidence_sha256":hashes,
       "next_phase":"EXPLICIT_RELEASE_AUTHORIZATION_V2_REQUIRED"}
 body["plan_sha256"]=digest(body);return body
