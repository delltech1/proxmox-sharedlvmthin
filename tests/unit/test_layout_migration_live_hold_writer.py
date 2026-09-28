import copy,fcntl,importlib.util,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
def module(name,path):
 s=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
M=module("writer",ROOT/"experiments/thick-generations/layout-migration-live-hold-writer.py")
A=module("adapter_test",ROOT/"tests/unit/test_layout_migration_v2_v1_adapter.py")
class Tests(unittest.TestCase):
 def fixture(self,node="pve01"):
  af=A.Tests().fixture();projection=A.Tests().project(af);manifest=projection["manifest"];sidecar=projection["sidecar"];now=af[7];boot=next(x["boot_id"] for x in af[3]["nodes"] if x["name"]==node)
  facts={"node":node,"boot_id":boot,"cluster_name":af[3]["cluster_name"],"corosync_conf_sha256":"d"*64,"storage_cfg_sha256":af[3]["baseline_storage_cfg_sha256"]}
  auth={"schema":"slt-live-hold-write-authorization/v1","tx":af[3]["tx"],"generation":af[3]["generation"],"context_sha256":af[3]["context_sha256"],"manifest_sha256":sidecar["manifest_sha256"],"node":node,"boot_id":boot,"action":"create-prepare-once","operation_id":"9"*32,"issued_at":now-10,"expires_at":now+300}
  return manifest,sidecar,auth,facts,now
 def transition_fixture(self,root,node="pve01"):
  manifest,sidecar,prepare,facts,now=self.fixture(node);w=M.Writer(root);w.create_prepare_once(manifest,sidecar,prepare,facts,now)
  successor=copy.deepcopy(manifest);successor["phase"]="CONFIG_COMMITTED";old_sha=M.digest(M.canonical(manifest));new_sha=M.digest(M.canonical(successor))
  facts=copy.deepcopy(facts);facts["storage_cfg_sha256"]=manifest["target_storage_cfg_sha256"]
  preinst={"schema":"slt-package-maintenance-receipt/v1","tx":manifest["tx"],"generation":manifest["generation"],"phase":"PREINST_ACCEPTED","node":node,"boot_id":facts["boot_id"],"package":manifest["candidate"]["package"],"version":manifest["candidate"]["version"],"flavor":manifest["candidate"]["flavor"],"artifact_sha256":manifest["candidate"]["artifact_sha256"],"manifest_sha256":old_sha,"storage_cfg_sha256":manifest["baseline_storage_cfg_sha256"],"recorded_at":now-1}
  boots={x["name"]:x["boot_id"] for x in manifest["nodes"]};proof={"schema":"slt-config-commit-proof/v1","tx":manifest["tx"],"generation":manifest["generation"],"context_sha256":sidecar["context_sha256"],"old_manifest_sha256":old_sha,"new_manifest_sha256":new_sha,"target_storage_cfg_sha256":manifest["target_storage_cfg_sha256"],"config_cas_sha256":"7"*64,"all_target_payload_present":True,"nodes":[{"node":x["node"],"boot_id":boots[x["node"]],"role":x["role"],"action":x["action"],"payload_evidence_sha256":f"{i+5}"*64} for i,x in enumerate(sidecar["actions"])]}
  receipts=root/"receipts";receipt_path=receipts/(manifest["tx"]+".json");receipt_path.write_bytes(M.canonical(preinst)+b"\n");receipt_path.chmod(0o600)
  auth={"schema":"slt-live-hold-transition-authorization/v1","tx":manifest["tx"],"generation":manifest["generation"],"context_sha256":sidecar["context_sha256"],"old_manifest_sha256":old_sha,"new_manifest_sha256":new_sha,"node":node,"boot_id":facts["boot_id"],"action":"transition-config-committed-once","operation_id":"8"*32,"prepare_operation_id":prepare["operation_id"],"proof_sha256":M.digest(M.canonical(proof)),"preinst_sha256":M.digest(M.canonical(preinst)+b"\n"),"issued_at":now-10,"expires_at":now+300}
  return w,manifest,sidecar,auth,facts,preinst,proof,now,successor
 def test_create_is_durable_and_second_attempt_refuses(self):
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp)/"maintenance";w=M.Writer(root);f=self.fixture();out=w.create_prepare_once(*f)
   self.assertEqual(out["classification"],"PREPARE_HOLD_CREATED");self.assertEqual((root/"active.json").read_bytes(),M.canonical(f[0]));self.assertTrue((root/"receipts").is_dir());self.assertFalse(out["rollout_authorized"])
   with self.assertRaises(M.Refusal):w.create_prepare_once(*f)
 def test_control_only_refuses_before_first_filesystem_write(self):
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp)/"maintenance";f=list(self.fixture("pve04"))
   with self.assertRaisesRegex(M.Refusal,"authorized SAN"):
    M.Writer(root).create_prepare_once(*f)
   self.assertFalse(root.exists())
 def test_effect_then_error_is_not_retried(self):
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp)/"maintenance";w=M.Writer(root);f=self.fixture()
   def fault(point):
    if point=="after_active_durable":raise OSError("injected")
   with self.assertRaises(M.Refusal):w.create_prepare_once(*f,fault=fault)
   self.assertTrue((root/"active.json").is_file());self.assertEqual(list((root/"attempts").glob("*-complete.json")),[])
   with self.assertRaises(M.Refusal):w.create_prepare_once(*f)
 def test_prepublication_fault_leaves_no_active_and_pending_attempt(self):
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp)/"maintenance";w=M.Writer(root);f=self.fixture()
   def fault(point):
    if point=="after_intent_durable":raise OSError("injected")
   with self.assertRaises(M.Refusal):w.create_prepare_once(*f,fault=fault)
   self.assertFalse((root/"active.json").exists());self.assertEqual(len(list((root/"attempts").glob("*-intent.json"))),1)
   with self.assertRaises(M.Refusal):w.create_prepare_once(*f)
 def test_foreign_active_symlink_and_expired_authorization_refuse(self):
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp)/"maintenance";root.mkdir(mode=0o700);(root/"active.json").write_bytes(b"foreign");os.chmod(root/"active.json",0o600)
   with self.assertRaises(M.Refusal):M.Writer(root).create_prepare_once(*self.fixture())
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp)/"maintenance";f=list(self.fixture());f[2]=copy.deepcopy(f[2]);f[2]["expires_at"]=f[4]-1
   with self.assertRaises(M.Refusal):M.Writer(root).create_prepare_once(*f)
   self.assertFalse(root.exists())
 def test_manifest_sidecar_boot_and_operation_binding_refuse(self):
  for mutate in (lambda f:f[1].update(manifest_sha256="0"*64),lambda f:f[2].update(boot_id="99999999-9999-4999-8999-999999999999"),lambda f:f[2].update(action="transition")):
   with tempfile.TemporaryDirectory() as temp:
    root=Path(temp)/"maintenance";f=list(self.fixture());f[1]=copy.deepcopy(f[1]);f[2]=copy.deepcopy(f[2]);mutate(f)
    with self.assertRaises(M.Refusal):M.Writer(root).create_prepare_once(*f)
    self.assertFalse(root.exists())
 def test_crash_matrix_never_retries_after_durable_intent(self):
  points=("after_intent_link","after_intent_directory_fsync","after_intent_durable",
          "after_active_file_fsync","after_active_link","after_active_directory_fsync","after_active_durable",
          "after_complete_file_fsync","after_complete_link","after_complete_directory_fsync")
  for point in points:
   with self.subTest(point=point),tempfile.TemporaryDirectory() as temp:
    root=Path(temp)/"maintenance";w=M.Writer(root);f=self.fixture()
    def fault(current,point=point):
     if current==point:raise OSError("injected")
    with self.assertRaises(M.Refusal):w.create_prepare_once(*f,fault=fault)
    self.assertTrue(any((root/"attempts").glob("*-intent.json")))
    with self.assertRaises(M.Refusal):w.create_prepare_once(*f)
 def test_pre_intent_sidecar_fault_requires_explicit_recovery(self):
  for point in ("after_sidecar_file_fsync","after_sidecar_link","after_sidecar_directory_fsync"):
   with self.subTest(point=point),tempfile.TemporaryDirectory() as temp:
    root=Path(temp)/"maintenance";w=M.Writer(root);f=self.fixture()
    def fault(current,point=point):
     if current==point:raise OSError("injected")
    with self.assertRaises(M.Refusal):w.create_prepare_once(*f,fault=fault)
    self.assertFalse((root/"active.json").exists())
    with self.assertRaises(M.Refusal):w.create_prepare_once(*f)
 def test_unpublished_intent_temp_can_only_reconcile_same_bytes(self):
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp)/"maintenance";w=M.Writer(root);f=self.fixture()
   def fault(point):
    if point=="after_intent_file_fsync":raise OSError("injected")
   with self.assertRaises(M.Refusal):w.create_prepare_once(*f,fault=fault)
   self.assertFalse(any((root/"attempts").glob("*-intent.json")))
   with self.assertRaises(M.Refusal):w.create_prepare_once(*f)
 def test_new_operation_id_cannot_bypass_pending_reservation(self):
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp)/"maintenance";w=M.Writer(root);f=list(self.fixture())
   def fault(point):
    if point=="after_intent_durable":raise OSError("injected")
   with self.assertRaises(M.Refusal):w.create_prepare_once(*f,fault=fault)
   f[2]=copy.deepcopy(f[2]);f[2]["operation_id"]="8"*32
   with self.assertRaises(M.Refusal):w.create_prepare_once(*f)
 def test_namespace_replacement_after_effect_never_reports_success(self):
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp)/"maintenance";moved=Path(temp)/"moved";w=M.Writer(root);f=self.fixture()
   def fault(point):
    if point=="after_active_durable":root.rename(moved);root.mkdir(mode=0o700)
   with self.assertRaises(M.Refusal):w.create_prepare_once(*f,fault=fault)
   self.assertFalse((root/"active.json").exists());self.assertTrue((moved/"active.json").exists())
 def test_lock_timeout_closes_writer_fds(self):
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp)/"maintenance";w=M.Writer(root);w._ensure_root();lock=os.open(root/".maintenance.lock",os.O_RDWR|os.O_CREAT,0o600);fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
   before=len(os.listdir("/proc/self/fd"))
   with self.assertRaises(Exception):w.create_prepare_once(*self.fixture(),timeout=0.01)
   self.assertEqual(len(os.listdir("/proc/self/fd")),before)
   os.close(lock);self.assertEqual(w.create_prepare_once(*self.fixture())["classification"],"PREPARE_HOLD_CREATED")
 def test_foreign_sidecar_projection_refuses_before_root_creation(self):
  mutations=(lambda s:s.update(tx="../escape"),lambda s:s.update(generation=2),lambda s:s["actions"][0].update(action="VERIFY_CURRENT_ONLY"),lambda s:s["actions"].pop(),lambda s:s["plan"].update(context_sha256="0"*64))
  for mutate in mutations:
   with self.subTest(mutate=mutate),tempfile.TemporaryDirectory() as temp:
    root=Path(temp)/"maintenance";f=list(self.fixture());f[1]=copy.deepcopy(f[1]);mutate(f[1])
    with self.assertRaises(M.Refusal):M.Writer(root).create_prepare_once(*f)
    self.assertFalse(root.exists())
 def test_foreign_unarchived_evidence_blocks_new_transaction(self):
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp)/"maintenance";w=M.Writer(root);w._ensure_root();(root/"attempts").mkdir(mode=0o700);foreign=root/"attempts/foreign-intent.json";foreign.write_bytes(b"foreign");foreign.chmod(0o600)
   with self.assertRaises(M.Refusal):w.create_prepare_once(*self.fixture())
   self.assertFalse((root/"active.json").exists())
 def test_expiry_after_active_temp_fsync_prevents_publication(self):
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp)/"maintenance";f=list(self.fixture());deadline=min(f[0]["expires_at"],f[2]["expires_at"])
   with patch.object(M.time,"time",side_effect=[deadline,deadline+1]):
    with self.assertRaises(M.Refusal):M.Writer(root).create_prepare_once(*f)
   self.assertFalse((root/"active.json").exists());self.assertTrue((root/".active.json.tmp").exists())
 def test_callback_cannot_expire_authority_after_last_guard(self):
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp)/"maintenance";f=list(self.fixture());deadline=min(f[0]["expires_at"],f[2]["expires_at"]);clock=[deadline]
   def fault(point):
    if point=="after_active_file_fsync":clock[0]=deadline+1
   with patch.object(M.time,"time",side_effect=lambda:clock[0]):
    with self.assertRaises(M.Refusal):M.Writer(root).create_prepare_once(*f,fault=fault)
   self.assertFalse((root/"active.json").exists())
 def test_sidecar_and_intent_identity_survive_until_success(self):
  for mode in ("delete-sidecar","replace-sidecar","delete-intent","replace-intent"):
   with self.subTest(mode=mode),tempfile.TemporaryDirectory() as temp:
    root=Path(temp)/"maintenance";w=M.Writer(root);f=self.fixture()
    def fault(point,mode=mode):
     trigger="after_complete_file_fsync" if "intent" in mode else "after_active_durable"
     if point!=trigger:return
     folder=root/("attempts" if "intent" in mode else "sidecars");pattern="*-intent.json" if "intent" in mode else "*.json";path=next(folder.glob(pattern));raw=path.read_bytes();path.unlink()
     if mode.startswith("replace"):
      path.write_bytes(raw);path.chmod(0o600)
    def run():return w.create_prepare_once(*f,fault=fault)
    with self.assertRaises(M.Refusal):run()
 def test_transition_is_atomic_file_only_and_one_shot(self):
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp)/"maintenance";f=self.transition_fixture(root);out=f[0].transition_config_committed_once(*f[1:8])
   self.assertEqual(out["classification"],"LOCAL_CONFIG_COMMITTED_HOLD");self.assertFalse(out["rollout_authorized"]);self.assertEqual((root/"active.json").read_bytes(),M.canonical(f[8]))
   with self.assertRaises(M.Refusal):f[0].transition_config_committed_once(*f[1:8])
 def test_transition_crash_matrix_never_removes_active_or_retries(self):
  points=("after_transition_evidence_file_fsync","after_transition_evidence_link","after_transition_evidence_directory_fsync",
          "after_transition_intent_file_fsync","after_transition_intent_link","after_transition_intent_directory_fsync","after_transition_intent_durable",
          "after_transition_active_file_fsync","after_transition_active_link","after_transition_active_directory_fsync","before_transition_replace",
          "after_transition_directory_fsync","after_transition_complete_file_fsync","after_transition_complete_link","after_transition_complete_directory_fsync")
  for point in points:
   with self.subTest(point=point),tempfile.TemporaryDirectory() as temp:
    root=Path(temp)/"maintenance";f=self.transition_fixture(root);old=M.canonical(f[1]);new=M.canonical(f[8])
    def fault(current,point=point):
     if current==point:raise OSError("injected")
    with self.assertRaises(M.Refusal):f[0].transition_config_committed_once(*f[1:8],fault=fault)
    self.assertTrue((root/"active.json").is_file());self.assertIn((root/"active.json").read_bytes(),(old,new))
    with self.assertRaises(M.Refusal):f[0].transition_config_committed_once(*f[1:8])
 def test_transition_refuses_incomplete_proof_or_control_node_before_effect(self):
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp)/"maintenance";f=list(self.transition_fixture(root));f[6]=copy.deepcopy(f[6]);f[6]["nodes"].pop();f[3]=copy.deepcopy(f[3]);f[3]["proof_sha256"]=M.digest(M.canonical(f[6]))
   with self.assertRaises(M.Refusal):f[0].transition_config_committed_once(*f[1:8])
   self.assertEqual((root/"active.json").read_bytes(),M.canonical(f[1]))
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp)/"maintenance";manifest,sidecar,prepare,facts,now=self.fixture("pve04");successor=copy.deepcopy(manifest);successor["phase"]="CONFIG_COMMITTED"
   auth={"schema":"slt-live-hold-transition-authorization/v1","tx":manifest["tx"],"generation":manifest["generation"],"context_sha256":sidecar["context_sha256"],"old_manifest_sha256":M.digest(M.canonical(manifest)),"new_manifest_sha256":M.digest(M.canonical(successor)),"node":"pve04","boot_id":facts["boot_id"],"action":"transition-config-committed-once","operation_id":"8"*32,"prepare_operation_id":prepare["operation_id"],"proof_sha256":"7"*64,"preinst_sha256":"6"*64,"issued_at":now-10,"expires_at":now+300}
   with self.assertRaises(M.Refusal):M.Writer(root).transition_config_committed_once(manifest,sidecar,auth,facts,{}, {},now)
   self.assertFalse(root.exists())
 def test_transition_refuses_forged_prepare_predecessor(self):
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp)/"maintenance";f=self.transition_fixture(root);complete=next((root/"attempts").glob("*-complete.json"));complete.write_bytes(b"{}");complete.chmod(0o600)
   with self.assertRaises(M.Refusal):f[0].transition_config_committed_once(*f[1:8])
   self.assertEqual((root/"active.json").read_bytes(),M.canonical(f[1]))
 def test_transition_refuses_temp_source_tamper_without_replacing_active(self):
  for mode in ("delete","replace","symlink"):
   with self.subTest(mode=mode),tempfile.TemporaryDirectory() as temp:
    root=Path(temp)/"maintenance";f=self.transition_fixture(root);old=M.canonical(f[1])
    def fault(point,mode=mode):
     if point!="before_transition_replace":return
     path=next(root.glob(".*-active.tmp"));path.unlink()
     if mode=="replace":path.write_bytes(M.canonical(f[8]));path.chmod(0o600)
     elif mode=="symlink":path.symlink_to(root/"active.json")
    with self.assertRaises(M.Refusal):f[0].transition_config_committed_once(*f[1:8],fault=fault)
    self.assertEqual((root/"active.json").read_bytes(),old)
 def test_transition_never_succeeds_after_evidence_inode_tamper(self):
  for point in ("before_transition_replace","after_transition_directory_fsync","after_transition_complete_file_fsync"):
   with self.subTest(point=point),tempfile.TemporaryDirectory() as temp:
    root=Path(temp)/"maintenance";f=self.transition_fixture(root)
    def fault(current,point=point):
     if current!=point:return
     path=next((root/"attempts").glob("*-create-prepare-*-complete.json"));raw=path.read_bytes();path.unlink();path.write_bytes(raw);path.chmod(0o600)
    with self.assertRaises(M.Refusal):f[0].transition_config_committed_once(*f[1:8],fault=fault)
    self.assertIn((root/"active.json").read_bytes(),(M.canonical(f[1]),M.canonical(f[8])))
 def test_transition_rejects_non_integer_generations(self):
  for target,index in (("preinst",5),("proof",6)):
   for value in (True,1.0):
    with self.subTest(target=target,value=value),tempfile.TemporaryDirectory() as temp:
     root=Path(temp)/"maintenance";f=list(self.transition_fixture(root));f[index]=copy.deepcopy(f[index]);f[index]["generation"]=value
     if target=="preinst":
      f[3]=copy.deepcopy(f[3]);f[3]["preinst_sha256"]=M.digest(M.canonical(f[index])+b"\n")
     else:
      f[3]=copy.deepcopy(f[3]);f[3]["proof_sha256"]=M.digest(M.canonical(f[index]))
     with self.assertRaises(M.Refusal):f[0].transition_config_committed_once(*f[1:8])
     self.assertEqual((root/"active.json").read_bytes(),M.canonical(f[1]))
 def test_transition_expiry_during_last_active_read_prevents_replace(self):
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp)/"maintenance";f=self.transition_fixture(root);deadline=f[3]["expires_at"];clock=[deadline];original=M.BASE._read_regular;active_reads=[0]
   def wrapped(*args,**kwargs):
    result=original(*args,**kwargs)
    if len(args)>1 and args[1]=="active.json":
     active_reads[0]+=1
     if active_reads[0]==2:clock[0]=deadline+1
    return result
   with patch.object(M.time,"time",side_effect=lambda:clock[0]),patch.object(M.BASE,"_read_regular",side_effect=wrapped):
    with self.assertRaises(M.Refusal):f[0].transition_config_committed_once(*f[1:8])
   self.assertEqual((root/"active.json").read_bytes(),M.canonical(f[1]));self.assertGreaterEqual(active_reads[0],2)
if __name__=="__main__":unittest.main()
