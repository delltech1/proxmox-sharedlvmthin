import json,pathlib,unittest,importlib.util
ROOT=pathlib.Path(__file__).resolve().parents[2]
class RollingCandidateTests(unittest.TestCase):
 def setUp(self):
  self.doc=json.loads((ROOT/'usr/share/pve-sharedlvmthin/pve-qualified-tuples.json').read_text())
  spec=importlib.util.spec_from_file_location('policy',ROOT/'usr/libexec/pve-sharedlvmthin/sharedlvmthin_update_policy.py')
  self.policy=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.policy)
 def test_old_api16_runtime_is_listed_not_qualified(self):
  rows=[x for x in self.doc['tuples'] if '0.9.0~rc5.101~tg53' in x.get('plugin_versions',[]) and x['api']==16 and x['running_kernel']=='7.0.14-16-pve' and x['packages']['pve-manager']=='9.2.18' and x['packages']['libpve-storage-perl']=='9.1.12']
  self.assertEqual(len(rows),1);r=rows[0]
  self.assertEqual(r['status'],'RETEST_REQUIRED');self.assertIn('san-dataplane',r['excluded_scopes'])
  for profile in ('dual','thick-only'):
   self.assertIsNone(self.policy.exact_tuple(self.doc,r['packages'],profile=profile,api=16,running_kernel=r['running_kernel'],plugin_version='0.9.0~rc5.101~tg53',scope='san-dataplane'))
 def test_new_rolling_edge_is_unqualified(self):
  e=next(x for x in self.doc['rolling_edges'] if x.get('id')=='rc5101-api15-api16-k28-update')
  self.assertEqual(e['status'],'RETEST_REQUIRED');self.assertEqual(e['directions'],[])
 def test_new_candidate_preserves_previous_identity_and_refuses_san_qualification(self):
  versions=('0.9.0~rc5.101~tg53','0.9.0~rc5.102~tg53')
  rows=[r for r in self.doc['tuples'] if all(v in r.get('plugin_versions',[]) for v in versions)]
  self.assertEqual(len(rows),10)
  for row in rows:
   self.assertEqual(row['status'],'RETEST_REQUIRED')
   for version in versions:
    for profile in row['profiles']:
     self.assertIsNone(self.policy.exact_tuple(self.doc,row['packages'],profile=profile,api=row['api'],running_kernel=row['running_kernel'],plugin_version=version,scope='san-dataplane'))
if __name__=='__main__':unittest.main()
