import copy,hashlib,importlib.util,unittest
from pathlib import Path
P=Path(__file__).resolve().parents[2]/"experiments/thick-generations/layout-migration-v2-context.py"
S=importlib.util.spec_from_file_location("ctx",P);M=importlib.util.module_from_spec(S);S.loader.exec_module(M)
class Tests(unittest.TestCase):
 def baseline(self,scope="pve01,pve02,pve03"):
  parts=[]
  for vg,suffix in (("vg-a","a"),("vg-b","b")):
   common=f"\tslt-vgname {vg}\n\tnodes {scope}\n\tslt-thin-leaseguard runtime-guard\n\tslt-expected-vg-uuid vg-uuid-{suffix}\n\tslt-expected-pv-uuid pv-uuid-{suffix}\n\tslt-expected-wwid wwid-{suffix}\n"
   parts += [f"sharedlvmthin: thin-{suffix}\n{common}\tslt-allocation-mode thin\n",f"sharedlvmthin: thick-{suffix}\n{common}\tslt-allocation-mode thick-generations\n"]
  return "".join(parts).encode()
 def template(self):
  return {"schema":"slt-layout-topology/v2","cluster_name":"lab","nodes":[{"name":f"pve0{i}","boot_id":f"0000000{i}-0000-4000-8000-00000000000{i}","san_role":"SAN_PARTICIPANT" if i<4 else "CONTROL_ONLY"} for i in range(1,5)]}
 def candidate(self):return {"package":"pve-sharedlvmthin","version":"0.9.0~rc5.13~tg34","flavor":"dual","architecture":"all","deb_sha256":"a"*64,"artifact_sha256":"b"*64}
 def build(self):
  base=self.baseline();target,_=M.PLAN.target_layout(base)
  return base,target,M.build_context({"tx":"c"*32,"generation":1},self.candidate(),self.template(),base,target)
 def test_context_binds_four_roles_two_configs_and_exact_identities(self):
  base,target,value=self.build();self.assertEqual(M.validate_context(value,self.template(),base,target),value)
  self.assertEqual(value["node_roles"]["pve04"],"CONTROL_ONLY");self.assertEqual(value["expected_vg_identities_by_node"]["pve04"],[])
  self.assertTrue(value["thinguard_required_by_node"]["pve01"]);self.assertFalse(value["thinguard_required_by_node"]["pve04"])
  self.assertEqual(len(value["expected_vg_identities_by_node"]["pve01"]),2);self.assertNotEqual(value["baseline_storage_cfg_sha256"],value["target_storage_cfg_sha256"])
 def test_self_hashed_scope_tamper_cannot_override_config_bytes(self):
  base,target,value=self.build();bad=copy.deepcopy(value);bad["managed_storage_scopes"][0]["nodes"].append("pve04")
  body=copy.deepcopy(bad);body.pop("context_sha256");bad["context_sha256"]=M.digest(body)
  with self.assertRaises(M.Refusal):M.validate_context(bad,self.template(),base,target)
 def test_target_scope_expansion_or_extra_change_refuses(self):
  base,target,_=self.build()
  for bad in (target.replace(b"pve01,pve02,pve03",b"pve01,pve02,pve03,pve04",1),target+b"# foreign\n"):
   with self.assertRaises(M.Refusal):M.build_context({"tx":"c"*32,"generation":1},self.candidate(),self.template(),base,bad)
 def test_semantically_exact_renderer_target_is_bound(self):
  base,target,_=self.build();needle=b"\tslt-vgname vg-a\n\tnodes pve01,pve02,pve03\n";replacement=b"\tnodes pve01,pve02,pve03\n\tslt-vgname vg-a\n";rendered=target.replace(needle,replacement,1)
  self.assertNotEqual(target,rendered);value=M.build_context({"tx":"c"*32,"generation":1},self.candidate(),self.template(),base,rendered)
  self.assertEqual(value["target_storage_cfg_sha256"],hashlib.sha256(rendered).hexdigest())
 def test_mixed_vg_scope_must_cover_every_san_node(self):
  base=self.baseline("pve01,pve02");target,_=M.PLAN.target_layout(base)
  with self.assertRaises(M.Refusal):M.build_context({"tx":"c"*32,"generation":1},self.candidate(),self.template(),base,target)
 def test_mixed_alias_scopes_must_match(self):
  base=self.baseline();needle=b"\tnodes pve01,pve02,pve03\n";base=base.replace(needle,b"\tnodes pve01,pve02\n",1);target,_=M.PLAN.target_layout(base)
  with self.assertRaises(M.Refusal):M.build_context({"tx":"c"*32,"generation":1},self.candidate(),self.template(),base,target)
 def test_role_swap_and_rehashed_context_refuses(self):
  base,target,value=self.build();template=self.template();template["nodes"][0]["san_role"]="CONTROL_ONLY";template["nodes"][3]["san_role"]="SAN_PARTICIPANT"
  bad=copy.deepcopy(value);bad["nodes"]=copy.deepcopy(template["nodes"]);bad["node_roles"]={x["name"]:x["san_role"] for x in template["nodes"]};body=copy.deepcopy(bad);body.pop("context_sha256");bad["context_sha256"]=M.digest(body)
  with self.assertRaises(M.Refusal):M.validate_context(bad,self.template(),base,target)
 def test_candidate_boolean_generation_and_changed_target_refuse(self):
  base,target,_=self.build()
  with self.assertRaises(M.Refusal):M.build_context({"tx":"c"*32,"generation":True},self.candidate(),self.template(),base,target)
  candidate=self.candidate();candidate["deb_sha256"]=None
  with self.assertRaises(M.Refusal):M.build_context({"tx":"c"*32,"generation":1},candidate,self.template(),base,target)
if __name__=="__main__":unittest.main()
