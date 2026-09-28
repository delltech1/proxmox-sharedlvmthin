#!/usr/bin/env python3
"""Execute one authorized target-service phase once on the local node."""
import argparse,hashlib,json,os,re,socket,stat,subprocess,sys,time
from pathlib import Path
ROOT=Path("/var/lib/pve-sharedlvmthin/service-restore");SHA=re.compile(r"^[0-9a-f]{64}$");HEX=re.compile(r"^[0-9a-f]{32}$")
PHASES={"BACKEND":["qmeventd.service","pvedaemon.service","pvestatd.service"],"API":["pveproxy.service","spiceproxy.service"],"HA":["pve-ha-crm.service","pve-ha-lrm.service"],"SCHEDULER":["pvescheduler.service"]}
PRESERVE=["corosync.service","multipathd.service","pve-cluster.service","pve-guests.service","pve-sharedlvmthin-thin-guard.service"]
class Refusal(ValueError):pass
def require(v,m):
 if not v:raise Refusal(m)
def canonical(v):return json.dumps(v,sort_keys=True,separators=(",",":")).encode()
def digest(v):return hashlib.sha256(canonical(v)).hexdigest()
def read(path,label):
 raw=Path(path).read_bytes();require(0<len(raw)<=4*1024*1024,label+" bytes invalid")
 try:v=json.loads(raw)
 except (UnicodeDecodeError,json.JSONDecodeError) as e:raise Refusal(label+" invalid") from e
 require(type(v)is dict,label+" root invalid");return v
def cmd(argv):
 r=subprocess.run(argv,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=60,check=False,env={"PATH":"/usr/sbin:/usr/bin:/sbin:/bin","LC_ALL":"C"})
 require(r.returncode==0,f"command failed rc={r.returncode}")
 return r.stdout
def prop(unit,name):return cmd(["/usr/bin/systemctl","show","--property="+name,"--value",unit]).decode().strip()
def state(unit):
 active,sub,pid,control=prop(unit,"ActiveState"),prop(unit,"SubState"),int(prop(unit,"MainPID")),int(prop(unit,"ControlPID"));mask=Path("/etc/systemd/system")/unit
 return {"active_state":active,"sub_state":sub,"main_pid":pid,"control_pid":control,"persistent_mask":os.path.islink(mask) and os.readlink(mask)=="/dev/null"}
def persist(path,value):
 raw=canonical(value)+b"\n";parent_fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
 try:fd=os.open(path.name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=parent_fd)
 finally:os.close(parent_fd)
 try:
  view=memoryview(raw)
  while view:n=os.write(fd,view);require(n>0,"short write");view=view[n:]
  os.fsync(fd)
 finally:os.close(fd)
 dfd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW);os.fsync(dfd);os.close(dfd);return hashlib.sha256(raw).hexdigest()
def safe_root():
 parent=ROOT.parent;st=os.lstat(parent);require(stat.S_ISDIR(st.st_mode) and st.st_uid==0 and not(st.st_mode&0o022),"state parent unsafe")
 try:os.mkdir(ROOT,0o700)
 except FileExistsError:pass
 st=os.lstat(ROOT);require(stat.S_ISDIR(st.st_mode) and st.st_uid==0 and stat.S_IMODE(st.st_mode)==0o700,"state root unsafe")
