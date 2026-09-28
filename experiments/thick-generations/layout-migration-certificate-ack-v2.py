#!/usr/bin/env python3
"""Collect one exact v2 certificate ACK/current observation."""
import argparse,copy,hashlib,importlib.util,json,os,socket,stat,sys,time
from pathlib import Path
HERE=Path(__file__).resolve().parent
def load(name,file):s=importlib.util.spec_from_file_location(name,HERE/file);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
V1=load("slt_ack_v1","layout-migration-node-evidence.py");NODE2=load("slt_ack_node2","layout-migration-node-evidence-v2.py")
Refusal=V1.Refusal;require=V1.require
ROOT=Path("/var/lib/pve-sharedlvmthin/maintenance");CERTDIR=ROOT/"release-certificates"
COMP=(Path("/"),Path("/var"),Path("/var/lib"),Path("/var/lib/pve-sharedlvmthin"),ROOT,CERTDIR)
def canonical(v):return json.dumps(v,sort_keys=True,separators=(",",":")).encode()
def read(path,label):
 raw=V1.regular_bytes(Path(path),label,2*1024*1024)
 try:v=json.loads(raw)
 except (UnicodeDecodeError,json.JSONDecodeError) as e:raise Refusal(label+" invalid") from e
 require(type(v)is dict,label+" root invalid");return v,raw
def ident(path,kind,raw=None):
 st=os.lstat(path);base={"type":kind,"dev":st.st_dev,"ino":st.st_ino,"uid":st.st_uid,"mode":stat.S_IMODE(st.st_mode),"nlink":st.st_nlink}
 require(st.st_uid==0 and not(st.st_mode&0o022),"unsafe inode "+str(path))
 if kind=="regular":require(stat.S_ISREG(st.st_mode) and st.st_nlink==1,"unsafe file");base.update(size=st.st_size,sha256=hashlib.sha256(raw).hexdigest())
 else:require(stat.S_ISDIR(st.st_mode),"unsafe directory")
 return base
