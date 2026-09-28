import copy, importlib.util, unittest
from pathlib import Path

P=Path(__file__).resolve().parents[2]/"experiments/thick-generations/layout-migration-barrier-model.py"
S=importlib.util.spec_from_file_location("barrier",P); M=importlib.util.module_from_spec(S); S.loader.exec_module(M)

class Fake:
 def __init__(self,plan): self.plan=plan; self.events=[]; self.clock=1000; self.fail=None; self.effects=[]
 def now(self): return self.clock
 def load_events(self,tx): return copy.deepcopy(self.events)
 def persist_event(self,event):
  if self.fail==("persist",event["kind"],event["index"]): raise OSError("persist")
  self.events.append(copy.deepcopy(event))
 def obs(self,node):
  boot=next(x["boot_id"] for x in self.plan["participants"] if x["node"]==node)
  return {"node":node,"boot_id":boot,"membership":[x["node"] for x in self.plan["participants"]],
   "storage_cfg_sha256":"b"*64,"workload_snapshot_sha256":"a"*64,"quorate":True,
   "running_guests":[],"ha_state":"DRAINED","tasks":[],"workers":[],"pending_jobs":[],
   "persistent_masks":sorted(M.RESTARTABLE_MASKS),"active_services":["corosync.service","pve-cluster.service"],
   "stopped_services":sorted(M.REQUIRED_STOPS)}
 def effect(self,kind,node,value):
  self.effects.append((kind,node))
  if self.fail==(kind,node,"after"): raise OSError("effect then error")
  return value
 def mask_persistent(self,node,units): return self.effect("mask",node,{"persistent_masks":sorted(units),"terminal_ingress":M.TERMINAL_INGRESS})
 def drain_services(self,node): return self.effect("drain",node,self.obs(node))
 def observe_node(self,node): return self.obs(node)