def execute(a):
 plan=read(a.plan,"target plan");auth=read(a.authorization,"phase authorization");before=read(a.before,"before record");phase=a.phase;require(phase in PHASES,"phase invalid")
 body=dict(plan);claimed=body.pop("plan_sha256");require(claimed==digest(body) and plan["authorization"]=="NONE","target plan invalid")
 node=socket.gethostname().split(".",1)[0];boot=Path("/proc/sys/kernel/random/boot_id").read_text().strip();now=int(time.time())
 require(auth=={**auth} and auth.get("schema")=="slt-target-service-phase-authorization/v1" and auth.get("tx")==plan["tx"] and auth.get("generation")==plan["generation"] and auth.get("context_sha256")==plan["context_sha256"] and auth.get("plan_sha256")==claimed and auth.get("phase")==phase and auth.get("node_order")==list(plan["participant_boots"]) and auth.get("participant_boots")==plan["participant_boots"] and auth.get("challenges",{}).get(node) and auth.get("allowed_effects")==["unmask-and-start-exact-phase-once"] and type(auth.get("issued_at"))is int and type(auth.get("expires_at"))is int and auth["issued_at"]<=now<=auth["expires_at"] and auth["expires_at"]-auth["issued_at"]<=900,"authorization invalid")
 require(plan["participant_boots"].get(node)==boot and before["node"]==node and plan["observed_before_sha256"].get(node)==digest(before),"node/before identity differs")
 units=next(x["units"] for x in plan["phases"] if x["phase"]==phase);require(units==PHASES[phase],"phase units differ")
 before_map={x["unit"]:x for x in before["services"]}
 preserve_before={u:state(u) for u in PRESERVE}
 safe_root();intent_path=ROOT/f"{plan['tx']}-{node}-{phase}.intent.json";outcome_path=ROOT/f"{plan['tx']}-{node}-{phase}.outcome.json"
 require(not outcome_path.exists(),"phase already completed; inspect durable state")
 intent={"schema":"slt-target-service-phase-intent/v1","tx":plan["tx"],"generation":plan["generation"],"node":node,"boot_id":boot,"phase":phase,"plan_sha256":claimed,"authorization_sha256":digest(auth),"before_sha256":digest(before),"units":units,"prior_cohort_sha256":auth["prior_cohort_sha256"],"reserved_at":now}
 resuming=intent_path.exists()
 if resuming:
  stored=read(intent_path,"phase intent");intent["reserved_at"]=stored.get("reserved_at");require(stored==intent,"existing intent differs; recovery required")
  intent_sha=hashlib.sha256(intent_path.read_bytes()).hexdigest()
 else:intent_sha=persist(intent_path,intent)
 results=[]
 for unit in units:
  expected=before_map[unit];current=state(unit);require(expected["active_state"]=="inactive","unit predecessor differs")
  if resuming and current["active_state"]=="active" and current["persistent_mask"] is False:
   after=current
  else:
   require(current["active_state"]=="inactive" and current["sub_state"]=="dead" and current["main_pid"]==0 and current["control_pid"]==0 and (current["persistent_mask"] is True or resuming),"unit predecessor differs")
   if current["persistent_mask"] is True:cmd(["/usr/bin/systemctl","unmask","--",unit])
   cmd(["/usr/bin/systemctl","--job-mode=fail","start","--",unit]);after=state(unit)
  require(after["active_state"]=="active" and after["sub_state"] in ("running","exited") and after["persistent_mask"] is False,"unit target not reached");results.append({"unit":unit,"result":"ACTIVE_UNMASKED","main_pid":after["main_pid"]})
 require({u:state(u) for u in PRESERVE}==preserve_before,"preserved service changed")
 outcome={"schema":"slt-target-service-phase-outcome/v1","tx":plan["tx"],"generation":plan["generation"],"node":node,"boot_id":boot,"phase":phase,"plan_sha256":claimed,"authorization_sha256":digest(auth),"intent_sha256":intent_sha,"prior_cohort_sha256":auth["prior_cohort_sha256"],"challenge":auth["challenges"][node],"results":results,"preserved_states_sha256":digest(preserve_before),"finished_at":int(time.time()),"classification":"PHASE_TARGET_REACHED"}
 outcome_sha=persist(outcome_path,outcome);return {"schema":"slt-target-service-phase-receipt/v1","node":node,"boot_id":boot,"phase":phase,"plan_sha256":claimed,"authorization_sha256":digest(auth),"prior_cohort_sha256":auth["prior_cohort_sha256"],"outcome_sha256":outcome_sha,"outcome":outcome}
def main():
 p=argparse.ArgumentParser();p.add_argument("--plan",required=True);p.add_argument("--authorization",required=True);p.add_argument("--before",required=True);p.add_argument("--phase",required=True);a=p.parse_args()
 try:r=execute(a)
 except (Refusal,OSError,ValueError,KeyError,TypeError,subprocess.TimeoutExpired) as e:print(json.dumps({"schema":"slt-target-service-phase-receipt/v1","classification":"UNKNOWN_RETAIN","reason":str(e)},sort_keys=True));return 2
 print(json.dumps(r,sort_keys=True,indent=2));return 0
if __name__=="__main__":sys.exit(main())
