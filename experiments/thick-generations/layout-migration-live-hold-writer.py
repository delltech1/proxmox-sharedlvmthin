#!/usr/bin/env python3
"""Narrow file-only writer for one local PREPARE maintenance hold.

There is deliberately no SSH, package, pmxcfs, service, storage, transition or
release operation here.  A post-intent ambiguity is recovery-required and is
never retried automatically.
"""

import copy,fcntl,hashlib,importlib.machinery,importlib.util,json,os,re,stat,time
from pathlib import Path

HERE=Path(__file__).resolve().parent;ROOT=HERE.parents[1]
def load(name,path,loader=None):
 spec=importlib.util.spec_from_loader(name,loader(name,str(path))) if loader else importlib.util.spec_from_file_location(name,path)
 mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod);return mod
BASE=load("slt_hold_writer_file_base",HERE/"local-hold-release-file-lab.py")
CHECK=load("slt_hold_writer_checker",ROOT/"usr/libexec/pve-sharedlvmthin/sharedlvmthin-package-maintenance-check",importlib.machinery.SourceFileLoader)
Refusal=BASE.Refusal;require=BASE.require;SHA=re.compile(r"^[0-9a-f]{64}$");HEX32=re.compile(r"^[0-9a-f]{32}$")
def canonical(v):return json.dumps(v,sort_keys=True,separators=(",",":")).encode()
def digest(raw):return hashlib.sha256(raw).hexdigest()
def exact(v,fields,label):require(type(v) is dict and set(v)==set(fields),f"{label} fields invalid")
def posint(v):return type(v) is int and not isinstance(v,bool) and v>0

