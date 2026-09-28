import json
import os
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]
MODULE_DIR = ROOT / "experiments" / "thick-generations"


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux pidfd only")
class AnchoredSelfPIDFDCloseTests(unittest.TestCase):
    def _run(self, body):
        source = (
            "import json,sys,time\n"
            f"sys.path.insert(0,{str(MODULE_DIR)!r})\n"
            "import prelive_anchored_self_pidfd as lab\n" + body)
        return subprocess.run([sys.executable, "-I", "-c", source],
                              text=True, capture_output=True, timeout=10)

    def test_disposable_success_is_inert_and_anchor_survives(self):
        proc = self._run(
            "r=lab.run_disposable(time.monotonic_ns()+2_000_000_000)\n"
            "print(json.dumps(r,sort_keys=True))\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)
        self.assertEqual(result["classification"],
                         "DISPOSABLE_ANCHORED_PRIMARY_CLOSE_CONFIRMED")
        self.assertEqual(result["same_raw"], 0)
        self.assertEqual(result["same_errno"], 0)
        self.assertEqual(result["primary_close_attempts"], 1)
        self.assertTrue(result["primary_close_confirmed"])
        self.assertTrue(result["anchor_owned"])
        self.assertTrue(result["current_anchor_signal0_usable"])
        self.assertFalse(result["runtime_authorized"])
        self.assertFalse(result["storage_authorized"])
        self.assertFalse(result["exec_proven"])

    def test_expired_deadline_refuses_before_pidfd(self):
        proc = self._run(
            "g=lab.AnchoredSelfPIDFDCloseGate()\n"
            "try:g.run_once(time.monotonic_ns()-1)\n"
            "except BaseException as e: print(type(e).__name__,str(e))\n"
            "else: raise SystemExit(91)\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("deadline before acquisition", proc.stdout)

    def test_gate_is_one_shot_after_refusal(self):
        proc = self._run(
            "g=lab.AnchoredSelfPIDFDCloseGate()\n"
            "for d in (time.monotonic_ns()-1,time.monotonic_ns()+10**9):\n"
            " try:g.run_once(d)\n"
            " except BaseException as e: print(type(e).__name__,str(e))\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("one-shot", proc.stdout)

    def test_trace_hook_is_refused(self):
        proc = self._run(
            "g=lab.AnchoredSelfPIDFDCloseGate()\n"
            "sys.settrace(lambda *a: None)\n"
            "try:g.run_once(time.monotonic_ns()+10**9)\n"
            "except BaseException as e: print(type(e).__name__,str(e))\n"
            "else: raise SystemExit(92)\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("uncontrolled execution environment", proc.stdout)

    def test_gate_binding_is_immutable(self):
        proc = self._run(
            "g=lab.AnchoredSelfPIDFDCloseGate()\n"
            "try:g._close=lambda fd:None\n"
            "except BaseException as e: print(type(e).__name__,str(e))\n"
            "else: raise SystemExit(93)\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("immutable", proc.stdout)

    def test_strict_deadline_type(self):
        proc = self._run(
            "g=lab.AnchoredSelfPIDFDCloseGate()\n"
            "try:g.run_once(True)\n"
            "except BaseException as e: print(type(e).__name__,str(e))\n"
            "else: raise SystemExit(94)\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("strict positive", proc.stdout)

    def test_preimport_posix_close_substitution_is_refused(self):
        source = (
            "import os,posix,sys\n"
            "posix.close=lambda fd:None\n"
            f"sys.path.insert(0,{str(MODULE_DIR)!r})\n"
            "try: import prelive_anchored_self_pidfd as lab; "
            "lab.AnchoredSelfPIDFDCloseGate()\n"
            "except BaseException as e: print(type(e).__name__,str(e))\n"
            "else: raise SystemExit(95)\n")
        proc = subprocess.run([sys.executable, "-I", "-c", source],
                              text=True, capture_output=True, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("original POSIX", proc.stdout)

    def test_preimport_critical_function_substitutions_are_refused(self):
        cases = (
            ("import posix; posix.pidfd_open=lambda *a:41", "pidfd"),
            ("import fcntl; fcntl.fcntl=lambda *a:42", "dup"),
            ("import time; time.monotonic_ns=lambda:1", "clock"),
        )
        for mutation, label in cases:
            with self.subTest(label=label):
                source = (
                    "import os,sys\n" + mutation + "\n"
                    f"sys.path.insert(0,{str(MODULE_DIR)!r})\n"
                    "try: import prelive_anchored_self_pidfd as lab; "
                    "lab.AnchoredSelfPIDFDCloseGate()\n"
                    "except BaseException as e: print(type(e).__name__,str(e))\n"
                    "else: raise SystemExit(96)\n")
                proc = subprocess.run([sys.executable, "-I", "-c", source],
                                      text=True, capture_output=True, timeout=10)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("original POSIX", proc.stdout)

    def test_audit_hook_cannot_interpose_on_success_sequence(self):
        proc = self._run(
            "events=[]\n"
            "def audit(event,args):\n"
            " if event in ('fcntl.fcntl','ctypes.get_errno'):\n"
            "  events.append(event); raise RuntimeError('audit interposition')\n"
            "sys.addaudithook(audit)\n"
            "r=lab.run_disposable(time.monotonic_ns()+2_000_000_000)\n"
            "print(json.dumps({'events':events,'receipt':r},sort_keys=True))\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)
        self.assertEqual(result["events"], [])
        self.assertTrue(result["receipt"]["primary_close_confirmed"])
        self.assertEqual(result["receipt"]["primary_close_attempts"], 1)
        self.assertEqual(result["receipt"]["primary_acquired_fd"],
                         result["receipt"]["primary_close_target"])

    def test_syscall_table_is_deeply_immutable(self):
        proc = self._run(
            "g=lab.AnchoredSelfPIDFDCloseGate()\n"
            "errors=[]\n"
            "for table in (lab._SYSCALLS[g._machine],g._syscall_nr):\n"
            " try:table['close']=24\n"
            " except BaseException as e:errors.append(type(e).__name__)\n"
            "print(json.dumps(errors))\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), ["TypeError", "TypeError"])

    def test_automatic_gc_is_disabled_during_descriptor_sequence(self):
        proc = self._run(
            "import gc\n"
            "events=[]\n"
            "gc.callbacks.append(lambda phase,info:events.append((phase,gc.isenabled())))\n"
            "r=lab.run_disposable(time.monotonic_ns()+2_000_000_000)\n"
            "print(json.dumps({'events':events,'confirmed':r['primary_close_confirmed']}))\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)
        self.assertTrue(result["confirmed"])
        self.assertEqual(result["events"], [["start", True], ["stop", True]])

    def test_dup_emfile_retains_only_confirmed_primary(self):
        proc = self._run(
            "import resource\n"
            "opened=len(__import__('os').listdir('/proc/self/fd'))-1\n"
            "resource.setrlimit(resource.RLIMIT_NOFILE,(opened+1,opened+1))\n"
            "g=lab.AnchoredSelfPIDFDCloseGate()\n"
            "try:g.run_once(time.monotonic_ns()+2_000_000_000)\n"
            "except BaseException:\n"
            " print(json.dumps(g.outcome(),sort_keys=True))\n"
            "else: raise SystemExit(97)\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)
        self.assertTrue(result["primary_owned"])
        self.assertIsInstance(result["primary_fd"], int)
        self.assertFalse(result["anchor_owned"])
        self.assertIsNone(result["anchor_fd"])
        self.assertEqual(result["primary_close_attempts"], 0)

    def test_post_mask_audit_hook_cannot_reenable_gc(self):
        proc = self._run(
            "import gc\n"
            "seen=[0]\n"
            "def audit(event,args):\n"
            " if event=='os.listdir':\n"
            "  seen[0]+=1\n"
            "  if seen[0]==2:gc.enable()\n"
            "sys.addaudithook(audit)\n"
            "g=lab.AnchoredSelfPIDFDCloseGate()\n"
            "try:g.run_once(time.monotonic_ns()+2_000_000_000)\n"
            "except BaseException as e:\n"
            " print(json.dumps({'error':str(e),'outcome':g.outcome()}))\n"
            "else:raise SystemExit(98)\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)
        self.assertIn("GC re-enabled", result["error"])
        self.assertIsNone(result["outcome"]["primary_acquired_fd"])
        self.assertEqual(result["outcome"]["primary_close_attempts"], 0)

    def test_post_mask_audit_hook_cannot_unmask_signals(self):
        proc = self._run(
            "import signal\n"
            "seen=[0]\n"
            "def audit(event,args):\n"
            " if event=='os.listdir':\n"
            "  seen[0]+=1\n"
            "  if seen[0]==2:signal.pthread_sigmask(signal.SIG_SETMASK,set())\n"
            "sys.addaudithook(audit)\n"
            "g=lab.AnchoredSelfPIDFDCloseGate()\n"
            "try:g.run_once(time.monotonic_ns()+2_000_000_000)\n"
            "except BaseException as e:\n"
            " print(json.dumps({'error':str(e),'outcome':g.outcome()}))\n"
            "else:raise SystemExit(99)\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)
        self.assertIn("signals unmasked", result["error"])
        self.assertIsNone(result["outcome"]["primary_acquired_fd"])
        self.assertEqual(result["outcome"]["primary_close_attempts"], 0)

    @unittest.skipUnless(hasattr(sys, "monitoring"), "sys.monitoring absent")
    def test_post_mask_audit_hook_cannot_enable_monitoring(self):
        proc = self._run(
            "mon=sys.monitoring\n"
            "tool=5; seen=[0]\n"
            "def callback(*args):return None\n"
            "def audit(event,args):\n"
            " if event=='os.listdir':\n"
            "  seen[0]+=1\n"
            "  if seen[0]==2:\n"
            "   mon.use_tool_id(tool,'slt-test')\n"
            "   mon.register_callback(tool,mon.events.LINE,callback)\n"
            "   mon.set_events(tool,mon.events.LINE)\n"
            "sys.addaudithook(audit)\n"
            "g=lab.AnchoredSelfPIDFDCloseGate()\n"
            "try:g.run_once(time.monotonic_ns()+2_000_000_000)\n"
            "except BaseException as e:\n"
            " result={'error':str(e),'outcome':g.outcome()}\n"
            " mon.set_events(tool,0)\n"
            " mon.register_callback(tool,mon.events.LINE,None)\n"
            " mon.free_tool_id(tool)\n"
            " print(json.dumps(result))\n"
            "else:raise SystemExit(100)\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)
        self.assertIn("environment changed", result["error"])
        self.assertIsNone(result["outcome"]["primary_acquired_fd"])
        self.assertEqual(result["outcome"]["primary_close_attempts"], 0)

    def test_failed_attempt_retains_ledger(self):
        proc = self._run(
            "g=lab.AnchoredSelfPIDFDCloseGate()\n"
            "try:g.run_once(time.monotonic_ns()-1)\n"
            "except BaseException: pass\n"
            "print(json.dumps(g.outcome(),sort_keys=True))\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)
        self.assertEqual(result["primary_close_attempts"], 0)
        self.assertFalse(result["primary_close_confirmed"])
        self.assertFalse(result["runtime_authorized"])


if __name__ == "__main__":
    unittest.main()
