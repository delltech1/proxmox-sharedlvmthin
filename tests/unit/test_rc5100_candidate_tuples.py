import json,pathlib,unittest,importlib.util
ROOT=pathlib.Path(__file__).resolve().parents[2]
class CandidateTupleTests(unittest.TestCase):
 def setUp(self):
  self.doc=json.loads((ROOT/'usr/share/pve-sharedlvmthin/pve-qualified-tuples.json').read_text())
  self.rows={x['id']:x for x in self.doc['tuples']}
  spec=importlib.util.spec_from_file_location('policy',ROOT/'usr/libexec/pve-sharedlvmthin/sharedlvmthin_update_policy.py')
  self.policy=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.policy)
 def test_unqualified_tuples_never_qualify_san(self):
  for ident in ('rc5100-api14-control','rc5100-api15-source','rc5100-api16-k26-source','rc5100-api16-k26-common927','rc5100-api16-k28-target'):
   r=self.rows[ident]
   self.assertEqual(r['status'],'RETEST_REQUIRED');self.assertIn('san-dataplane',r['excluded_scopes'])
   for profile in ('dual','thick-only'):
    matched=self.policy.exact_tuple(self.doc,r['packages'],profile=profile,api=r['api'],running_kernel=r['running_kernel'],plugin_version='0.9.0~rc5.100~tg53',scope='san-dataplane')
    self.assertIsNone(matched)
 def test_rolling_edge_has_no_qualified_direction(self):
  edge=next(x for x in self.doc['rolling_edges'] if x.get("id")=='rc5100-api15-api16-k28-update')
  self.assertEqual(edge['status'],'RETEST_REQUIRED');self.assertEqual(edge['directions'],[])
if __name__=='__main__':unittest.main()
