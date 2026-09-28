import copy,importlib.util,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];P=ROOT/"experiments/thick-generations/layout-migration-all-ready-v2.py"
S=importlib.util.spec_from_file_location("ar2",P);M=importlib.util.module_from_spec(S);S.loader.exec_module(M)
HS=importlib.util.spec_from_file_location("ach",ROOT/"tests/unit/test_layout_migration_all_configured_v2.py");H=importlib.util.module_from_spec(HS);HS.loader.exec_module(H)
class Tests(unittest.TestCase):
 def fixture(self,guard_mode="runtime-guard"):
  helper=H.Tests();base,target,template,context,configured_records,now=helper.fixture(guard_mode);configured=M.AC.evaluate(context,template,base,target,configured_records,now)
  names=[x["name"] for x in context["nodes"]];authorization={"schema":"slt-layout-refresh-authorization/v2","tx":context["tx"],"generation":1,"context_sha256":context["context_sha256"],"all_configured_plan_sha256":configured["plan_sha256"],"authorization_id":"a"*32,"issued_at":now-20,"expires_at":now+200,"node_order":names,"node_roles":copy.deepcopy(context["node_roles"]),"challenges":{name:f"{i+5:x}"*32 for i,name in enumerate(names)},"service_plan":[]}
  records=[]
  for i,node in enumerate(context["nodes"]):
   name=node["name"];units=[];results=[]
   for j,unit in enumerate(sorted(M.UNITS)):
    is_guard=unit=="pve-sharedlvmthin-thin-guard.service";active=not (is_guard and not context["thinguard_required_by_node"][name])
    before={"active":active,"pid":100+i*20+j if active else 0,"starttime":1000+i*20+j if active else 0};after={"active":active,"pid":200+i*20+j if active else 0,"starttime":2000+i*20+j if active else 0}
    control=context["node_roles"][name]=="CONTROL_ONLY";op="verify-current-only" if control else ("restart-active" if is_guard and active else ("preserve-inactive" if is_guard else "try-restart-active"))
    if control:after=copy.deepcopy(before)
    units.append({"unit":unit,"operation":op,"was_active":active,"before":copy.deepcopy(before)});results.append({"unit":unit,"operation":op,"before":before,"after":after,"result":"VERIFIED_UNCHANGED" if control else ("RESTARTED_ACTIVE" if active else "PRESERVED_INACTIVE")})
   authorization["service_plan"].append({"node":name,"units":units})
   hold={"active":False,"context_sha256":context["context_sha256"],"manifest_sha256":"0"*64,"device":0,"inode":0} if control else {"active":True,"context_sha256":context["context_sha256"],"manifest_sha256":f"{i+1:x}"*64,"device":100+i,"inode":200+i}
   record={"schema":"slt-layout-all-ready-node/v2","challenge":authorization["challenges"][name],"authorization_sha256":"pending","observed_at":now-1,"tx":context["tx"],"generation":1,"cluster_name":context["cluster_name"],"node":name,"boot_id":node["boot_id"],"cluster_nodes":names,"quorate":True,"target_storage_cfg_sha256":context["target_storage_cfg_sha256"],"context_sha256":context["context_sha256"],"candidate":copy.deepcopy(context["candidate"]),"workers":{"inventory_complete":True,"storage_processes":[],"transient_units":[],"pve_tasks":[]},"hold":hold,"refresh":{"completed_at":now-5,"units":results}}
   if context["thinguard_required_by_node"][name]:record.update(vg_identities=copy.deepcopy(context["expected_vg_identities_by_node"][name]),thinguard={"service_active_state":"active","daemon_pid":results[[x["unit"] for x in results].index("pve-sharedlvmthin-thin-guard.service")]["after"]["pid"],"daemon_starttime":results[[x["unit"] for x in results].index("pve-sharedlvmthin-thin-guard.service")]["after"]["starttime"],"socket_inode":900+i,"samples":[{"request_id":"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa","observed_at":now-4,"state":"IDLE","action":"NONE","watchdog":"DISARMED","response_sha256":"d"*64},{"request_id":"bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb","observed_at":now-2,"state":"IDLE","action":"NONE","watchdog":"DISARMED","response_sha256":"e"*64}]})
   else:record.update(vg_identities=copy.deepcopy(context["expected_vg_identities_by_node"][name]),thinguard={"service_active_state":"inactive","main_pid":0,"jobs":[],"daemon_processes":[],"watchdog_registrations":[]})
   records.append(record)
  auth_sha=M.digest(authorization)
  for record in records:record["authorization_sha256"]=auth_sha
  return base,target,template,context,configured,authorization,records,now
 def test_role_aware_refresh_is_non_authorizing(self):
  b,t,tp,c,ac,a,r,n=self.fixture();out=M.evaluate(c,tp,b,t,ac,a,r,n);self.assertEqual(out["authorization"],"NONE");self.assertEqual(out["next_phase"],"EXPLICIT_RELEASE_AUTHORIZATION_V2_REQUIRED")
 def test_remote_audit_preserves_guard_inactive_on_san_nodes(self):
  b,t,tp,c,ac,a,r,n=self.fixture("remote-audit");out=M.evaluate(c,tp,b,t,ac,a,r,n);self.assertEqual(out["authorization"],"NONE")
  for i,node in enumerate(c["nodes"]):
   guard=next(x for x in r[i]["refresh"]["units"] if x["unit"]=="pve-sharedlvmthin-thin-guard.service");self.assertEqual(guard["result"],"VERIFIED_UNCHANGED" if node["san_role"]=="CONTROL_ONLY" else "PRESERVED_INACTIVE")
 def test_control_guard_start_or_restart_intent_refuses(self):
  b,t,tp,c,ac,a,r,n=self.fixture();guard=next(x for x in a["service_plan"][-1]["units"] if x["unit"]=="pve-sharedlvmthin-thin-guard.service");guard.update(operation="restart-active",was_active=True,before={"active":True,"pid":5,"starttime":6})
  with self.assertRaises(M.Refusal):M.evaluate(c,tp,b,t,ac,a,r,n)
 def test_control_must_be_preserved_inactive_in_result(self):
  b,t,tp,c,ac,a,r,n=self.fixture();item=next(x for x in r[-1]["refresh"]["units"] if x["unit"]=="pve-sharedlvmthin-thin-guard.service");item.update(after={"active":True,"pid":8,"starttime":9},result="RESTARTED_ACTIVE")
  with self.assertRaises(M.Refusal):M.evaluate(c,tp,b,t,ac,a,r,n)
 def test_non_guard_inactive_service_is_preserved(self):
  b,t,tp,c,ac,a,r,n=self.fixture();unit="pve-ha-lrm.service"
  intent=next(x for x in a["service_plan"][0]["units"] if x["unit"]==unit);intent.update(was_active=False,before={"active":False,"pid":0,"starttime":0})
  result=next(x for x in r[0]["refresh"]["units"] if x["unit"]==unit);result.update(before={"active":False,"pid":0,"starttime":0},after={"active":False,"pid":0,"starttime":0},result="PRESERVED_INACTIVE")
  auth_sha=M.digest(a)
  for record in r:record["authorization_sha256"]=auth_sha
  self.assertEqual(M.evaluate(c,tp,b,t,ac,a,r,n)["authorization"],"NONE")
 def test_missing_node_reboot_or_hold_loss_refuses(self):
  b,t,tp,c,ac,a,r,n=self.fixture();variants=[r[:-1],copy.deepcopy(r),copy.deepcopy(r)];variants[1][0]["boot_id"]="99999999-9999-4999-8999-999999999999";variants[2][0]["hold"]["active"]=False
  for bad in variants:
   with self.assertRaises(M.Refusal):M.evaluate(c,tp,b,t,ac,a,bad,n)
 def test_guard_samples_must_follow_refresh_and_active_lifecycle_changes(self):
  b,t,tp,c,ac,a,r,n=self.fixture();variants=[copy.deepcopy(r),copy.deepcopy(r)];variants[0][0]["thinguard"]["samples"][0]["observed_at"]=n-6
  item=next(x for x in variants[1][0]["refresh"]["units"] if x["unit"]=="pve-sharedlvmthin-thin-guard.service");item["after"]=copy.deepcopy(item["before"])
  for bad in variants:
   with self.assertRaises(M.Refusal):M.evaluate(c,tp,b,t,ac,a,bad,n)
 def test_guard_status_identity_must_equal_refresh_after_lifecycle(self):
  b,t,tp,c,ac,a,r,n=self.fixture();unit=next(x for x in r[0]["refresh"]["units"] if x["unit"]=="pve-sharedlvmthin-thin-guard.service")
  variants=[]
  for mutate in (lambda g:g.update(daemon_pid=g["daemon_pid"]+1),lambda g:g.update(daemon_starttime=g["daemon_starttime"]+1),lambda g:g.update(daemon_pid=unit["before"]["pid"],daemon_starttime=unit["before"]["starttime"])):
   bad=copy.deepcopy(r);mutate(bad[0]["thinguard"]);variants.append(bad)
  for bad in variants:
   with self.assertRaises(M.Refusal):M.evaluate(c,tp,b,t,ac,a,bad,n)
 def test_authorization_expiry_alias_generation_and_plan_tamper_refuse(self):
  b,t,tp,c,ac,a,r,n=self.fixture();variants=[]
  aa=copy.deepcopy(a);aa["expires_at"]=n-1;variants.append((ac,aa,r));aa=copy.deepcopy(a);aa["generation"]=True;variants.append((ac,aa,r));badac=copy.deepcopy(ac);badac["node_roles"]["pve04"]="SAN_PARTICIPANT";body=copy.deepcopy(badac);body.pop("plan_sha256");badac["plan_sha256"]=M.digest(body);variants.append((badac,a,r))
  for cp,auth,records in variants:
   with self.assertRaises(M.Refusal):M.evaluate(c,tp,b,t,cp,auth,records,n)
if __name__=="__main__":unittest.main()
