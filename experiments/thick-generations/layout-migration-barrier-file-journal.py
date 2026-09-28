#!/usr/bin/env python3
"""Create-only durable event journal and inspection-only replay for barrier model."""

import copy, hashlib, importlib.util, json, os, re, stat
from pathlib import Path

HERE=Path(__file__).resolve().parent
SPEC=importlib.util.spec_from_file_location("exact_backend",HERE/"prelive_exact_journal_file_backend.py")
BASE=importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(BASE)
MSPEC=importlib.util.spec_from_file_location("barrier_model",HERE/"layout-migration-barrier-model.py")
MODEL=importlib.util.module_from_spec(MSPEC); MSPEC.loader.exec_module(MODEL)
Refusal=BASE.Refusal
HEX32=re.compile(r"^[0-9a-f]{32}$"); SHA=re.compile(r"^[0-9a-f]{64}$")
UUID=re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
SAFE=re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

def require(v,m):
 if not v: raise Refusal(m)

def canonical(v): return json.dumps(v,sort_keys=True,separators=(",",":")).encode()
def stored(v): return canonical(v)+b"\n"
def digest(v): return hashlib.sha256(v).hexdigest()
def strict(raw):
 def pairs(rows):
  out={}
  for k,v in rows:
   require(k not in out,"duplicate JSON key"); out[k]=v
  return out
 try: return json.loads(raw,object_pairs_hook=pairs)
 except (UnicodeDecodeError,json.JSONDecodeError) as e: raise Refusal("invalid JSON") from e

class BarrierFileJournal:
 def __init__(self,nonce,plan,coordinator):
  require(type(plan) is dict and type(coordinator) is dict
          and set(coordinator)=={"node","boot_id"}
          and SAFE.fullmatch(coordinator["node"] or "")
          and UUID.fullmatch(coordinator["boot_id"] or ""),"journal identity invalid")
  MODEL.validate_plan(plan,plan["issued_at"])
  self.plan=copy.deepcopy(plan); self.plan_sha=digest(canonical(plan))
  self.tx=plan.get("tx"); require(HEX32.fullmatch(self.tx or ""),"transaction invalid")
  self.backend=BASE.create_fresh_file_backend(nonce); self.events=[]; self.previous="0"*64
  identity={"schema":"slt-barrier-action-journal/v1","tx":self.tx,"run_id":nonce,
            "plan":self.plan,"plan_sha256":self.plan_sha,"coordinator":copy.deepcopy(coordinator)}
  raw=stored(identity); self.backend.persist_exact_file("exact-event-000000.json",raw)
 def now(self): raise Refusal("journal is not a clock")
 def load_events(self,tx):
  require(tx==self.tx,"transaction differs from journal"); return copy.deepcopy(self.events)
 def persist_event(self,event):
  require(type(event) is dict,"event invalid")
  seq=len(self.events)+1
  expected={"tx","plan_sha256","kind","index","step","node"}
  if event.get("kind")=="OBSERVED_COMPLETE": expected.add("receipt")
  require(set(event)==expected and event["tx"]==self.tx
          and event["plan_sha256"]==self.plan_sha,"event identity invalid")
  record={"schema":"slt-barrier-action-event/v1","sequence":seq,
          "previous_sha256":self.previous,**copy.deepcopy(event)}
  record["record_sha256"]=digest(canonical(record))
  raw=stored(record)
  ack=self.backend.persist_exact_file(f"exact-event-{seq:06d}.json",raw)
  require(ack["sha256"]==digest(raw) and ack["file_synced"] is True
          and ack["dir_synced"] is True and ack["closed"] is True,
          "durable event acknowledgement invalid")
  self.previous=record["record_sha256"]; self.events.append(copy.deepcopy(event))
 def close(self): self.backend.close_preserving_artifacts()
 def artifact_path(self): return self.backend.artifact_path()

