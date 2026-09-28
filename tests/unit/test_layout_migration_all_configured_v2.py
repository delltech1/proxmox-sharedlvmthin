import copy,importlib.util,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];P=ROOT/"experiments/thick-generations/layout-migration-all-configured-v2.py"
S=importlib.util.spec_from_file_location("ac2",P);M=importlib.util.module_from_spec(S);S.loader.exec_module(M)
HELP_SPEC=importlib.util.spec_from_file_location("helper",ROOT/"tests/unit/test_layout_migration_v2_context.py");H=importlib.util.module_from_spec(HELP_SPEC);HELP_SPEC.loader.exec_module(H)
class Tests(unittest.TestCase):
 def fixture(self,guard_mode="runtime-guard"):
  h=H.Tests();base=h.baseline().replace(b"runtime-guard",guard_mode.encode());target,_=M.CTX.PLAN.target_layout(base);template=h.template();candidate=h.candidate();context=M.CTX.build_context({"tx":"c"*32,"generation":1},candidate,template,base,target);now=2_000_000_000
  records=[]
  for i,node in enumerate(context["nodes"]):
   name=node["name"];common={"schema":"slt-layout-all-configured-node/v2","challenge":f"{i+1}"*32,"observed_at":now-1,"tx":context["tx"],"generation":1,"cluster_name":"lab","node":name,"boot_id":node["boot_id"],"cluster_nodes":[x["name"] for x in context["nodes"]],"quorate":True,"target_storage_cfg_sha256":context["target_storage_cfg_sha256"],"context_sha256":context["context_sha256"],"candidate":copy.deepcopy(candidate),"workers":{"inventory_complete":True,"storage_processes":[],"transient_units":[],"pve_tasks":[]}}
   if context["node_roles"][name]=="CONTROL_ONLY":common["receipt"]={"schema":"slt-current-payload-verified/v2","phase":"CURRENT_PAYLOAD_VERIFIED","tx":context["tx"],"generation":1,"node":name,"boot_id":node["boot_id"],"context_sha256":context["context_sha256"],"candidate":copy.deepcopy(candidate),"target_storage_cfg_sha256":context["target_storage_cfg_sha256"],"recorded_at":now-4,"dpkg_state":"installed","payload_verified":True}
   else:common["receipt"]={"schema":"slt-package-maintenance-receipt/v2","phase":"PACKAGE_CONFIGURED_DEFERRED","tx":context["tx"],"generation":1,"node":name,"boot_id":node["boot_id"],"context_sha256":context["context_sha256"],"candidate":copy.deepcopy(candidate),"target_storage_cfg_sha256":context["target_storage_cfg_sha256"],"recorded_at":now-4}
   if context["thinguard_required_by_node"][name]:common.update(vg_identities=copy.deepcopy(context["expected_vg_identities_by_node"][name]),thinguard={"service_active_state":"active","daemon_pid":9,"daemon_starttime":10,"socket_inode":11,"samples":[{"request_id":"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa","observed_at":now-3,"state":"IDLE","action":"NONE","watchdog":"DISARMED","response_sha256":"d"*64},{"request_id":"bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb","observed_at":now-2,"state":"IDLE","action":"NONE","watchdog":"DISARMED","response_sha256":"e"*64}]})
   else:common.update(vg_identities=copy.deepcopy(context["expected_vg_identities_by_node"][name]),thinguard={"service_active_state":"inactive","main_pid":0,"jobs":[],"daemon_processes":[],"watchdog_registrations":[]})
   records.append(common)
  return base,target,template,context,records,now
 def test_exact_four_node_barrier_is_non_authorizing(self):
  b,t,tp,c,r,n=self.fixture();out=M.evaluate(c,tp,b,t,r,n);self.assertEqual(out["authorization"],"NONE");self.assertEqual(out["node_roles"]["pve04"],"CONTROL_ONLY")
 def test_remote_audit_requires_inactive_guard_on_san_but_exact_vgs(self):
  b,t,tp,c,r,n=self.fixture("remote-audit");self.assertTrue(all(v is False for v in c["thinguard_required_by_node"].values()));self.assertEqual(M.evaluate(c,tp,b,t,r,n)["authorization"],"NONE");self.assertTrue(r[0]["vg_identities"])
 def test_missing_duplicate_or_rebooted_participant_refuses(self):
  b,t,tp,c,r,n=self.fixture()
  variants=[r[:-1],r[:-1]+[copy.deepcopy(r[0])],copy.deepcopy(r)]
  variants[2][0]["boot_id"]="99999999-9999-4999-8999-999999999999"
  for value in variants:
   with self.assertRaises(M.Refusal):M.evaluate(c,tp,b,t,value,n)
 def test_missing_or_foreign_vg_and_duplicate_guard_request_refuse(self):
  b,t,tp,c,r,n=self.fixture();variants=[]
  for mutate in (lambda x:x[0].update(vg_identities=[]),lambda x:x[0]["vg_identities"].append({"vg_name":"foreign"}),lambda x:x[0]["thinguard"]["samples"][1].update(request_id=x[0]["thinguard"]["samples"][0]["request_id"])):
   bad=copy.deepcopy(r);mutate(bad);variants.append(bad)
  for value in variants:
   with self.assertRaises(M.Refusal):M.evaluate(c,tp,b,t,value,n)
 def test_control_guard_must_remain_strictly_inactive(self):
  b,t,tp,c,r,n=self.fixture()
  for mutate in (lambda g:g.update(service_active_state="active"),lambda g:g.update(main_pid=2),lambda g:g["jobs"].append("start"),lambda g:g["watchdog_registrations"].append("unknown")):
   bad=copy.deepcopy(r);mutate(bad[-1]["thinguard"])
   with self.assertRaises(M.Refusal):M.evaluate(c,tp,b,t,bad,n)
 def test_control_requires_verify_only_not_fake_package_receipt(self):
  b,t,tp,c,r,n=self.fixture();self.assertEqual(r[-1]["receipt"]["phase"],"CURRENT_PAYLOAD_VERIFIED")
  fake=copy.deepcopy(r);fake[-1]["receipt"]={"schema":"slt-package-maintenance-receipt/v2","phase":"PACKAGE_CONFIGURED_DEFERRED","tx":c["tx"],"generation":1,"node":"pve04","boot_id":c["nodes"][-1]["boot_id"],"context_sha256":c["context_sha256"],"candidate":copy.deepcopy(c["candidate"]),"target_storage_cfg_sha256":c["target_storage_cfg_sha256"],"recorded_at":n-4}
  with self.assertRaises(M.Refusal):M.evaluate(c,tp,b,t,fake,n)
 def test_san_cannot_use_control_verify_receipt(self):
  b,t,tp,c,r,n=self.fixture();bad=copy.deepcopy(r);receipt=copy.deepcopy(bad[-1]["receipt"]);receipt.update(node="pve01",boot_id=c["nodes"][0]["boot_id"]);bad[0]["receipt"]=receipt
  with self.assertRaises(M.Refusal):M.evaluate(c,tp,b,t,bad,n)
 def test_receipt_candidate_context_and_worker_are_exact(self):
  b,t,tp,c,r,n=self.fixture()
  for mutate in (lambda x:x[0]["receipt"].update(context_sha256="0"*64),lambda x:x[0]["candidate"].update(version="other"),lambda x:x[0]["workers"]["pve_tasks"].append("UPID")):
   bad=copy.deepcopy(r);mutate(bad)
   with self.assertRaises(M.Refusal):M.evaluate(c,tp,b,t,bad,n)
 def test_guard_samples_cannot_predate_configured_receipt(self):
  b,t,tp,c,r,n=self.fixture();bad=copy.deepcopy(r);bad[0]["receipt"]["recorded_at"]=n-1
  with self.assertRaises(M.Refusal):M.evaluate(c,tp,b,t,bad,n)
 def test_generation_bool_or_float_alias_refuses_in_record_and_receipt(self):
  b,t,tp,c,r,n=self.fixture()
  for location in ("record","receipt"):
   for alias in (True,1.0):
    bad=copy.deepcopy(r)
    if location=="record":bad[0]["generation"]=alias
    else:bad[0]["receipt"]["generation"]=alias
    with self.assertRaises(M.Refusal):M.evaluate(c,tp,b,t,bad,n)
if __name__=="__main__":unittest.main()
