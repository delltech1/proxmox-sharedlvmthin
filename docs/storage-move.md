# Storage move

Online storage move uses PVE drive mirror and the normal storage-plugin lifecycle. Test both source-retained and source-deleted behavior. When PVE retains the source as `unused`, deleting that entry should remove the source LV and remove the owned per-VM pool only when no VM disk or snapshot remains.

Always inspect both source and destination for orphan volumes after an interrupted or failed move. Do not manually remove ambiguous LVs by name alone.
