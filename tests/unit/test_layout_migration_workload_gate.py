import importlib.util,json,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
P=ROOT/'experiments/thick-generations/layout-migration-workload-gate.py'
S=importlib.util.spec_from_file_location('workload_gate',P); G=importlib.util.module_from_spec(S); S.loader.exec_module(G)

class GateTests(unittest.TestCase):
 def setUp(self):
  self.t=tempfile.TemporaryDirectory(); self.r=Path(self.t.name); (self.r/'nodes/n1/qemu-server').mkdir(parents=True)
  self.storage=self.r/'storage.cfg'; self.storage.write_text('sharedlvmthin: thin\n\tslt-vgname vg\n')
  self.resources=self.r/'resources.json'
 def tearDown(self): self.t.cleanup()
 def snapshot(self,status='stopped',vmid=100,node='n1'):
  self.resources.write_text(json.dumps({'schema':'slt-pve-resources/v1','observed_at':1000,'resources':[{'vmid':vmid,'type':'qemu','node':node,'status':status,'name':'test'}]}))
 def config(self,text='scsi0: thin:vm-100-disk-0,size=1G\n'):
  (self.r/'nodes/n1/qemu-server/100.conf').write_text(text)
 def evaluate(self): return G.evaluate(self.storage,self.r/'nodes',self.resources,1010,120)
 def test_stopped_consumer_is_ready(self):
  self.snapshot(); self.config(); self.assertEqual(self.evaluate()['verdict'],'SNAPSHOT_READY')
 def test_running_consumer_blocks(self):
  self.snapshot('running'); self.config(); out=self.evaluate(); self.assertEqual(out['verdict'],'BLOCKED'); self.assertEqual(out['running_consumers'][0]['vmid'],100)
 def test_snapshot_section_is_not_live_config(self):
  self.snapshot(); self.config('[snap]\nscsi0: thin:vm-100-old,size=1G\n'); self.assertEqual(self.evaluate()['consumers'],[])
 def test_efi_tpm_and_file_property_block(self):
  for key,value in [('efidisk0','thin:vm-100-efi'),('tpmstate0','thin:vm-100-tpm'),('scsi0','file=thin:vm-100-disk-0,size=1G'),('scsi0','cache=none,file=thin:vm-100-mid,size=1G'),('scsi0','cache=none,size=1G,file=thin:vm-100-last')]:
   self.snapshot('running'); self.config(f'{key}: {value}\n')
   self.assertEqual(self.evaluate()['verdict'],'BLOCKED')
 def test_multiple_disk_identities_refuse(self):
  self.snapshot('running')
  for value in ('file=thin:a,file=thin:b','thin:a,file=thin:b'):
   self.config(f'scsi0: {value}\n')
   with self.assertRaisesRegex(G.Refusal,'conflicting'): self.evaluate()
 def test_running_lxc_rootfs_and_mountpoint_block(self):
  qdir=self.r/'nodes/n1/qemu-server'; (qdir/'100.conf').unlink(missing_ok=True)
  ldir=self.r/'nodes/n1/lxc'; ldir.mkdir()
  for key in ('rootfs','mp0'):
   self.resources.write_text(json.dumps({'schema':'slt-pve-resources/v1','observed_at':1000,'resources':[{'vmid':100,'type':'lxc','node':'n1','status':'running','name':'ct'}]}))
   (ldir/'100.conf').write_text(f'{key}: thin:subvol-100-disk-0,size=1G\n')
   out=self.evaluate(); self.assertEqual(out['verdict'],'BLOCKED'); self.assertEqual(out['running_consumers'][0]['type'],'lxc')
 def test_type_and_node_mismatch_refuse_even_without_managed_disk(self):
  self.config('scsi0: local:vm-100-disk-0\n')
  self.snapshot('running',node='n2')
  with self.assertRaisesRegex(G.Refusal,'type/node'): self.evaluate()
  self.snapshot('running')
  data=json.loads(self.resources.read_text()); data['resources'][0]['type']='lxc'; self.resources.write_text(json.dumps(data))
  with self.assertRaisesRegex(G.Refusal,'type/node'): self.evaluate()
 def test_missing_resource_refuses(self):
  self.resources.write_text(json.dumps({'schema':'slt-pve-resources/v1','observed_at':1000,'resources':[]})); self.config()
  with self.assertRaisesRegex(G.Refusal,'absent'): self.evaluate()
 def test_unknown_status_refuses(self):
  self.snapshot('unknown'); self.config()
  with self.assertRaisesRegex(G.Refusal,'unknown'): self.evaluate()
 def test_stale_snapshot_refuses(self):
  self.snapshot(); self.config()
  with self.assertRaisesRegex(G.Refusal,'stale'): G.evaluate(self.storage,self.r/'nodes',self.resources,2000,120)
 def test_duplicate_vm_config_refuses(self):
  self.snapshot(); self.config(); (self.r/'nodes/n2/qemu-server').mkdir(parents=True); (self.r/'nodes/n2/qemu-server/100.conf').write_text('scsi0: thin:vm-100-disk-0\n')
  with self.assertRaisesRegex(G.Refusal,'more than once'): self.evaluate()
 def test_running_resource_without_config_refuses(self):
  self.snapshot('running')
  with self.assertRaisesRegex(G.Refusal,'empty|coverage'): self.evaluate()
 def test_duplicate_live_disk_key_refuses(self):
  self.snapshot(); self.config('scsi0: thin:vm-100-a\nscsi0: thin:vm-100-b\n')
  with self.assertRaisesRegex(G.Refusal,'duplicate'): self.evaluate()
if __name__=='__main__': unittest.main()