def inspect(root,expected_plan):
    MODEL.validate_plan(expected_plan,expected_plan["issued_at"])
    root=Path(root); require(root.parent==Path(BASE.PARENT),"journal parent is not pinned")
    flags=os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC
    parent_fd=os.open(BASE.PARENT,flags); root_fd=None; fds=[]; record_identities=[]
    try:
        parent_info=os.fstat(parent_fd); parent_named=os.stat(BASE.PARENT,follow_symlinks=False)
        require((parent_info.st_dev,parent_info.st_ino)==(parent_named.st_dev,parent_named.st_ino),"journal parent changed")
        root_fd=os.open(root.name,flags,dir_fd=parent_fd); info=os.fstat(root_fd)
        named=os.stat(root.name,dir_fd=parent_fd,follow_symlinks=False)
        require(stat.S_ISDIR(info.st_mode) and info.st_uid==os.geteuid()
                and stat.S_IMODE(info.st_mode)==0o700
                and (info.st_dev,info.st_ino)==(named.st_dev,named.st_ino),"journal root identity invalid")
        names=sorted(os.listdir(root_fd))
        require(names and names[0]=="exact-event-000000.json"
                and all(re.fullmatch(r"exact-event-[0-9]{6}\.json",n) for n in names),"journal file set invalid")
        require(names==[f"exact-event-{i:06d}.json" for i in range(len(names))],"journal sequence has a gap or duplicate")
        values=[]
        for name in names:
            fd=os.open(name,os.O_RDONLY|os.O_NONBLOCK|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=root_fd); fds.append(fd)
            before=os.fstat(fd); named_file=os.stat(name,dir_fd=root_fd,follow_symlinks=False)
            require(stat.S_ISREG(before.st_mode) and before.st_uid==os.geteuid()
                    and stat.S_IMODE(before.st_mode)==0o600 and before.st_nlink==1
                    and 0<before.st_size<=1024*1024
                    and (before.st_dev,before.st_ino)==(named_file.st_dev,named_file.st_ino),"journal record identity invalid")
            chunks=[]; total=0
            while True:
                chunk=os.read(fd,min(65536,1024*1024+1-total))
                if not chunk: break
                chunks.append(chunk); total+=len(chunk); require(total<=1024*1024,"journal record too large")
            raw=b"".join(chunks); after=os.fstat(fd); value=strict(raw)
            require((before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns)==
                    (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns)
                    and raw==stored(value),"journal record changed or is noncanonical")
            os.fsync(fd); values.append(value); record_identities.append((name,before))
        identity=values[0]; plan_sha=digest(canonical(expected_plan))
        require(type(identity) is dict and set(identity)=={"schema","tx","run_id","plan","plan_sha256","coordinator"}
                and identity["schema"]=="slt-barrier-action-journal/v1"
                and identity["tx"]==expected_plan["tx"] and HEX32.fullmatch(identity["run_id"] or "")
                and type(identity["coordinator"]) is dict and set(identity["coordinator"])=={"node","boot_id"}
                and SAFE.fullmatch(identity["coordinator"]["node"] or "")
                and UUID.fullmatch(identity["coordinator"]["boot_id"] or "")
                and identity["plan"]==expected_plan and identity["plan_sha256"]==plan_sha,"journal identity differs from expected plan")
        previous="0"*64; events=[]
        for seq,record in enumerate(values[1:],1):
            fields={"schema","sequence","previous_sha256","tx","plan_sha256","kind","index","step","node","record_sha256"}
            if record.get("kind")=="OBSERVED_COMPLETE": fields.add("receipt")
            require(type(record) is dict and set(record)==fields
                    and record["schema"]=="slt-barrier-action-event/v1"
                    and type(record["sequence"]) is int and not isinstance(record["sequence"],bool)
                    and record["sequence"]==seq and record["tx"]==identity["tx"]
                    and record["plan_sha256"]==plan_sha and record["previous_sha256"]==previous,"journal event identity or chain invalid")
            claimed=record["record_sha256"]; body=dict(record); body.pop("record_sha256")
            require(SHA.fullmatch(claimed or "") and claimed==digest(canonical(body)),"journal record hash invalid")
            previous=claimed; events.append({k:copy.deepcopy(v) for k,v in record.items() if k not in {"schema","sequence","previous_sha256","record_sha256"}})
        actions=expected_plan["actions"]; kinds=("INTENT_DURABLE","ATTEMPTED","OBSERVED_COMPLETE")
        require(len(events)<=len(actions)*3,"journal contains too many events")
        for position,event in enumerate(events):
            action=actions[position//3]
            require(event["kind"]==kinds[position%3]
                    and type(event["index"]) is int and not isinstance(event["index"],bool)
                    and event["index"]==position//3 and event["step"]==action["step"]
                    and event["node"]==action["node"],"journal action prefix is invalid")
            if event["kind"]=="OBSERVED_COMPLETE":
                if action["step"]=="ENTRY_BLOCK":
                    require(event["receipt"]=={"persistent_masks":sorted(MODEL.ENTRY_UNITS),"terminal_ingress":MODEL.TERMINAL_INGRESS},"entry receipt invalid")
                else: MODEL.validate_observation(event["receipt"],expected_plan,action["node"],final=action["step"]=="FINAL_AUDIT")
        os.fsync(root_fd); require(sorted(os.listdir(root_fd))==names,"journal file set changed")
        for fd,(name,before) in zip(fds,record_identities):
            opened=os.fstat(fd); current=os.stat(name,dir_fd=root_fd,follow_symlinks=False)
            expected=(before.st_dev,before.st_ino,before.st_uid,stat.S_IMODE(before.st_mode),before.st_nlink,before.st_size,before.st_mtime_ns)
            require((opened.st_dev,opened.st_ino,opened.st_uid,stat.S_IMODE(opened.st_mode),opened.st_nlink,opened.st_size,opened.st_mtime_ns)==expected
                    and (current.st_dev,current.st_ino,current.st_uid,stat.S_IMODE(current.st_mode),current.st_nlink,current.st_size,current.st_mtime_ns)==expected
                    and stat.S_ISREG(current.st_mode),"journal named record changed after reading")
        root_after=os.fstat(root_fd); root_named=os.stat(root.name,dir_fd=parent_fd,follow_symlinks=False)
        parent_after=os.fstat(parent_fd); parent_named_after=os.stat(BASE.PARENT,follow_symlinks=False)
        require((parent_after.st_dev,parent_after.st_ino,parent_after.st_uid,stat.S_IMODE(parent_after.st_mode))==
                (parent_info.st_dev,parent_info.st_ino,parent_info.st_uid,stat.S_IMODE(parent_info.st_mode))
                and (parent_named_after.st_dev,parent_named_after.st_ino,parent_named_after.st_uid,stat.S_IMODE(parent_named_after.st_mode))==
                (parent_info.st_dev,parent_info.st_ino,parent_info.st_uid,stat.S_IMODE(parent_info.st_mode))
                and (root_after.st_dev,root_after.st_ino,root_after.st_uid,stat.S_IMODE(root_after.st_mode))==
                (info.st_dev,info.st_ino,info.st_uid,stat.S_IMODE(info.st_mode))
                and (root_named.st_dev,root_named.st_ino,root_named.st_uid,stat.S_IMODE(root_named.st_mode))==
                (info.st_dev,info.st_ino,info.st_uid,stat.S_IMODE(info.st_mode)),"journal root or parent changed")
        complete=len(events)==len(actions)*3
        return {"state":"MODEL_COMPLETED_HISTORICAL" if complete else ("EMPTY_INSPECTION" if not events else "RECOVERY_REQUIRED"),
                "events":events,"runtime_authorized":False,"rollout_authorized":False,"release_authorized":False}
    finally:
        for fd in reversed(fds):
            try: os.close(fd)
            except OSError: pass
        if root_fd is not None: os.close(root_fd)
        os.close(parent_fd)
