# Testing

Result categories are PASS, FAIL, BLOCKED, SKIPPED, INFRASTRUCTURE_FAIL, and NEEDS_USER_APPROVAL. A destructive test is not PASS unless cleanup and post-test POC health both pass.

CI-safe checks:

```sh
python3 -m unittest discover -s tests/unit -v
prove -v tests/unit/plugin_lifecycle.t
perl -c usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm
python3 -m py_compile usr/libexec/pve-sharedlvmthin/sharedlvmthin-web usr/libexec/pve-sharedlvmthin/sharedlvmthin-health-json
bash -n usr/sbin/* DEBIAN/postinst DEBIAN/postrm DEBIAN/prerm
sh scripts/build.sh
```

Integration tests must use reserved `SLT-AUTOTEST-*` resources and a ledger containing preconditions, objects, actions, result, cleanup, and final POC health. Failure injection requires an approved recovery plan and must never combine failure classes.
