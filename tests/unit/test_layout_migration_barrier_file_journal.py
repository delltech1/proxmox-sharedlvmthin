import importlib.util,json,tempfile,unittest
from pathlib import Path
from unittest import mock

E=Path(__file__).resolve().parents[2]/"experiments/thick-generations"
S=importlib.util.spec_from_file_location("journal",E/"layout-migration-barrier-file-journal.py")
J=importlib.util.module_from_spec(S); S.loader.exec_module(J)

class Tests(unittest.TestCase):
 def setUp(self):
  self.t=tempfile.TemporaryDirectory(); self.p=mock.patch.object(J.BASE,"PARENT",self.t.name); self.p.start()
  names=("pve01","pve02","pve03","pve04")
  actions=[{"step":s,"node":n} for s in ("ENTRY_BLOCK","SERVICE_DRAIN","FINAL_AUDIT") for n in names]
  parts=[{"node":n,"boot_id":f"0000000{i}-0000-4000-8000-00000000000{i}","san_role":i<4} for i,n in enumerate(names,1)]
  self.plan={"schema":"slt-maintenance-barrier-model/v1","tx":"a"*32,"participants":parts,
   "workload_snapshot_sha256":"1"*64,"storage_cfg_sha256":"2"*64,"candidate_sha256":"3"*64,
   "issued_at":900,"deadline":1100,"actions":actions}
  self.coord={"node":"pve01","boot_id":"00000001-0000-4000-8000-000000000001"}; self.backends=[]
 def tearDown(self):
  for b in self.backends:
   if b.backend.root_fd is not None:
    try:b.backend.release_descriptors_preserving_state()
    except BaseException:pass
  self.p.stop(); self.t.cleanup()
 def journal(self,nonce="1"*32):
  b=J.BarrierFileJournal(nonce,self.plan,self.coord); self.backends.append(b); return b
 def event(self,kind="INTENT_DURABLE",receipt=False):
  e={"tx":"a"*32,"plan_sha256":J.digest(J.canonical(self.plan)),"kind":kind,"index":0,"step":"ENTRY_BLOCK","node":"pve01"}
  if receipt:e["receipt"]={"persistent_masks":sorted(J.MODEL.ENTRY_UNITS),"terminal_ingress":J.MODEL.TERMINAL_INGRESS}
  return e
 def test_empty_and_partial_are_inspection_only(self):
  b=self.journal(); self.assertEqual(J.inspect(b.artifact_path(),self.plan)["state"],"EMPTY_INSPECTION")
  b.persist_event(self.event()); self.assertEqual(J.inspect(b.artifact_path(),self.plan)["state"],"RECOVERY_REQUIRED")
 def test_exact_chain_and_receipt_round_trip(self):
  b=self.journal(); b.persist_event(self.event()); b.persist_event(self.event("ATTEMPTED")); b.persist_event(self.event("OBSERVED_COMPLETE",True))
  out=J.inspect(b.artifact_path(),self.plan); self.assertEqual(out["events"][-1]["receipt"]["terminal_ingress"],J.MODEL.TERMINAL_INGRESS); self.assertFalse(out["rollout_authorized"])
 def test_wrong_plan_gap_extra_and_hardlink_refuse(self):
  b=self.journal(); b.persist_event(self.event()); root=Path(b.artifact_path())
  wrong=dict(self.plan); wrong["tx"]="f"*32
  with self.assertRaises((J.Refusal,J.MODEL.Refusal)): J.inspect(root,wrong)
  (root/"junk").write_text("x")
  with self.assertRaises(J.Refusal): J.inspect(root,self.plan)
  (root/"junk").unlink(); (root/"link").hardlink_to(root/"exact-event-000001.json")
  with self.assertRaises(J.Refusal): J.inspect(root,self.plan)
 def test_duplicate_json_and_bool_sequence_refuse(self):
  b=self.journal(); b.close(); root=Path(b.artifact_path()); p=root/"exact-event-000000.json"
  raw=p.read_text(); p.write_text(raw[:-2]+',"tx":"a"}\n')
  with self.assertRaises(J.Refusal): J.inspect(root,self.plan)
 def test_persist_failure_never_updates_visible_history(self):
  b=self.journal(); real=b.backend.persist_exact_file
  with mock.patch.object(b.backend,"persist_exact_file",side_effect=J.Refusal("fail")):
   with self.assertRaises(J.Refusal): b.persist_event(self.event())
  self.assertEqual(b.events,[])
 def test_duplicate_writer_directory_is_create_only(self):
  self.journal()
  with self.assertRaises((J.Refusal,FileExistsError)): self.journal()
 def test_real_controller_end_to_end_uses_same_plan_hash(self):
  journal=self.journal("2"*32); plan=self.plan
  class Backend:
   def now(self): return 1000
   def load_events(self,tx): return journal.load_events(tx)
   def persist_event(self,event): return journal.persist_event(event)
   def observation(self,node):
    boot=next(x["boot_id"] for x in plan["participants"] if x["node"]==node)
    return {"node":node,"boot_id":boot,"membership":[x["node"] for x in plan["participants"]],
     "storage_cfg_sha256":"2"*64,"workload_snapshot_sha256":"1"*64,"quorate":True,
     "running_guests":[],"ha_state":"DRAINED","tasks":[],"workers":[],"pending_jobs":[],
     "persistent_masks":sorted(J.MODEL.RESTARTABLE_MASKS),
     "active_services":["corosync.service","pve-cluster.service"],
     "stopped_services":sorted(J.MODEL.REQUIRED_STOPS)}
   def observe_node(self,node): return self.observation(node)
   def mask_persistent(self,node,units): return {"persistent_masks":sorted(units),"terminal_ingress":J.MODEL.TERMINAL_INGRESS}
   def drain_services(self,node): return self.observation(node)
  result=J.MODEL.Controller(plan,Backend(),1000).run(); self.assertEqual(result["state"],"MODEL_HELD")
  journal.close(); self.assertEqual(J.inspect(journal.artifact_path(),plan)["state"],"MODEL_COMPLETED_HISTORICAL")
 def test_named_record_replacement_during_final_fsync_refuses(self):
  b=self.journal("3"*32); b.close(); root=Path(b.artifact_path()); target=root/"exact-event-000000.json"
  raw=target.read_bytes(); real=J.os.fsync; fired=[]
  def fsync(fd):
   if not fired and __import__("stat").S_ISDIR(J.os.fstat(fd).st_mode):
    fired.append(True); target.unlink(); target.write_bytes(raw); target.chmod(0o600)
   return real(fd)
  with mock.patch.object(J.os,"fsync",side_effect=fsync):
   with self.assertRaises(J.Refusal): J.inspect(root,self.plan)

if __name__=="__main__": unittest.main()
