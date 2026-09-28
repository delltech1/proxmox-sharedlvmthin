import copy,hashlib,importlib.util,unittest
from pathlib import Path
P=Path(__file__).resolve().parents[2]/"experiments/thick-generations/layout-migration-topology-v2.py"
S=importlib.util.spec_from_file_location("topology",P);M=importlib.util.module_from_spec(S);S.loader.exec_module(M)
class Tests(unittest.TestCase):
 def storage(self,global_storage=False,include_control=False):
  nodes="pve01,pve02,pve03"+(",pve04" if include_control else "")
  scope="" if global_storage else f"\tnodes {nodes}\n"
  return ("sharedlvmthin: thin\n\tslt-vgname vg\n\tslt-thin-leaseguard runtime-guard\n"+scope+"sharedlvmthin: thick\n\tslt-vgname vg\n\tslt-thin-leaseguard runtime-guard\n"+scope).encode()
 def topology(self,raw):
  return {"schema":"slt-layout-topology/v2","cluster_name":"lab","storage_cfg_sha256":hashlib.sha256(raw).hexdigest(),"nodes":[{"name":f"pve0{i}","boot_id":f"0000000{i}-0000-4000-8000-00000000000{i}","san_role":"SAN_PARTICIPANT" if i<4 else "CONTROL_ONLY"} for i in range(1,5)]}
 def test_three_san_one_control_binds_even_when_control_sorts_first(self):
  raw=self.storage();t=self.topology(raw);t["nodes"][0]["name"]="aaa-control";t["nodes"][0]["san_role"]="CONTROL_ONLY";t["nodes"][3]["name"]="pve01";t["nodes"][3]["san_role"]="SAN_PARTICIPANT";t["nodes"].sort(key=lambda x:x["name"])
  # Storage must bind the resulting three SAN names, not positional records.
  raw=b"sharedlvmthin: x\n\tnodes pve01,pve02,pve03\n";t["storage_cfg_sha256"]=hashlib.sha256(raw).hexdigest()
  self.assertEqual(M.validate(t,raw)["verdict"],"TOPOLOGY_BOUND")
 def test_global_or_control_scoped_storage_refuses(self):
  for raw in (self.storage(global_storage=True),self.storage(include_control=True)):
   with self.assertRaises(M.Refusal):M.validate(self.topology(raw),raw)
 def test_role_counts_missing_and_unknown_refuse(self):
  raw=self.storage()
  for mutate in (lambda t:t["nodes"].pop(),lambda t:t["nodes"][0].update(san_role="CONTROL_ONLY"),lambda t:t["nodes"][0].update(san_role="AUTO")):
   t=self.topology(raw);mutate(t)
   with self.assertRaises(M.Refusal):M.validate(t,raw)
 def test_node_evidence_role_and_boot_are_exact(self):
  raw=self.storage();e=M.validate(self.topology(raw),raw);node=e["nodes"][0]
  r={"schema":"slt-layout-node-role-evidence/v2","node":node["name"],"boot_id":node["boot_id"],"san_role":"SAN_PARTICIPANT","topology_sha256":e["topology_sha256"],"vg_identities":[{"vg":"x"}],"thinguard":{"service_active_state":"active","daemon_pid":2,"daemon_starttime":3,"socket_inode":4,"samples":[{"state":"IDLE","watchdog":"DISARMED"},{"state":"IDLE","watchdog":"DISARMED"}]}}
  self.assertEqual(M.bind_node_evidence(r,e),r)
  for field,value in (("san_role","CONTROL_ONLY"),("boot_id","00000009-0000-4000-8000-000000000009"),("topology_sha256","f"*64)):
   bad=copy.deepcopy(r);bad[field]=value
   with self.assertRaises(M.Refusal):M.bind_node_evidence(bad,e)
 def test_control_requires_closed_inactive_guard_shape(self):
  raw=self.storage();e=M.validate(self.topology(raw),raw);node=e["nodes"][-1]
  good={"schema":"slt-layout-node-role-evidence/v2","node":node["name"],"boot_id":node["boot_id"],"san_role":"CONTROL_ONLY","topology_sha256":e["topology_sha256"],"vg_identities":[],"thinguard":{"service_active_state":"inactive","main_pid":0,"jobs":[],"daemon_processes":[],"watchdog_registrations":[]}}
  self.assertEqual(M.bind_node_evidence(good,e),good)
  for mutate in (lambda r:r.update(vg_identities=[{"vg":"x"}]),lambda r:r["thinguard"].update(main_pid=1),lambda r:r["thinguard"].update(jobs=["start"])):
   bad=copy.deepcopy(good);mutate(bad)
   with self.assertRaises(M.Refusal):M.bind_node_evidence(bad,e)
 def test_san_with_disabled_guard_requires_inactive_but_keeps_vg_evidence(self):
  raw=self.storage().replace(b"runtime-guard",b"remote-audit");e=M.validate(self.topology(raw),raw);node=e["nodes"][0]
  good={"schema":"slt-layout-node-role-evidence/v2","node":node["name"],"boot_id":node["boot_id"],"san_role":"SAN_PARTICIPANT","topology_sha256":e["topology_sha256"],"vg_identities":[{"vg":"x"}],"thinguard":{"service_active_state":"inactive","main_pid":0,"jobs":[],"daemon_processes":[],"watchdog_registrations":[]}}
  self.assertEqual(M.bind_node_evidence(good,e),good)
  for invalid in ([],None,{},"vg"):
   bad=copy.deepcopy(good);bad["vg_identities"]=invalid
   with self.assertRaises(M.Refusal):M.bind_node_evidence(bad,e)
  for alias in (False,0.0):
   bad=copy.deepcopy(good);bad["thinguard"]["main_pid"]=alias
   with self.assertRaises(M.Refusal):M.bind_node_evidence(bad,e)
 def test_evidence_is_detached_and_tampering_is_rejected(self):
  raw=self.storage();t=self.topology(raw);e=M.validate(t,raw);original=copy.deepcopy(e)
  t["nodes"][0]["san_role"]="CONTROL_ONLY";self.assertEqual(e,original)
  bad=copy.deepcopy(e);bad["nodes"][0]["san_role"]="CONTROL_ONLY"
  with self.assertRaises(M.Refusal):M.bind_node_evidence({"schema":"slt-layout-node-role-evidence/v2","node":"pve01","boot_id":e["nodes"][0]["boot_id"],"san_role":"CONTROL_ONLY","topology_sha256":e["topology_sha256"],"vg_identities":[],"thinguard":{"service_active_state":"inactive","main_pid":0,"jobs":[],"daemon_processes":[],"watchdog_registrations":[]}},bad)
 def test_rehashed_evidence_with_null_boot_is_rejected(self):
  raw=self.storage();e=M.validate(self.topology(raw),raw);bad=copy.deepcopy(e)
  bad["nodes"][0]["boot_id"]=None
  topology={"schema":"slt-layout-topology/v2","cluster_name":bad["cluster_name"],"nodes":bad["nodes"],"storage_cfg_sha256":bad["storage_cfg_sha256"]}
  bad["topology_sha256"]=M.digest(topology);body=copy.deepcopy(bad);body.pop("evidence_sha256");bad["evidence_sha256"]=M.digest(body)
  with self.assertRaises(M.Refusal):M.validate_topology_evidence(bad)
 def test_rehashed_evidence_with_empty_scope_is_rejected(self):
  raw=self.storage();e=M.validate(self.topology(raw),raw);bad=copy.deepcopy(e)
  bad["managed_storage_scopes"][0]["nodes"]=[]
  body=copy.deepcopy(bad);body.pop("evidence_sha256");bad["evidence_sha256"]=M.digest(body)
  with self.assertRaises(M.Refusal):M.validate_topology_evidence(bad)
 def test_rehashed_evidence_with_null_storage_hash_is_rejected(self):
  raw=self.storage();e=M.validate(self.topology(raw),raw);bad=copy.deepcopy(e)
  bad["storage_cfg_sha256"]=None
  topology={"schema":"slt-layout-topology/v2","cluster_name":bad["cluster_name"],"nodes":bad["nodes"],"storage_cfg_sha256":bad["storage_cfg_sha256"]}
  bad["topology_sha256"]=M.digest(topology);body=copy.deepcopy(bad);body.pop("evidence_sha256");bad["evidence_sha256"]=M.digest(body)
  with self.assertRaises(M.Refusal):M.validate_topology_evidence(bad)
if __name__=="__main__":unittest.main()
