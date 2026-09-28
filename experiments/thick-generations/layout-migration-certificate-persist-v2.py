#!/usr/bin/env python3
"""Persist one exact v2 release certificate on a SAN participant."""
import argparse, hashlib, importlib.util, json, os, socket, stat, sys, time, uuid
from pathlib import Path
HERE=Path(__file__).resolve().parent
def load(name,file):
 s=importlib.util.spec_from_file_location(name,HERE/file);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
STORE=load("slt_cert_store","layout-migration-release-certificate.py")
Refusal=STORE.Refusal;require=STORE.require
ROOT=Path("/var/lib/pve-sharedlvmthin/maintenance");DEST=ROOT/"release-certificates"
def canonical(v):return json.dumps(v,sort_keys=True,separators=(",",":")).encode()
def read(path,label):
 raw=Path(path).read_bytes();require(0<len(raw)<=2*1024*1024,label+" bytes invalid")
 try:v=json.loads(raw)
 except (UnicodeDecodeError,json.JSONDecodeError) as e:raise Refusal(label+" invalid") from e
 require(type(v) is dict,label+" root invalid");return v,raw
def safe_dir(path,mode=None):
 st=os.lstat(path);require(stat.S_ISDIR(st.st_mode) and st.st_uid==0 and not(st.st_mode&0o022),"unsafe directory "+str(path))
 if mode is not None:require(stat.S_IMODE(st.st_mode)==mode,"directory mode differs")
 return st
def execute(args):
 context_doc,_=read(args.context,"context");context=context_doc.get("context",context_doc)
 auth,_=read(args.authorization,"release authorization");cert,raw=read(args.certificate,"release certificate")
 require(raw==canonical(cert)+b"\n","certificate bytes are not canonical")
 body=dict(cert);claimed=body.pop("certificate_sha256",None)
 require(claimed==hashlib.sha256(canonical(body)).hexdigest(),"embedded certificate digest differs")
 now=int(time.time());node=socket.gethostname().split(".",1)[0];boot=Path("/proc/sys/kernel/random/boot_id").read_text().strip()
 row=next((x for x in context["nodes"] if x["name"]==node),None)
 require(row is not None and row["boot_id"]==boot and context["node_roles"][node]=="SAN_PARTICIPANT","local SAN identity differs")
 require(auth["schema"]=="slt-layout-release-authorization/v2" and auth["tx"]==context["tx"]
         and auth["generation"]==context["generation"] and auth["context_sha256"]==context["context_sha256"]
         and auth["participant_boots"]=={x["name"]:x["boot_id"] for x in context["nodes"]}
         and auth["allowed_effects"]==["persist-release-certificate-v2"]
         and auth["issued_at"]<=now<=auth["release_not_after"]<=auth["expires_at"],"release authorization invalid/expired")
 require(cert["schema"]=="slt-layout-release-commit/v2" and cert["phase"]=="RELEASE_COMMITTED"
         and cert["tx"]==auth["tx"] and cert["generation"]==auth["generation"]
         and cert["context_sha256"]==auth["context_sha256"] and cert["commit_id"]==auth["commit_id"]
         and cert["authorization_id"]==auth["authorization_id"]
         and cert["release_not_after"]==auth["release_not_after"]
         and node in cert["release_nodes"] and node not in cert["verify_only_nodes"],"certificate identity/role differs")
 safe_dir(Path("/"));safe_dir(Path("/var"));safe_dir(Path("/var/lib"));safe_dir(Path("/var/lib/pve-sharedlvmthin"));safe_dir(ROOT)
 try:os.mkdir(DEST,0o700);pfd=os.open(ROOT,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW);os.fsync(pfd);os.close(pfd)
 except FileExistsError:pass
 safe_dir(DEST,0o700)
 name=f"{context['tx']}-{context['generation']}.json"
 result=STORE.persist_create_only(DEST,name,raw,expected_uid=0,valid_until=auth["release_not_after"])
 return {"schema":"slt-certificate-persist-result/v2","node":node,"boot_id":boot,"tx":context["tx"],"generation":context["generation"],"commit_id":cert["commit_id"],"certificate_bytes_sha256":hashlib.sha256(raw).hexdigest(),"result":result,"authorization":"NONE","hold_released":False}
def main():
 p=argparse.ArgumentParser();p.add_argument("--context",required=True);p.add_argument("--authorization",required=True);p.add_argument("--certificate",required=True);a=p.parse_args()
 try:r=execute(a)
 except (Refusal,OSError,ValueError,KeyError,TypeError) as e:print(json.dumps({"schema":"slt-certificate-persist-result/v2","verdict":"BLOCKED","reason":str(e)},sort_keys=True));return 2
 print(json.dumps(r,sort_keys=True,indent=2));return 0
if __name__=="__main__":sys.exit(main())
