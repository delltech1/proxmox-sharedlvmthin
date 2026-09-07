# Tests

The public tree contains CI-safe unit and lifecycle tests. They use mocks and
fixtures and must not mutate PVE, LVM, multipath, or block devices.

```bash
python3 -m unittest discover -s tests/unit -v
prove -v tests/unit/*.t
```

Hardware, SAN, failure-injection, migration and guest integration qualification
must be performed in an explicitly disposable environment. Those environment-
specific harnesses and their infrastructure identities are intentionally not
part of the public source tree.