class Writer:
 def __init__(self,root,expected_uid=None):
  self.root=Path(root);self.expected_uid=os.geteuid() if expected_uid is None else expected_uid
 def validate(self,manifest,sidecar,authorization,facts,now):
  exact(facts,{"node","boot_id","cluster_name","corosync_conf_sha256","storage_cfg_sha256"},"local facts")
  exact(sidecar,{"schema","tx","generation","context_sha256","manifest_sha256","plan","actions","authorization","mutation_performed"},"adapter sidecar")
  require(sidecar["schema"]=="slt-v2-v1-package-adapter/v2" and sidecar["manifest_sha256"]==digest(canonical(manifest))
          and type(sidecar["tx"]) is str and HEX32.fullmatch(sidecar["tx"]) and sidecar["tx"]==manifest["tx"]
          and posint(sidecar["generation"]) and sidecar["generation"]==manifest["generation"]
          and type(sidecar["context_sha256"]) is str and SHA.fullmatch(sidecar["context_sha256"])
          and type(sidecar["plan"]) is dict and sidecar["plan"].get("tx")==sidecar["tx"]
          and sidecar["plan"].get("generation")==sidecar["generation"]
          and sidecar["plan"].get("context_sha256")==sidecar["context_sha256"]
          and sidecar["plan"].get("actions")==sidecar["actions"]
          and digest(canonical(sidecar["plan"]))==manifest["plan_sha256"]
          and sidecar["authorization"]=="PROJECTED_ONLY" and sidecar["mutation_performed"] is False,"sidecar binding invalid")
  actions=sidecar["actions"];manifest_names=[x["name"] for x in manifest["nodes"]]
  require(type(actions) is list and len(actions)==4,"sidecar action topology invalid")
  for action in actions:
   exact(action,{"node","role","action"},"sidecar action");require(type(action["node"]) is str,"sidecar action node invalid")
  require(sorted(x["node"] for x in actions)==manifest_names
          and len({x.get("node") for x in actions})==4
          and sum(x=={"node":x.get("node"),"role":"SAN_PARTICIPANT","action":"UNPACK_CONFIGURE"} for x in actions)==3
          and sum(x=={"node":x.get("node"),"role":"CONTROL_ONLY","action":"VERIFY_CURRENT_ONLY"} for x in actions)==1,
          "sidecar action topology invalid")
  local=[x for x in sidecar["actions"] if x.get("node")==facts["node"]]
  require(len(local)==1 and local[0]=={"node":facts["node"],"role":"SAN_PARTICIPANT","action":"UNPACK_CONFIGURE"},
          "local node is not an authorized SAN package participant")
  exact(authorization,{"schema","tx","generation","context_sha256","manifest_sha256","node","boot_id","action","operation_id","issued_at","expires_at"},"writer authorization")
  require(authorization["schema"]=="slt-live-hold-write-authorization/v1" and authorization["tx"]==sidecar["tx"]
          and posint(authorization["generation"]) and authorization["generation"]==sidecar["generation"]
          and authorization["context_sha256"]==sidecar["context_sha256"] and authorization["manifest_sha256"]==sidecar["manifest_sha256"]
          and authorization["node"]==facts["node"] and authorization["boot_id"]==facts["boot_id"]
          and authorization["action"]=="create-prepare-once" and type(authorization["operation_id"]) is str
          and HEX32.fullmatch(authorization["operation_id"]) and posint(authorization["issued_at"])
          and posint(authorization["expires_at"]) and authorization["issued_at"]<=now<=authorization["expires_at"]
          and 0<authorization["expires_at"]-authorization["issued_at"]<=1800,"writer authorization invalid")
  CHECK.validate_manifest(manifest,expected_phase="PREPARE_READY",package=manifest["candidate"]["package"],
                          version=manifest["candidate"]["version"],flavor=manifest["candidate"]["flavor"],
                          artifact_sha256=manifest["candidate"]["artifact_sha256"],storage_sha256=facts["storage_cfg_sha256"],
                          hostname=facts["node"],boot_id=facts["boot_id"],cluster_name=facts["cluster_name"],
                          corosync_sha256=facts["corosync_conf_sha256"],now=now)
  return local[0]
 def _ensure_root(self):
  parent=self.root.parent;name=self.root.name
  require(name not in ("",".",".."),"maintenance root name invalid")
  descriptors,identities=BASE.BASE._open_pinned_directory(parent,self.expected_uid)
  parent_fd=descriptors[-1]
  try:
   try:os.mkdir(name,0o700,dir_fd=parent_fd);os.fsync(parent_fd)
   except FileExistsError:pass
   info=os.stat(name,dir_fd=parent_fd,follow_symlinks=False)
   require(stat.S_ISDIR(info.st_mode) and info.st_uid==self.expected_uid and stat.S_IMODE(info.st_mode)==0o700,"maintenance root is unsafe")
  finally:
   for fd in reversed(descriptors):os.close(fd)
 def _open_locked(self,timeout):
  descriptors=[];lock_fd=None
  try:
   descriptors,identities=BASE.BASE._open_pinned_directory(self.root,self.expected_uid);root_fd=descriptors[-1]
   lock_fd=os.open(".maintenance.lock",os.O_RDWR|os.O_CREAT|getattr(os,"O_NOFOLLOW",0),0o600,dir_fd=root_fd)
   info=os.fstat(lock_fd);require(stat.S_ISREG(info.st_mode) and info.st_uid==self.expected_uid and stat.S_IMODE(info.st_mode)==0o600 and info.st_nlink==1,"maintenance lock unsafe")
   BASE._acquire_lock(lock_fd,timeout);named=os.stat(".maintenance.lock",dir_fd=root_fd,follow_symlinks=False)
   require((named.st_dev,named.st_ino)==(info.st_dev,info.st_ino),"maintenance lock namespace changed")
   return descriptors,identities,root_fd,lock_fd,info
  except Exception:
   if lock_fd is not None:os.close(lock_fd)
   for fd in reversed(descriptors):os.close(fd)
   raise
 def _subdir(self,root_fd,name):
  try:os.mkdir(name,0o700,dir_fd=root_fd);os.fsync(root_fd)
  except FileExistsError:pass
  flags=os.O_RDONLY|os.O_DIRECTORY|getattr(os,"O_NOFOLLOW",0);fd=os.open(name,flags,dir_fd=root_fd)
  try:
   info=os.fstat(fd);require(stat.S_ISDIR(info.st_mode) and info.st_uid==self.expected_uid and stat.S_IMODE(info.st_mode)==0o700,"hold evidence directory unsafe")
   return fd,info
  except Exception:os.close(fd);raise
 def _existing_subdir(self,root_fd,name):
  flags=os.O_RDONLY|os.O_DIRECTORY|getattr(os,"O_NOFOLLOW",0);fd=os.open(name,flags,dir_fd=root_fd)
  try:
   info=os.fstat(fd);require(stat.S_ISDIR(info.st_mode) and info.st_uid==self.expected_uid and stat.S_IMODE(info.st_mode)==0o700,"hold evidence directory unsafe")
   return fd,info
  except Exception:os.close(fd);raise
 def _revalidate(self,descriptors,identities,root_fd,lock_fd,lock_info,children):
  BASE.BASE._require_directory_chain(descriptors,identities,self.root,self.expected_uid)
  opened=os.fstat(lock_fd);named=os.stat(".maintenance.lock",dir_fd=root_fd,follow_symlinks=False)
  require((opened.st_dev,opened.st_ino)==(named.st_dev,named.st_ino)==(lock_info.st_dev,lock_info.st_ino)
          and stat.S_ISREG(named.st_mode) and named.st_uid==self.expected_uid and stat.S_IMODE(named.st_mode)==0o600 and named.st_nlink==1,"maintenance lock continuity failed")
  for name,(fd,identity) in children.items():
   current=os.fstat(fd);named=os.stat(name,dir_fd=root_fd,follow_symlinks=False)
   require((current.st_dev,current.st_ino)==(named.st_dev,named.st_ino)==(identity.st_dev,identity.st_ino)
           and stat.S_ISDIR(named.st_mode) and named.st_uid==self.expected_uid and stat.S_IMODE(named.st_mode)==0o700,"hold evidence namespace changed")
 def _deadline(self,manifest,authorization):
  current=int(time.time());require(current<=authorization["expires_at"] and current<=manifest["expires_at"],"hold authorization expired at publication boundary")
 def _record(self,fd,name,expected,identity,label):
  raw,info=BASE._read_regular(fd,name,self.expected_uid)
  require(raw==expected and (info.st_dev,info.st_ino,info.st_size,stat.S_IMODE(info.st_mode),info.st_nlink)
          ==(identity.st_dev,identity.st_ino,identity.st_size,0o600,1),f"{label} evidence changed")
 def create_prepare_once(self,manifest,sidecar,authorization,facts,now,timeout=5.0,fault=None):
  # This validation occurs before even creating the maintenance directory, so
  # VERIFY_CURRENT_ONLY nodes cannot receive a filesystem mutation.
  self.validate(manifest,sidecar,authorization,facts,now);manifest_raw=canonical(manifest);sidecar_raw=canonical(sidecar)
  self._ensure_root();ctx=self._open_locked(timeout);descriptors,identities,root_fd,lock_fd,lock_info=ctx
  attempts_fd=sidecars_fd=receipts_fd=None;children={}
  try:
   BASE.BASE._require_directory_chain(descriptors,identities,self.root,self.expected_uid)
   require(BASE._read_regular(root_fd,"active.json",self.expected_uid,missing=True)[0] is None,"active maintenance hold already exists")
   attempts_fd,attempts_info=self._subdir(root_fd,"attempts");sidecars_fd,sidecars_info=self._subdir(root_fd,"sidecars");receipts_fd,receipts_info=self._subdir(root_fd,"receipts");children={"attempts":(attempts_fd,attempts_info),"sidecars":(sidecars_fd,sidecars_info),"receipts":(receipts_fd,receipts_info)}
   self._revalidate(descriptors,identities,root_fd,lock_fd,lock_info,children)
   reservation=f"{sidecar['tx']}-{sidecar['generation']}-{facts['node']}-create-prepare"
   stem=reservation+"-"+authorization["operation_id"]
   intent={"schema":"slt-live-hold-attempt/v1","state":"INTENT_DURABLE","operation_id":authorization["operation_id"],
           "node":facts["node"],"boot_id":facts["boot_id"],"manifest_sha256":digest(manifest_raw),"sidecar_sha256":digest(sidecar_raw)}
   intent_raw=canonical(intent)
   require(os.listdir(attempts_fd)==[] and os.listdir(sidecars_fd)==[] and os.listdir(receipts_fd)==[],"existing maintenance evidence requires explicit recovery")
   BASE._write_once(sidecars_fd,stem+".json",sidecar_raw,self.expected_uid,fault,"sidecar")
   _,sidecar_info=BASE._read_regular(sidecars_fd,stem+".json",self.expected_uid)
   BASE._write_once(attempts_fd,stem+"-intent.json",intent_raw,self.expected_uid,fault,"intent")
   _,intent_info=BASE._read_regular(attempts_fd,stem+"-intent.json",self.expected_uid)
   if fault:fault("after_intent_durable")
   self._revalidate(descriptors,identities,root_fd,lock_fd,lock_info,children);self._deadline(manifest,authorization)
   self._record(sidecars_fd,stem+".json",sidecar_raw,sidecar_info,"sidecar");self._record(attempts_fd,stem+"-intent.json",intent_raw,intent_info,"intent")
   require(BASE._read_regular(root_fd,"active.json",self.expected_uid,missing=True)[0] is None,"active hold appeared before publication")
   def publication_fault(point):
    if point=="after_active_file_fsync":
     if fault:fault(point)
     self._revalidate(descriptors,identities,root_fd,lock_fd,lock_info,children);self._deadline(manifest,authorization)
     self._record(sidecars_fd,stem+".json",sidecar_raw,sidecar_info,"sidecar");self._record(attempts_fd,stem+"-intent.json",intent_raw,intent_info,"intent")
     require(BASE._read_regular(root_fd,"active.json",self.expected_uid,missing=True)[0] is None,"active hold appeared at publication boundary")
    elif fault:fault(point)
   publication=BASE._write_once(root_fd,"active.json",manifest_raw,self.expected_uid,publication_fault,"active")
   if fault:fault("after_active_durable")
   self._revalidate(descriptors,identities,root_fd,lock_fd,lock_info,children)
   self._record(sidecars_fd,stem+".json",sidecar_raw,sidecar_info,"sidecar");self._record(attempts_fd,stem+"-intent.json",intent_raw,intent_info,"intent")
   active_bytes,active_info=BASE._read_regular(root_fd,"active.json",self.expected_uid);require(active_bytes==manifest_raw,"active hold postcondition differs")
   receipt={"schema":"slt-live-hold-attempt/v1","state":"COMPLETE","operation_id":authorization["operation_id"],
            "node":facts["node"],"boot_id":facts["boot_id"],"manifest_sha256":digest(manifest_raw),
            "sidecar_sha256":digest(sidecar_raw),"intent_sha256":digest(intent_raw)}
   BASE._write_once(attempts_fd,stem+"-complete.json",canonical(receipt),self.expected_uid,fault,"complete")
   self._revalidate(descriptors,identities,root_fd,lock_fd,lock_info,children)
   self._record(sidecars_fd,stem+".json",sidecar_raw,sidecar_info,"sidecar");self._record(attempts_fd,stem+"-intent.json",intent_raw,intent_info,"intent")
   final_bytes,final_info=BASE._read_regular(root_fd,"active.json",self.expected_uid)
   require(final_bytes==manifest_raw and (final_info.st_dev,final_info.st_ino)==(active_info.st_dev,active_info.st_ino),"active hold changed before success")
   return {"classification":"PREPARE_HOLD_CREATED","publication":publication,"manifest_sha256":digest(manifest_raw),
           "rollout_authorized":False,"release_authorized":False}
  except Exception as error:
   raise Refusal("hold creation failed or is ambiguous; explicit inspection required") from error
  finally:
   for fd in (receipts_fd,sidecars_fd,attempts_fd,lock_fd):
    if fd is not None:os.close(fd)
   for fd in reversed(descriptors):os.close(fd)

 def transition_config_committed_once(self,manifest,sidecar,authorization,facts,
                                      preinst,proof,now,timeout=5.0,fault=None):
  """Replace one exact PREPARE hold with its CONFIG_COMMITTED successor.

  This is deliberately a file-only checkpoint.  ``proof`` is evidence from a
  separately qualified config/package executor; this method never performs
  that executor's effects and never authorizes release or rollout.
  """
  exact(authorization,{"schema","tx","generation","context_sha256","old_manifest_sha256","new_manifest_sha256",
        "node","boot_id","action","operation_id","prepare_operation_id","proof_sha256","preinst_sha256","issued_at","expires_at"},"transition authorization")
  old_facts=dict(facts);old_facts["storage_cfg_sha256"]=manifest["baseline_storage_cfg_sha256"]
  prepare_authorization={"schema":"slt-live-hold-write-authorization/v1","tx":authorization["tx"],
      "generation":authorization["generation"],"context_sha256":authorization["context_sha256"],
      "manifest_sha256":authorization["old_manifest_sha256"],"node":authorization["node"],
      "boot_id":authorization["boot_id"],"action":"create-prepare-once",
      "operation_id":authorization["prepare_operation_id"],"issued_at":authorization["issued_at"],
      "expires_at":authorization["expires_at"]}
  self.validate(manifest,sidecar,prepare_authorization,old_facts,now)
  old_raw=canonical(manifest);old_sha=digest(old_raw)
  successor=copy.deepcopy(manifest);successor["phase"]="CONFIG_COMMITTED";new_raw=canonical(successor);new_sha=digest(new_raw)
  require(authorization["schema"]=="slt-live-hold-transition-authorization/v1"
          and authorization["tx"]==manifest["tx"] and authorization["generation"]==manifest["generation"]
          and authorization["context_sha256"]==sidecar["context_sha256"]
          and authorization["old_manifest_sha256"]==old_sha and authorization["new_manifest_sha256"]==new_sha
          and authorization["node"]==facts["node"] and authorization["boot_id"]==facts["boot_id"]
          and authorization["action"]=="transition-config-committed-once"
          and all(type(authorization[k]) is str and HEX32.fullmatch(authorization[k]) for k in ("operation_id","prepare_operation_id"))
          and posint(authorization["issued_at"]) and posint(authorization["expires_at"])
          and authorization["issued_at"]<=now<=authorization["expires_at"]
          and 0<authorization["expires_at"]-authorization["issued_at"]<=1800,"transition authorization invalid")
  CHECK.validate_manifest(successor,expected_phase="CONFIG_COMMITTED",package=manifest["candidate"]["package"],
                          version=manifest["candidate"]["version"],flavor=manifest["candidate"]["flavor"],
                          artifact_sha256=manifest["candidate"]["artifact_sha256"],storage_sha256=facts["storage_cfg_sha256"],
                          hostname=facts["node"],boot_id=facts["boot_id"],cluster_name=facts["cluster_name"],
                          corosync_sha256=facts["corosync_conf_sha256"],now=now)
  exact(preinst,{"schema","tx","generation","phase","node","boot_id","package","version","flavor","artifact_sha256","manifest_sha256","storage_cfg_sha256","recorded_at"},"PREINST receipt")
  require(preinst["schema"]=="slt-package-maintenance-receipt/v1" and preinst["phase"]=="PREINST_ACCEPTED"
          and preinst["tx"]==manifest["tx"] and posint(preinst["generation"]) and preinst["generation"]==manifest["generation"]
          and preinst["node"]==facts["node"] and preinst["boot_id"]==facts["boot_id"]
          and preinst["package"]==manifest["candidate"]["package"] and preinst["version"]==manifest["candidate"]["version"]
          and preinst["flavor"]==manifest["candidate"]["flavor"] and preinst["artifact_sha256"]==manifest["candidate"]["artifact_sha256"]
          and preinst["manifest_sha256"]==old_sha and preinst["storage_cfg_sha256"]==manifest["baseline_storage_cfg_sha256"]
          and posint(preinst["recorded_at"]) and preinst["recorded_at"]<=now,"PREINST predecessor invalid")
  CHECK.receipt_transition(None,preinst,"PREINST_ACCEPTED")
  exact(proof,{"schema","tx","generation","context_sha256","old_manifest_sha256","new_manifest_sha256",
        "target_storage_cfg_sha256","config_cas_sha256","all_target_payload_present","nodes"},"config commit proof")
  require(proof["schema"]=="slt-config-commit-proof/v1" and proof["tx"]==manifest["tx"]
          and posint(proof["generation"]) and proof["generation"]==manifest["generation"] and proof["context_sha256"]==sidecar["context_sha256"]
          and proof["old_manifest_sha256"]==old_sha and proof["new_manifest_sha256"]==new_sha
          and proof["target_storage_cfg_sha256"]==manifest["target_storage_cfg_sha256"]
          and type(proof["config_cas_sha256"]) is str and SHA.fullmatch(proof["config_cas_sha256"])
          and proof["all_target_payload_present"] is True and type(proof["nodes"]) is list and len(proof["nodes"])==4,
          "config commit proof invalid")
  actions={x["node"]:(x["role"],x["action"]) for x in sidecar["actions"]};seen=set()
  for row in proof["nodes"]:
   exact(row,{"node","boot_id","role","action","payload_evidence_sha256"},"config proof node")
   require(row["node"] in actions and row["node"] not in seen and (row["role"],row["action"])==actions[row["node"]]
           and row["boot_id"]==next(x["boot_id"] for x in manifest["nodes"] if x["name"]==row["node"])
           and type(row["payload_evidence_sha256"]) is str and SHA.fullmatch(row["payload_evidence_sha256"]),"config proof node invalid")
   seen.add(row["node"])
  require(seen==set(actions),"config proof node set incomplete")
  proof_raw=canonical(proof);preinst_raw=canonical(preinst)+b"\n"
  require(authorization["proof_sha256"]==digest(proof_raw) and authorization["preinst_sha256"]==digest(preinst_raw),"transition evidence binding invalid")
  ctx=self._open_locked(timeout);descriptors,identities,root_fd,lock_fd,lock_info=ctx
  attempts_fd=sidecars_fd=receipts_fd=None;children={}
  try:
   attempts_fd,attempts_info=self._subdir(root_fd,"attempts");sidecars_fd,sidecars_info=self._subdir(root_fd,"sidecars");receipts_fd,receipts_info=self._existing_subdir(root_fd,"receipts");children={"attempts":(attempts_fd,attempts_info),"sidecars":(sidecars_fd,sidecars_info),"receipts":(receipts_fd,receipts_info)}
   self._revalidate(descriptors,identities,root_fd,lock_fd,lock_info,children)
   active,active_info=BASE._read_regular(root_fd,"active.json",self.expected_uid);require(active==old_raw,"PREPARE predecessor differs")
   prepare_stem=f"{manifest['tx']}-{manifest['generation']}-{facts['node']}-create-prepare-{authorization['prepare_operation_id']}"
   sidecar_name=prepare_stem+".json";intent_name=prepare_stem+"-intent.json";complete_name=prepare_stem+"-complete.json"
   prepare_sidecar,prepare_sidecar_info=BASE._read_regular(sidecars_fd,sidecar_name,self.expected_uid)
   prepare_intent,prepare_intent_info=BASE._read_regular(attempts_fd,intent_name,self.expected_uid)
   prepare_complete,prepare_complete_info=BASE._read_regular(attempts_fd,complete_name,self.expected_uid)
   require(prepare_sidecar==canonical(sidecar),"prepare sidecar differs")
   expected_prepare_intent={"schema":"slt-live-hold-attempt/v1","state":"INTENT_DURABLE","operation_id":authorization["prepare_operation_id"],
       "node":facts["node"],"boot_id":facts["boot_id"],"manifest_sha256":old_sha,"sidecar_sha256":digest(prepare_sidecar)}
   expected_prepare_intent_raw=canonical(expected_prepare_intent)
   expected_prepare_complete={"schema":"slt-live-hold-attempt/v1","state":"COMPLETE","operation_id":authorization["prepare_operation_id"],
       "node":facts["node"],"boot_id":facts["boot_id"],"manifest_sha256":old_sha,"sidecar_sha256":digest(prepare_sidecar),
       "intent_sha256":digest(expected_prepare_intent_raw)}
   require(prepare_intent==expected_prepare_intent_raw and prepare_complete==canonical(expected_prepare_complete),
           "prepare predecessor evidence differs")
   local_preinst,local_preinst_info=BASE._read_regular(receipts_fd,manifest["tx"]+".json",self.expected_uid)
   require(local_preinst==preinst_raw,"local PREINST receipt differs")
   reservation=f"{manifest['tx']}-{manifest['generation']}-{facts['node']}-config-committed"
   require(not any(name.startswith(reservation) or name.startswith("."+reservation)
                   for name in os.listdir(attempts_fd)+os.listdir(sidecars_fd)),
           "existing transition evidence requires explicit recovery")
   stem=reservation+"-"+authorization["operation_id"]
   evidence={"schema":"slt-live-hold-transition-evidence/v1","preinst":preinst,"proof":proof};evidence_raw=canonical(evidence)
   intent={"schema":"slt-live-hold-transition-attempt/v1","state":"INTENT_DURABLE","operation_id":authorization["operation_id"],
           "node":facts["node"],"boot_id":facts["boot_id"],"old_manifest_sha256":old_sha,"new_manifest_sha256":new_sha,
           "old_active_dev":active_info.st_dev,"old_active_ino":active_info.st_ino,"evidence_sha256":digest(evidence_raw)};intent_raw=canonical(intent)
   BASE._write_once(sidecars_fd,stem+".json",evidence_raw,self.expected_uid,fault,"transition_evidence");_,evidence_info=BASE._read_regular(sidecars_fd,stem+".json",self.expected_uid)
   BASE._write_once(attempts_fd,stem+"-intent.json",intent_raw,self.expected_uid,fault,"transition_intent");_,transition_intent_info=BASE._read_regular(attempts_fd,stem+"-intent.json",self.expected_uid)
   if fault:fault("after_transition_intent_durable")
   self._revalidate(descriptors,identities,root_fd,lock_fd,lock_info,children)
   self._record(sidecars_fd,sidecar_name,prepare_sidecar,prepare_sidecar_info,"prepare sidecar");self._record(attempts_fd,intent_name,prepare_intent,prepare_intent_info,"prepare intent");self._record(attempts_fd,complete_name,prepare_complete,prepare_complete_info,"prepare complete");self._record(receipts_fd,manifest["tx"]+".json",local_preinst,local_preinst_info,"local PREINST")
   current,current_info=BASE._read_regular(root_fd,"active.json",self.expected_uid);require(current==old_raw and (current_info.st_dev,current_info.st_ino)==(active_info.st_dev,active_info.st_ino),"PREPARE active changed")
   temporary="."+stem+"-active.tmp"
   BASE._write_once(root_fd,temporary,new_raw,self.expected_uid,fault,"transition_active");_,temporary_info=BASE._read_regular(root_fd,temporary,self.expected_uid)
   if fault:fault("before_transition_replace")
   self._revalidate(descriptors,identities,root_fd,lock_fd,lock_info,children)
   self._record(sidecars_fd,stem+".json",evidence_raw,evidence_info,"transition evidence");self._record(attempts_fd,stem+"-intent.json",intent_raw,transition_intent_info,"transition intent");self._record(sidecars_fd,sidecar_name,prepare_sidecar,prepare_sidecar_info,"prepare sidecar");self._record(attempts_fd,intent_name,prepare_intent,prepare_intent_info,"prepare intent");self._record(attempts_fd,complete_name,prepare_complete,prepare_complete_info,"prepare complete");self._record(receipts_fd,manifest["tx"]+".json",local_preinst,local_preinst_info,"local PREINST");self._record(root_fd,temporary,new_raw,temporary_info,"transition active source")
   current,current_info=BASE._read_regular(root_fd,"active.json",self.expected_uid);require(current==old_raw and (current_info.st_dev,current_info.st_ino)==(active_info.st_dev,active_info.st_ino),"PREPARE active changed before transition")
   self._deadline(successor,authorization)
   os.replace(temporary,"active.json",src_dir_fd=root_fd,dst_dir_fd=root_fd);os.fsync(root_fd)
   if fault:fault("after_transition_directory_fsync")
   self._revalidate(descriptors,identities,root_fd,lock_fd,lock_info,children)
   final,final_info=BASE._read_regular(root_fd,"active.json",self.expected_uid);require(final==new_raw and (final_info.st_dev,final_info.st_ino)==(temporary_info.st_dev,temporary_info.st_ino) and (final_info.st_dev,final_info.st_ino)!=(active_info.st_dev,active_info.st_ino),"CONFIG_COMMITTED postcondition differs")
   receipt={"schema":"slt-live-hold-transition-attempt/v1","state":"COMPLETE","operation_id":authorization["operation_id"],"node":facts["node"],"boot_id":facts["boot_id"],"old_manifest_sha256":old_sha,"new_manifest_sha256":new_sha,"intent_sha256":digest(intent_raw)}
   BASE._write_once(attempts_fd,stem+"-complete.json",canonical(receipt),self.expected_uid,fault,"transition_complete")
   self._revalidate(descriptors,identities,root_fd,lock_fd,lock_info,children);self._record(sidecars_fd,sidecar_name,prepare_sidecar,prepare_sidecar_info,"prepare sidecar");self._record(attempts_fd,intent_name,prepare_intent,prepare_intent_info,"prepare intent");self._record(attempts_fd,complete_name,prepare_complete,prepare_complete_info,"prepare complete");self._record(receipts_fd,manifest["tx"]+".json",local_preinst,local_preinst_info,"local PREINST");self._record(sidecars_fd,stem+".json",evidence_raw,evidence_info,"transition evidence");self._record(attempts_fd,stem+"-intent.json",intent_raw,transition_intent_info,"transition intent");final_again,final_again_info=BASE._read_regular(root_fd,"active.json",self.expected_uid);require(final_again==new_raw and (final_again_info.st_dev,final_again_info.st_ino)==(temporary_info.st_dev,temporary_info.st_ino),"CONFIG_COMMITTED changed before success")
   return {"classification":"LOCAL_CONFIG_COMMITTED_HOLD","manifest_sha256":new_sha,"rollout_authorized":False,"release_authorized":False}
  except Exception as error:
   raise Refusal("CONFIG_COMMITTED transition failed or is ambiguous; explicit inspection required") from error
  finally:
   for fd in (receipts_fd,sidecars_fd,attempts_fd,lock_fd):
    if fd is not None:os.close(fd)
   for fd in reversed(descriptors):os.close(fd)
