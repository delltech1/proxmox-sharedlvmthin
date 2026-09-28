import copy,importlib.util,unittest
from pathlib import Path
P=Path(__file__).resolve().parents[2]/"experiments/thick-generations/layout-migration-guest-shutdown-model.py"
S=importlib.util.spec_from_file_location("shutdown",P); M=importlib.util.module_from_spec(S); S.loader.exec_module(M)

class Fake:
 def __init__(self,p):self.p=p;self.events=[];self.effects=[];self.clock=1000;self.running=True;self.fail=None
 def now(self):return self.clock
 def load_events(self,tx):return copy.deepcopy(self.events)
 def persist_event(self,e):
  if self.fail==("persist",e["kind"]):raise OSError("persist")
  self.events.append(copy.deepcopy(e))
 def consumers(self):
  if not self.running:return []
  return [{k:g[k] for k in ("vmid","type","node","config_sha256","managed_disks")} for g in self.p["guests"]]
 def observe_cluster(self):return {"participants":self.p["participants"],"barrier_plan_sha256":"b"*64,"barrier_evidence_sha256":"d"*64,"storage_cfg_sha256":"e"*64,"workload_snapshot_sha256":"f"*64,"admission_closed":True,"ha_idle_disarmed":True,"tasks":[],"workers":[],"running_consumers":self.consumers()}
 def submit_graceful_shutdown(self,g):
  self.effects.append(("submit",g["vmid"]));
  if self.fail=="submit-after":raise OSError("ambiguous")
  g=self.p["guests"][0];return {"upid":"UPID:pve02:1:2:3:qmshutdown:100:root@pam:","node":g["node"],"vmid":g["vmid"],"type":g["type"],"task_type":"qmshutdown"}
 def wait_task(self,u,t,d):
  if self.fail=="wait":raise TimeoutError("timeout")
  self.running=False;g=self.p["guests"][0];return {"upid":u,"node":g["node"],"vmid":g["vmid"],"type":g["type"],"task_type":"qmshutdown","terminal":True,"exitstatus":"OK"}
 def observe_guest(self,g):return {"vmid":g["vmid"],"type":g["type"],"node":g["node"],"config_sha256":g["config_sha256"],"managed_disks":g["managed_disks"],"running":self.running,"ha_managed":False,"task_refs":[],"process_refs":["guest-runtime"] if self.running else [],"cgroup_populated":self.running,"cleanup_tasks":[]}

class Tests(unittest.TestCase):
 def plan(self):return {"schema":"slt-supervised-shutdown-model/v1","tx":"a"*32,"barrier_plan_sha256":"b"*64,"barrier_evidence_sha256":"d"*64,"storage_cfg_sha256":"e"*64,"workload_snapshot_sha256":"f"*64,
  "participants":[{"node":n,"boot_id":f"0000000{i}-0000-4000-8000-00000000000{i}"} for i,n in enumerate(("pve01","pve02","pve03","pve04"),1)],"issued_at":900,"deadline":1100,"per_task_timeout":60,"force_stop":False,
  "guests":[{"vmid":100,"type":"qemu","node":"pve02","config_sha256":"c"*64,"managed_disks":["thin:vm-100-disk-0"],"ha_managed":False}]}
 def execute(self,p=None,b=None):p=p or self.plan();b=b or Fake(p);return M.Controller(p,b,1000).run(),b
 def test_success_uses_one_graceful_task_and_no_authority(self):
  r,b=self.execute();self.assertEqual(r["state"],"MODEL_ALLOWLIST_STOPPED");self.assertEqual(b.effects,[("submit",100)]);self.assertEqual([e["kind"] for e in b.events],["INTENT_DURABLE","ATTEMPTED","TASK_ACCEPTED","TASK_TERMINAL","OBSERVED_ABSENT"]);self.assertFalse(r["force_used"])
 def test_nonallowlisted_or_active_task_has_zero_effects(self):
  for mutate in (lambda s:s["running_consumers"].append({"vmid":999}),lambda s:s["tasks"].append("x"),lambda s:s.update(ha_idle_disarmed=False)):
   p=self.plan();b=Fake(p);old=b.observe_cluster
   def obs(mutate=mutate):v=old();mutate(v);return v
   b.observe_cluster=obs
   with self.assertRaises(M.Refusal):self.execute(p,b)
   self.assertEqual(b.effects,[])
 def test_intent_failure_has_zero_effects(self):
  p=self.plan();b=Fake(p);b.fail=("persist","INTENT_DURABLE")
  with self.assertRaises(OSError):self.execute(p,b)
  self.assertEqual(b.effects,[])
 def test_submit_after_effect_and_timeout_never_retry(self):
  for failure in ("submit-after","wait"):
   p=self.plan();b=Fake(p);b.fail=failure
   with self.assertRaises(Exception):self.execute(p,b)
   effects=list(b.effects);b.fail=None
   with self.assertRaisesRegex(M.Refusal,"history"):self.execute(p,b)
   self.assertEqual(b.effects,effects)
 def test_bad_terminal_or_absence_refuses(self):
  p=self.plan();b=Fake(p);acc=b.submit_graceful_shutdown(p["guests"][0]);b.effects=[];b.wait_task=lambda u,t,d:{**acc,"terminal":True,"exitstatus":"ERROR"}
  with self.assertRaisesRegex(M.Refusal,"confirmed success"):self.execute(p,b)
  p=self.plan();b=Fake(p);old=b.observe_guest;calls=[]
  def alive(g):calls.append(1);v=old(g);return v if len(calls)==1 else {**v,"running":True,"process_refs":["guest-runtime"],"cgroup_populated":True}
  b.observe_guest=alive
  with self.assertRaisesRegex(M.Refusal,"not proven"):self.execute(p,b)
 def test_deadline_and_plan_mutation_refuse(self):
  p=self.plan();b=Fake(p);b.clock=1101
  with self.assertRaises(M.Refusal):M.Controller(p,b,1000).run()
  self.assertEqual(b.effects,[])
  p=self.plan();b=Fake(p);c=M.Controller(p,b,1000);old=b.submit_graceful_shutdown
  def mutate(g):c.plan["deadline"]+=1;return old(g)
  b.submit_graceful_shutdown=mutate
  with self.assertRaisesRegex(M.Refusal,"authority"):c.run()
 def test_duplicate_vmid_and_bool_aliases_refuse(self):
  p=self.plan();other=copy.deepcopy(p["guests"][0]);other.update(node="pve03",type="lxc");p["guests"].append(other);p["guests"].sort(key=lambda g:(g["node"],g["type"],g["vmid"]));b=Fake(p)
  with self.assertRaisesRegex(M.Refusal,"duplicated"):M.Controller(p,b,1000)
  p=self.plan();b=Fake(p);old=b.observe_guest
  def alias(g):v=old(g);v["running"]=1;return v
  b.observe_guest=alias
  with self.assertRaisesRegex(M.Refusal,"preflight"):self.execute(p,b)
 def test_post_submit_expiry_preserves_accepted_upid(self):
  p=self.plan();b=Fake(p);old=b.submit_graceful_shutdown
  def late(g):v=old(g);b.clock=1101;return v
  b.submit_graceful_shutdown=late
  with self.assertRaises(M.Refusal):self.execute(p,b)
  self.assertEqual([e["kind"] for e in b.events][-1],"TASK_ACCEPTED")
  self.assertEqual(b.effects,[("submit",100)])

if __name__=="__main__":unittest.main()
