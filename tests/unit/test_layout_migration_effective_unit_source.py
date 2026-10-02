import copy
import os
from pathlib import Path
import tempfile
import stat
import types
import unittest
from unittest import mock

from test_layout_migration_exact_baseline_collector import C


UNIT = "pvedaemon.service"


def props(unit=UNIT):
    return {"Id": unit, "LoadState": "loaded", "FragmentPath": "/usr/lib/systemd/system/" + unit,
            "DropInPaths": "", "SourcePath": "", "UnitFileState": "enabled", "Transient": "no",
            "NeedDaemonReload": "no"}


def wire(value):
    return ("\n".join(key + "=" + item for key, item in value.items()) + "\n").encode("ascii")


class PropertyTests(unittest.TestCase):
    def test_fixed_argv_all_includes_explicit_empty_properties(self):
        value = props()
        result = types.SimpleNamespace(returncode=0, stdout=wire(value), stderr=b"")
        with mock.patch.object(C.subprocess, "run", return_value=result) as run:
            self.assertEqual(C.source_properties(UNIT), value)
        argv = run.call_args.args[0]
        self.assertEqual(argv, ["/usr/bin/systemctl", "show", "--all", "--no-pager",
            *["--property=" + key for key in C.SOURCE_FIELDS], "--", UNIT])
        self.assertEqual(run.call_args.kwargs["timeout"], 10)
        self.assertNotIn("shell", run.call_args.kwargs)
        self.assertEqual(set(run.call_args.kwargs["env"]), {"PATH", "LC_ALL", "LANG"})
        with mock.patch.object(C.subprocess, "run") as run:
            for unit in ("evil.service", "--all", UNIT + "; reboot", "../pvedaemon.service"):
                with self.assertRaises(C.Refusal): C.source_properties(unit)
            run.assert_not_called()

    def test_empty_critical_missing_duplicate_unknown_and_control_fields_refuse(self):
        for field in C.SOURCE_FIELDS:
            value = props(); del value[field]
            with self.subTest(missing=field), self.assertRaises(C.Refusal):
                C.parse_source_properties(wire(value), UNIT)
        for field in ("Id", "LoadState", "FragmentPath", "UnitFileState", "Transient", "NeedDaemonReload"):
            value = props(); value[field] = ""
            with self.subTest(empty=field), self.assertRaises(C.Refusal):
                C.parse_source_properties(wire(value), UNIT)
        for raw in (wire(props()) + b"Id=pvedaemon.service\n", wire(props()) + b"Other=1\n",
                    wire(props()).replace(b"SourcePath=\n", b"SourcePath=\r\n"), wire(props()).rstrip(),
                    wire(props()) + b"\n", b"x" * 65537):
            with self.assertRaises(C.Refusal): C.parse_source_properties(raw, UNIT)

    def test_generated_transient_dropin_and_reload_needed_refuse(self):
        for key, value in (("SourcePath", "/etc/init.d/pvedaemon"), ("Transient", "yes"),
                           ("DropInPaths", "/run/systemd/system.control/pvedaemon.service.d/50.conf"),
                           ("DropInPaths", " "), ("NeedDaemonReload", "yes")):
            item = props(); item[key] = value
            with self.subTest(key=key), self.assertRaises(C.Refusal):
                C.parse_source_properties(wire(item), UNIT)

    def test_all_foreign_effective_sources_and_unit_states_refuse(self):
        for root in ("/run/systemd/generator", "/run/systemd/transient", "/etc/systemd/system.control",
                     "/run/systemd/system.control", "/etc/systemd/system", "/usr/local/lib/systemd/system",
                     "/tmp", "/usr/lib/systemd/system/.."):
            value = props(); value["FragmentPath"] = root + "/" + UNIT
            with self.subTest(root=root), self.assertRaises(C.Refusal): C.validate_source(value, UNIT, False)
        for state in ("generated", "transient", "alias", "linked", "linked-runtime", "enabled-runtime",
                      "masked-runtime", "bad", "not-found"):
            value = props(); value["UnitFileState"] = state
            with self.subTest(state=state), self.assertRaises(C.Refusal): C.validate_source(value, UNIT, False)

    def test_exact_mask_and_usrmerge_alias_need_independent_namespace_proof(self):
        for state in C.STOCK_UNIT_STATES:
            value = props(); value["UnitFileState"] = state
            C.validate_source(value, UNIT, False)
        value = props(); value.update(LoadState="masked", UnitFileState="masked", FragmentPath="/dev/null")
        C.validate_source(value, UNIT, True)
        with self.assertRaises(C.Refusal): C.validate_source(value, UNIT, False)
        with self.assertRaises(C.Refusal): C.validate_source(props(), UNIT, True)
        value = props(); value["FragmentPath"] = "/lib/systemd/system/" + UNIT
        with self.assertRaises(C.Refusal): C.validate_source(value, UNIT, False)
        with self.assertRaises(C.Refusal): C.validate_source(value, UNIT, False, {"already_validated_alias": True})
        C.validate_source(value, UNIT, False, {"path": "/lib", "target": "usr/lib",
            "identity": [1, 2, stat.S_IFLNK | 0o777, 0, 0, 1, 7, 1, 1]})

    def test_probe_nonzero_stderr_and_timeout_never_return_properties(self):
        for result in (types.SimpleNamespace(returncode=1, stdout=wire(props()), stderr=b""),
                       types.SimpleNamespace(returncode=0, stdout=wire(props()), stderr=b"warning")):
            with mock.patch.object(C.subprocess, "run", return_value=result), self.assertRaises(C.Refusal):
                C.source_properties(UNIT)
        with mock.patch.object(C.subprocess, "run", side_effect=C.subprocess.TimeoutExpired("systemctl", 10)):
            with self.assertRaises(C.subprocess.TimeoutExpired): C.source_properties(UNIT)