class Tests(unittest.TestCase):
 def plan(self):
  names=["pve01","pve02","pve03","pve04"]
  parts=[{"node":n,"boot_id":f"0000000{i}-0000-4000-8000-00000000000{i}","san_role":i<4} for i,n in enumerate(names,1)]
  actions=([{"step":"ENTRY_BLOCK","node":n} for n in names]+[{"step":"SERVICE_DRAIN","node":n} for n in names]+[{"step":"FINAL_AUDIT","node":n} for n in names])
  return {"schema":"slt-maintenance-barrier-model/v1","tx":"1"*32,"participants":parts,
   "workload_snapshot_sha256":"a"*64,"storage_cfg_sha256":"b"*64,"candidate_sha256":"c"*64,
   "issued_at":900,"deadline":1100,"actions":actions}
 def execute(self,p=None,b=None):
  p=p or self.plan(); b=b or Fake(p); return M.Controller(p,b,1000).run(),b
 def test_success_is_model_only(self):
  result,b=self.execute(); self.assertEqual(result,{"state":"MODEL_HELD","runtime_authorized":False,"rollout_authorized":False,"release_authorized":False,"mutation_performed":False}); self.assertEqual(len(b.events),36)
  result=M.Controller(self.plan(),b,1000).run(); self.assertEqual(result["state"],"MODEL_COMPLETED_HISTORICAL")
 def test_exact_four_nodes_and_action_order(self):
  for mutate in (lambda p:p["participants"].pop(),lambda p:p["actions"].reverse(),lambda p:p["participants"].append(copy.deepcopy(p["participants"][0]))):
   p=self.plan(); mutate(p)
   with self.assertRaises(M.Refusal): M.Controller(p,Fake(p),1000)
 def test_running_guest_refuses_after_attempt(self):
  p=self.plan(); b=Fake(p); old=b.obs
  def obs(n): v=old(n); v["running_guests"]=[100]; return v
  b.obs=obs
  with self.assertRaisesRegex(M.Refusal,"running_guests"): self.execute(p,b)
  self.assertEqual(b.effects,[])
 def test_intent_failure_has_no_effect(self):
  p=self.plan(); b=Fake(p); b.fail=("persist","INTENT_DURABLE",0)
  with self.assertRaises(OSError): self.execute(p,b)
  self.assertEqual(b.effects,[])
 def test_effect_then_error_is_never_retried(self):
  p=self.plan(); b=Fake(p); b.fail=("mask","pve01","after")
  with self.assertRaises(OSError): self.execute(p,b)
  self.assertEqual(b.effects,[("mask","pve01")]); b.fail=None
  with self.assertRaisesRegex(M.Refusal,"pending"): self.execute(p,b)
  self.assertEqual(b.effects,[("mask","pve01")])
 def test_result_journal_failure_is_ambiguous(self):
  p=self.plan(); b=Fake(p); b.fail=("persist","OBSERVED_COMPLETE",0)
  with self.assertRaises(OSError): self.execute(p,b)
  self.assertEqual(len(b.effects),1); b.fail=None
  with self.assertRaisesRegex(M.Refusal,"pending"): self.execute(p,b)
  self.assertEqual(len(b.effects),1)
 def test_boot_config_and_membership_drift_refuse(self):
  for field,value in (("boot_id","00000009-0000-4000-8000-000000000009"),("storage_cfg_sha256","d"*64),("membership",["pve01"])):
   p=self.plan(); b=Fake(p); old=b.obs
   def obs(n,field=field,value=value): v=old(n); v[field]=value; return v
   b.obs=obs
   with self.assertRaisesRegex(M.Refusal,"drifted"): self.execute(p,b)
 def test_runtime_mask_or_forbidden_stop_refuses(self):
  p=self.plan(); b=Fake(p); old=b.obs
  def missing(n): v=old(n); v["persistent_masks"]=[]; return v
  b.obs=missing
  with self.assertRaisesRegex(M.Refusal,"entry barrier"): self.execute(p,b)
  p=self.plan(); b=Fake(p); old=b.obs
  def forbidden(n): v=old(n); v["stopped_services"].append("pve-cluster.service"); v["stopped_services"].sort(); return v
  b.obs=forbidden
  with self.assertRaisesRegex(M.Refusal,"forbidden"): self.execute(p,b)
 def test_late_completion_refuses_and_does_not_retry(self):
  p=self.plan(); b=Fake(p); old=b.mask_persistent
  def late(n,u): v=old(n,u); b.clock=1101; return v
  b.mask_persistent=late
  with self.assertRaisesRegex(M.Refusal,"deadline"): self.execute(p,b)
  effects=len(b.effects); b.clock=1000
  with self.assertRaisesRegex(M.Refusal,"pending"): self.execute(p,b)
  self.assertEqual(len(b.effects),effects)
 def test_expired_before_run_has_no_effect(self):
  p=self.plan(); b=Fake(p); b.clock=1101
  with self.assertRaisesRegex(M.Refusal,"deadline"): M.Controller(p,b,1000).run()
  self.assertEqual(b.effects,[])
 def test_replay_requires_exact_receipts_and_identity(self):
  p=self.plan(); b=Fake(p); self.execute(p,b)
  for mutation in (lambda h:h[2].pop("receipt"),lambda h:h[0].update(tx="f"*32),lambda h:h.append(copy.deepcopy(h[-1]))):
   clone=Fake(p); clone.events=copy.deepcopy(b.events); mutation(clone.events)
   with self.assertRaises(M.Refusal): M.Controller(p,clone,1000).run()
 def test_plan_mutation_and_caught_reentry_poison(self):
  p=self.plan(); b=Fake(p); controller=M.Controller(p,b,1000)
  old=b.mask_persistent
  def mutate(n,u): controller.plan["deadline"]+=1; return old(n,u)
  b.mask_persistent=mutate
  with self.assertRaisesRegex(M.Refusal,"plan changed"): controller.run()
  p=self.plan(); b=Fake(p); controller=M.Controller(p,b,1000); old=b.mask_persistent
  def nested(n,u):
   try: controller.run()
   except M.Refusal: pass
   return old(n,u)
  b.mask_persistent=nested
  with self.assertRaises(M.Refusal): controller.run()
 def test_partial_completed_replay_never_continues(self):
  p=self.plan(); b=Fake(p); self.execute(p,b); b.events=b.events[:3]
  effects=len(b.effects)
  with self.assertRaisesRegex(M.Refusal,"explicit recovery"): M.Controller(p,b,1000).run()
  self.assertEqual(len(b.effects),effects)
 def test_clock_callback_cannot_lower_watermark(self):
  p=self.plan(); b=Fake(p); controller=M.Controller(p,b,1000)
  def corrupt(): controller.last_clock=0; return 500
  b.now=corrupt
  with self.assertRaisesRegex(M.Refusal,"watermark"): controller.run()
  self.assertEqual(b.effects,[])

if __name__=="__main__": unittest.main()
