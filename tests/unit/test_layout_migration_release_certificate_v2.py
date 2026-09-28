import copy,importlib.util,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];P=ROOT/"experiments/thick-generations/layout-migration-release-certificate-v2.py"
S=importlib.util.spec_from_file_location("cert2",P);M=importlib.util.module_from_spec(S);S.loader.exec_module(M)
HS=importlib.util.spec_from_file_location("arh",ROOT/"tests/unit/test_layout_migration_all_ready_v2.py");H=importlib.util.module_from_spec(HS);HS.loader.exec_module(H)
class Tests(unittest.TestCase):
 def fixture(self):
  base,target,template,context,configured,refresh,records,now=H.Tests().fixture();ready=M.READY.evaluate(context,template,base,target,configured,refresh,records,now)
  boots={x["name"]:x["boot_id"] for x in context["nodes"]};release={"schema":"slt-layout-release-authorization/v2","tx":context["tx"],"generation":1,"context_sha256":context["context_sha256"],"all_ready_plan_sha256":M.digest(ready),"participant_boots":boots,"authorization_id":"d"*32,"commit_id":"e"*32,"coordinator":{"node":"pve01","boot_id":boots["pve01"]},"issued_at":now,"expires_at":now+300,"committed_at":now,"release_not_after":now+120,"allowed_effects":["persist-release-certificate-v2"]}
  return base,target,template,context,configured,refresh,ready,release,records,now
 def test_exact_fresh_inputs_make_non_releasing_certificate(self):
  b,t,tp,c,ac,ra,ready,rel,r,n=self.fixture();cert,payload=M.evaluate(c,tp,b,t,ac,ra,ready,rel,r,n);self.assertEqual(cert["phase"],"RELEASE_COMMITTED");self.assertEqual(cert["allowed_effects"],["archive-exact-active-context-v2"]);self.assertEqual(payload,M.canonical(cert)+b"\n");self.assertEqual([x["san_role"] for x in cert["participants"]].count("CONTROL_ONLY"),1);self.assertEqual(cert["release_nodes"],["pve01","pve02","pve03"]);self.assertEqual(cert["verify_only_nodes"],["pve04"]);self.assertEqual(M.hashlib.sha256(payload).hexdigest()==cert["certificate_sha256"],False)
 def test_ready_is_freshly_recomputed(self):
  b,t,tp,c,ac,ra,ready,rel,r,n=self.fixture();bad=copy.deepcopy(ready);bad["node_roles"]["pve04"]="SAN_PARTICIPANT";body=copy.deepcopy(bad);body.pop("plan_sha256");bad["plan_sha256"]=M.digest(body);rel["all_ready_plan_sha256"]=M.digest(bad)
  with self.assertRaises(M.Refusal):M.evaluate(c,tp,b,t,ac,ra,bad,rel,r,n)
 def test_python_numeric_aliases_cannot_masquerade_as_fresh_ready(self):
  b,t,tp,c,ac,ra,ready,rel,r,n=self.fixture()
  for field,alias in (("generation",True),("mutation_performed",0)):
   bad=copy.deepcopy(ready);bad[field]=alias;body=copy.deepcopy(bad);body.pop("plan_sha256");bad["plan_sha256"]=M.digest(body)
   auth=copy.deepcopy(rel);auth["all_ready_plan_sha256"]=M.digest(bad)
   with self.assertRaises(M.Refusal):M.evaluate(c,tp,b,t,ac,ra,bad,auth,r,n)
 def test_expiry_effect_expansion_reboot_and_alias_generation_refuse(self):
  b,t,tp,c,ac,ra,ready,rel,r,n=self.fixture();variants=[]
  for mutate in (lambda x:x.update(expires_at=n-1),lambda x:x["allowed_effects"].append("remove-hold"),lambda x:x["participant_boots"].update(pve04="0"*36),lambda x:x.update(generation=1.0)):
   bad=copy.deepcopy(rel);mutate(bad);variants.append(bad)
  for bad in variants:
   with self.assertRaises(M.Refusal):M.evaluate(c,tp,b,t,ac,ra,ready,bad,r,n)
 def test_release_must_follow_latest_ready_observation(self):
  b,t,tp,c,ac,ra,ready,rel,r,n=self.fixture();rel.update(issued_at=n-2,committed_at=n-2)
  with self.assertRaises(M.Refusal):M.evaluate(c,tp,b,t,ac,ra,ready,rel,r,n)
if __name__=="__main__":unittest.main()