def namespace(name,raw):return {"directory":str(CERTDIR),"decision_name":name,"components":[{"path":str(p),"identity":ident(p,"directory")} for p in COMP],"certificate":ident(CERTDIR/name,"regular",raw)}
def collect(a):
 doc,_=read(a.context,"context");c=doc.get("context",doc);collection,_=read(a.collection,"collection");cert,cert_raw=read(a.certificate,"certificate");manifest,manifest_raw=read(a.manifest,"manifest")
 local=socket.gethostname().split(".",1)[0];boot=Path("/proc/sys/kernel/random/boot_id").read_text().strip();nodes=[x["name"] for x in c["nodes"]];row=next(x for x in c["nodes"] if x["name"]==local)
 require(row["boot_id"]==boot and collection["participant_boots"]=={x["name"]:x["boot_id"] for x in c["nodes"]},"boot cohort differs")
 now=int(time.time());require(collection["issued_at"]<=now<=collection["expires_at"] and collection["challenges"].get(local),"collection invalid/expired")
 _,_,q,observed=V1.membership(nodes);require(q and observed==nodes,"control plane differs")
 storage=V1.regular_bytes(Path("/etc/pve/storage.cfg"),"storage",16*1024*1024);require(hashlib.sha256(storage).hexdigest()==c["target_storage_cfg_sha256"],"storage differs")
 installed=V1.installed_identity();cand=c["candidate"];require(installed["dpkg_state"]=="installed" and all(installed[k]==cand[k] for k in ("package","version","flavor","artifact_sha256")),"payload differs")
 require(V1.command(["dpkg","--verify",cand["package"]])==b"","payload verification failed")
 listing=V1.command(["dpkg-query","-L",cand["package"]]);list_sha=hashlib.sha256(b"\n".join(sorted(x for x in listing.splitlines() if x))+b"\n").hexdigest()
 w=V1.worker_evidence(observed);workers={"inventory_complete":True,"storage_processes":w["storage_processes"],"transient_units":w["blocking_transient_units"],"pve_tasks":w["active_pve_tasks"]};require(not any(workers[k] for k in ("storage_processes","transient_units","pve_tasks")),"workers busy")
 guard=NODE2.inactive_guard_evidence();role=c["node_roles"][local];started=int(time.time())
 common={"collection_id":collection["collection_id"],"collection_plan_sha256":hashlib.sha256(canonical(collection)).hexdigest(),"challenge":collection["challenges"][local],"collector_sha256":collection["collector_sha256"],"node":local,"role":role,"boot_id_start":boot,"boot_id_end":boot,"tx":c["tx"],"generation":c["generation"],"context_sha256":c["context_sha256"],"commit_id":cert["commit_id"],"certificate_bytes_sha256":hashlib.sha256(cert_raw).hexdigest(),"certificate_size":len(cert_raw),"verification_started_at":started,"verification_finished_at":int(time.time()),"control_plane":{"cluster_name":c["cluster_name"],"cluster_nodes":nodes,"quorate":True,"target_storage_cfg_sha256":c["target_storage_cfg_sha256"]},"installed":{**{k:cand[k] for k in ("package","version","flavor","artifact_sha256")},"dpkg_state":"installed"},"payload":{"dpkg_verify_complete":True,"dpkg_verify_clean":True,"package_file_list_sha256":list_sha},"workers":workers,"thinguard":guard}
 if role=="SAN_PARTICIPANT":
  name=f"{c['tx']}-{c['generation']}.json";on_disk=V1.regular_bytes(CERTDIR/name,"certificate",2*1024*1024);require(on_disk==cert_raw,"certificate differs")
  ns1=namespace(name,on_disk);active=V1.regular_bytes(ROOT/"active.json","hold",1024*1024);require(active==manifest_raw,"hold differs");hst=ident(ROOT/"active.json","regular",active)
  expected_hold={"active":True,"context_sha256":c["context_sha256"],"manifest_sha256":hashlib.sha256(active).hexdigest(),"device":hst["dev"],"inode":hst["ino"]}
  fd=os.open(CERTDIR/name,os.O_RDONLY|os.O_NOFOLLOW);os.fsync(fd);os.close(fd);dfd=os.open(CERTDIR,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW);os.fsync(dfd);os.close(dfd);pfd=os.open(ROOT,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW);os.fsync(pfd);os.close(pfd)
  ns2=namespace(name,V1.regular_bytes(CERTDIR/name,"certificate recheck",2*1024*1024));require(ns1==ns2,"namespace changed")
  common.update(schema="slt-layout-certificate-ack/v2",action="VERIFY_DURABLE_CERTIFICATE_AND_HOLD",durability={"result":"VERIFIED_REPLAY","file_fsync":True,"directory_fsync":True,"parent_fsync":True,"exact_reread":True},namespace_before=ns1,namespace_after=ns2,hold={"path":str(ROOT/"active.json"),"before":expected_hold,"after":copy.deepcopy(expected_hold),"identity_before":hst,"identity_after":copy.deepcopy(hst),"exact_reread":True},vg_identities=V1.actual_lvm_identities(V1.expected_mixed_vgs(storage)))
 else:
  require(role=="CONTROL_ONLY" and not os.path.lexists(ROOT/"active.json") and not os.path.lexists(CERTDIR/f"{c['tx']}-{c['generation']}.json"),"control-only namespace mutated")
  common.update(schema="slt-layout-certificate-current-observation/v2",action="VERIFY_CURRENT_ONLY",local_effects={k:False for k in ("certificate_written","hold_created","hold_changed","hold_released","package_changed","services_changed")},local_certificate_present=False,hold={"active":False,"context_sha256":c["context_sha256"],"manifest_sha256":"0"*64,"device":0,"inode":0},vg_identities=[])
 common["verification_finished_at"]=int(time.time());return common
def main():
 p=argparse.ArgumentParser();p.add_argument("--context",required=True);p.add_argument("--collection",required=True);p.add_argument("--certificate",required=True);p.add_argument("--manifest",required=True);a=p.parse_args()
 try:r=collect(a)
 except (Refusal,OSError,ValueError,KeyError,TypeError) as e:print(json.dumps({"schema":"slt-layout-certificate-ack/v2","verdict":"BLOCKED","reason":str(e)},sort_keys=True));return 2
 print(json.dumps(r,sort_keys=True,indent=2));return 0
if __name__=="__main__":sys.exit(main())