@unittest.skipUnless(os.name == "posix", "POSIX pinned unit inode tests")
class NamespaceTests(unittest.TestCase):
    def setUp(self):
        if os.geteuid() != 0: self.skipTest("root ownership fixture")
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.paths, self.fds = {}, {}
        for tag, logical in (("etc", "/etc/systemd/system"), ("run", "/run/systemd/system"),
                             ("vendor", "/usr/lib/systemd/system")):
            path = Path(self.tmp.name) / tag; path.mkdir(mode=0o700)
            self.paths[logical] = path
            self.fds[logical] = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
            self.addCleanup(os.close, self.fds[logical])
        for unit in C.M.UNITS:
            path = self.paths["/usr/lib/systemd/system"] / unit
            path.write_bytes(b"[Unit]\nDescription=fixture\n"); path.chmod(0o644)
        self.pins = types.SimpleNamespace(directory=lambda p: self.fds[p], check=lambda: None)
        self.patch = mock.patch.object(C, "source_properties", side_effect=lambda unit: props(unit))
        self.source = self.patch.start(); self.addCleanup(self.patch.stop)

    def test_double_capture_pins_content_inode_and_effective_properties(self):
        before = C.namespace(self.pins)
        self.assertEqual(C.namespace(self.pins), before)
        self.assertEqual(before["effective:" + UNIT]["properties"], props())
        path = self.paths["/usr/lib/systemd/system"] / UNIT
        original = path.read_bytes(); replacement = path.with_name("replacement")
        replacement.write_bytes(original); replacement.chmod(0o644); replacement.replace(path)
        self.assertNotEqual(C.namespace(self.pins), before, "same bytes/different inode must change the baseline")

    def test_mid_capture_same_bytes_inode_replacement_refuses(self):
        original = C.read_regular; count = {}
        def read(fd, unit, maximum=4 * 1024 * 1024):
            count[unit] = count.get(unit, 0) + 1
            if unit == UNIT and count[unit] == 2:
                path = self.paths["/usr/lib/systemd/system"] / unit
                replacement = path.with_name("replacement")
                replacement.write_bytes(path.read_bytes()); replacement.chmod(0o644); replacement.replace(path)
            return original(fd, unit, maximum)
        with mock.patch.object(C, "read_regular", side_effect=read), self.assertRaisesRegex(C.Refusal, "vendor inode changed"):
            C.namespace(self.pins)

    def test_effective_properties_change_during_pin_refuses(self):
        counts = {}
        def changing(unit):
            counts[unit] = counts.get(unit, 0) + 1
            value = props(unit)
            if counts[unit] == 2: value["UnitFileState"] = "disabled"
            return value
        self.source.side_effect = changing
        with self.assertRaisesRegex(C.Refusal, "effective source changed"):
            C.namespace(self.pins)

    def test_vendor_symlink_and_dropin_refuse(self):
        path = self.paths["/usr/lib/systemd/system"] / UNIT
        path.unlink(); path.symlink_to("/dev/null")
        with self.assertRaises((OSError, C.Refusal)): C.namespace(self.pins)
        path.unlink(); path.write_bytes(b"[Unit]\n"); path.chmod(0o644)
        (self.paths["/run/systemd/system"] / (UNIT + ".d")).mkdir()
        with self.assertRaisesRegex(C.Refusal, "drop-in"): C.namespace(self.pins)

    def test_usrmerge_alias_exact_target_and_inode_race(self):
        path = Path(self.tmp.name) / "lib"
        root_fd = os.open(self.tmp.name, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, root_fd)
        pins = types.SimpleNamespace(directory=lambda p: root_fd, check=lambda: None)
        path.symlink_to("usr/lib")
        value = C.stock_lib_alias(pins)
        self.assertEqual(value["target"], "usr/lib")
        path.unlink(); path.symlink_to("/tmp/foreign")
        with self.assertRaises(C.Refusal): C.stock_lib_alias(pins)
        path.unlink(); path.symlink_to("usr/lib")
        readlink = os.readlink
        def changed(*args, **kwargs):
            value = readlink(*args, **kwargs)
            # Keep the predecessor inode allocated while replacing its name.
            path.rename(Path(self.tmp.name) / "old-lib")
            path.symlink_to("usr/lib")
            return value
        with mock.patch.object(C.os, "readlink", side_effect=changed):
            with self.assertRaisesRegex(C.Refusal, "alias changed"):
                C.stock_lib_alias(pins)


if __name__ == "__main__": unittest.main()
