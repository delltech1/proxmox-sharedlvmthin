import copy,importlib.util,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];P=ROOT/"experiments/thick-generations/layout-migration-v2-v1-adapter.py"
S=importlib.util.spec_from_file_location("adapter",P);M=importlib.util.module_from_spec(S);S.loader.exec_module(M)
HSP=importlib.util.spec_from_file_location("helper",ROOT/"tests/unit/test_layout_migration_v2_context.py");H=importlib.util.module_from_spec(HSP);HSP.loader.exec_module(H)
class Tests(unittest.TestCase):
 def fixture(self,guard_mode="remote-audit"):
  h=H.Tests();base=h.baseline().replace(b"runtime-guard",guard_mode.encode());target,_=M.CTX.PLAN.target_layout(base);template=h.template();candidate=h.candidate();context=M.CTX.build_context({"tx":"c"*32,"generation":1},candidate,template,base,target);now=2_000_000_000;names=[x["name"] for x in context["nodes"]];rows=[];barrier=[]
  for i,node in enumerate(context["nodes"]):
   name=node["name"];role=node["san_role"];action="UNPACK_CONFIGURE" if role=="SAN_PARTICIPANT" else "VERIFY_CURRENT_ONLY";installed={"package":"pve-sharedlvmthin","version":candidate["version"] if role=="CONTROL_ONLY" else "old","flavor":"dual","artifact_sha256":candidate["artifact_sha256"] if role=="CONTROL_ONLY" else "f"*64,"dpkg_state":"installed"};guard={"service_active_state":"inactive","main_pid":0,"jobs":[],"daemon_processes":[],"watchdog_registrations":[]}
   if context["thinguard_required_by_node"][name]:guard={"service_active_state":"active","daemon_pid":9,"daemon_starttime":10,"socket_inode":11,"samples":[{"request_id":"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa","observed_at":now-3,"state":"IDLE","action":"NONE","watchdog":"DISARMED","response_sha256":"d"*64},{"request_id":"bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb","observed_at":now-2,"state":"IDLE","action":"NONE","watchdog":"DISARMED","response_sha256":"e"*64}]}
   rows.append({"schema":"slt-layout-node-maintenance-evidence/v2","challenge":f"{i+1}"*32,"node":name,"nodeid":i+1,"cluster_name":"lab","cluster_nodes":names,"observed_at":now-2,"corosync_conf_sha256":"d"*64,"role":role,"action":action,"candidate":copy.deepcopy(candidate),"installed":installed,"role_evidence":{"schema":"slt-layout-node-role-evidence/v2","node":name,"boot_id":node["boot_id"],"san_role":role,"topology_sha256":context["baseline_topology_sha256"],"vg_identities":copy.deepcopy(context["expected_vg_identities_by_node"][name]),"thinguard":guard},"workers":{"proc_inventory_complete":True,"systemd_inventory_complete":True,"storage_processes":[],"blocking_transient_units":[],"active_pve_tasks":[]},"verdict":"PREFLIGHT_ONLY","authorization":"NONE","authorizes_mutation":False,"mutation_performed":False})
   barrier.append({"node":name,"boot_id":node["boot_id"],"evidence_sha256":f"{i+5}"*64,"barrier":True,"workers_clear":True,"guard_quiescent":True,"old_consumers_absent":True})
  b={"schema":"slt-live-maintenance-barrier/v2","tx":context["tx"],"generation":1,"context_sha256":context["context_sha256"],"observed_at":now-1,"nodes":barrier,"authorization":"HOLD_ACTIVE","mutation_performed":True}
  a={"schema":"slt-v1-package-adapter-authorization/v2","tx":context["tx"],"generation":1,"context_sha256":context["context_sha256"],"authorization_id":"a"*32,"issued_at":now-10,"expires_at":now+300,"effect":"project-v1-prepare-manifest"}
  return base,target,template,context,rows,b,a,now
 def project(self,f):return M.project(f[3],f[2],f[0],f[1],f[4],f[5],f[6],f[7])
 def test_exact_projection_has_one_verify_only_and_v1_candidate_shape(self):
  out=self.project(self.fixture());self.assertEqual(sum(x["action"]=="VERIFY_CURRENT_ONLY" for x in out["sidecar"]["actions"]),1);self.assertEqual(set(out["manifest"]["candidate"]),{"package","version","flavor","artifact_sha256","deb_sha256"});self.assertFalse(out["sidecar"]["mutation_performed"])
 def test_pve04_never_receives_package_action(self):
  out=self.project(self.fixture());self.assertEqual(next(x for x in out["sidecar"]["actions"] if x["node"]=="pve04"),{"node":"pve04","role":"CONTROL_ONLY","action":"VERIFY_CURRENT_ONLY"})
 def test_control_payload_mismatch_refuses(self):
  f=list(self.fixture());f[4]=copy.deepcopy(f[4]);f[4][-1]["installed"]["artifact_sha256"]="0"*64
  with self.assertRaises(M.Refusal):self.project(f)
 def test_barrier_negative_stale_or_wrong_boot_refuses(self):
  for mutate in (lambda f:f[5]["nodes"][0].update(old_consumers_absent=False),lambda f:f[5].update(observed_at=f[7]-301),lambda f:f[5]["nodes"][0].update(boot_id="99999999-9999-4999-8999-999999999999")):
   f=list(self.fixture());f[5]=copy.deepcopy(f[5]);mutate(f)
   with self.assertRaises(M.Refusal):self.project(f)
 def test_expired_or_foreign_authorization_refuses(self):
  for mutate in (lambda f:f[6].update(expires_at=f[7]-1),lambda f:f[6].update(context_sha256="0"*64)):
   f=list(self.fixture());f[6]=copy.deepcopy(f[6]);mutate(f)
   with self.assertRaises(M.Refusal):self.project(f)
 def test_preflight_role_action_or_candidate_drift_refuses(self):
  for mutate in (lambda r:r[0].update(action="VERIFY_CURRENT_ONLY"),lambda r:r[-1].update(role="SAN_PARTICIPANT"),lambda r:r[0]["candidate"].update(version="other")):
   f=list(self.fixture());f[4]=copy.deepcopy(f[4]);mutate(f[4])
   with self.assertRaises(M.Refusal):self.project(f)
 def test_incomplete_workers_inner_identity_or_guard_refuses(self):
  for mutate in (lambda r:r[0]["workers"].update(proc_inventory_complete=False),lambda r:r[0]["workers"].update(systemd_inventory_complete=False),lambda r:r[0]["role_evidence"].update(node="pve02"),lambda r:r[0]["role_evidence"]["thinguard"]["daemon_processes"].append({"pid":9}),lambda r:r[0]["role_evidence"]["thinguard"]["watchdog_registrations"].append("unknown")):
   f=list(self.fixture());f[4]=copy.deepcopy(f[4]);mutate(f[4])
   with self.assertRaises(M.Refusal):self.project(f)
 def test_installed_state_profile_or_identity_refuses(self):
  for index in (0,-1):
   for mutate in (lambda i:i.update(dpkg_state="unpacked"),lambda i:i.update(flavor="thick-only"),lambda i:i.update(package="pve-sharedlvmthin-thick")):
    f=list(self.fixture());f[4]=copy.deepcopy(f[4]);mutate(f[4][index]["installed"])
    with self.assertRaises(M.Refusal):self.project(f)
 def test_active_guard_samples_are_fresh_against_evaluator_clock(self):
  f=list(self.fixture("runtime-guard"));f[4]=copy.deepcopy(f[4]);f[4][0]["observed_at"]=f[7]-300;f[4][0]["role_evidence"]["thinguard"]["samples"][0]["observed_at"]=f[7]-600;f[4][0]["role_evidence"]["thinguard"]["samples"][1]["observed_at"]=f[7]-599
  with self.assertRaises(M.Refusal):self.project(f)
if __name__=="__main__":unittest.main()
