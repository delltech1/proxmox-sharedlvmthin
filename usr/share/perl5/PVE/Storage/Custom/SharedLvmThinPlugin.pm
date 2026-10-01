# Copyright (C) 2026 BASTRIX Project Contributors
# SPDX-License-Identifier: GPL-3.0-only

package PVE::Storage::Custom::SharedLvmThinPlugin;

use strict;
use warnings;
use Fcntl qw(F_SETFD O_CREAT O_RDWR :flock :mode);
use Errno qw(ENOENT);

use Digest::SHA qw(sha256_hex);
use JSON::PP qw(decode_json);
use POSIX ();
use Scalar::Util qw(tainted);
use Time::HiRes ();
use PVE::Storage::Plugin;
use PVE::Storage::LVMPlugin;
use PVE::Cluster;
use PVE::SSHInfo;
use PVE::Tools ();
use PVE::SharedLvmThinSafety;
use PVE::SharedLvmThinThick qw(
    anchor_name clone_geometry decode_anchor_tags decode_generation_tags decode_transition_tags
    generation_name mapper_name object_key
    materialized_rebase_state validate_anchor_transition validate_generation_tags
    vg_intent_tags decode_vg_intent_tags
    transition_tags validate_transition_tags
    lazy_object_tags decode_lazy_object_tags validate_lazy_object_tags
);

use base qw(PVE::Storage::Plugin);

sub api {
    # Advertise the exact host API only inside the explicitly qualified range.
    # This avoids a false "older storage API" warning on API 15 without ever
    # claiming compatibility with an unaudited future API.
    my $runtime = __PACKAGE__->_runtime_storage_api();
    die "PVE Storage API $runtime is outside the tested SharedLvmThin range 14..15\n"
        if $runtime < 14 || $runtime > 15;
    return $runtime;
}

use constant MIN_TESTED_PVE_STORAGE_API => 14;
use constant MAX_TESTED_PVE_STORAGE_API => 15;

# scripts/build.sh replaces this template token in the staged package only.
# A long-lived PVE worker retains the value it actually loaded, while the
# root-owned marker on disk changes with the installed payload.  Their exact
# comparison prevents old workers from mutating storage after a package swap.
sub _loaded_runtime_build_id {
    return '__SLT_RUNTIME_BUILD_ID__';
}

sub _runtime_build_id_path {
    return '/usr/share/pve-sharedlvmthin/runtime-build-id';
}

sub _runtime_release_path {
    return '/var/lib/pve-sharedlvmthin/update-guard/runtime-release.json';
}

sub _maintenance_state_dir {
    return '/var/lib/pve-sharedlvmthin/maintenance';
}

sub _update_guard_state_dir {
    return '/var/lib/pve-sharedlvmthin/update-guard';
}

sub _current_boot_id {
    open(my $fh, '<', '/proc/sys/kernel/random/boot_id')
        or die "cannot read current boot identity: $!\n";
    my $boot = <$fh>;
    my $extra = <$fh>;
    close($fh) or die "cannot close current boot identity: $!\n";
    $boot =~ s/\s+$// if defined($boot);
    die "current boot identity is malformed\n"
        if !defined($boot) || defined($extra)
        || $boot !~ /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
    return $boot;
}

sub _current_kernel_release {
    my ($release) = (POSIX::uname())[2];
    die "current kernel release is malformed\n"
        if !defined($release) || $release !~ /^([A-Za-z0-9_.+~-]{1,127})$/;
    return $1;
}

sub _assert_runtime_release_identity {
    my ($class, $operation, $path) = @_;
    $path //= $class->_runtime_release_path();
    die "runtime release identity path is unsafe\n"
        if $path !~ m{^(/[A-Za-z0-9_.-]+)+$};
    my @identity = lstat($path);
    die "RUNTIME_NOT_QUALIFIED: $operation is refused because no safe runtime "
        . "release receipt exists\n"
        if !@identity || !S_ISREG($identity[2]) || $identity[4] != 0
        || ($identity[2] & 0022) || $identity[7] < 2 || $identity[7] > 16_384;
    open(my $fh, '<', $path)
        or die "RUNTIME_NOT_QUALIFIED: cannot read runtime release receipt: $!\n";
    local $/;
    my $raw = <$fh>;
    close($fh) or die "RUNTIME_NOT_QUALIFIED: cannot close runtime release receipt: $!\n";
    my $receipt = eval { decode_json($raw) };
    die "RUNTIME_NOT_QUALIFIED: runtime release receipt is malformed\n"
        if ref($receipt) ne 'HASH' || ($receipt->{schema} // 0) != 1
        || ($receipt->{qualified} // '') ne 'QUALIFIED'
        || ($receipt->{runtime_build_id} // '') !~ /^[0-9a-f]{64}$/
        || ($receipt->{plan_digest} // '') !~ /^[0-9a-f]{64}$/;
    my $loaded = $class->_loaded_runtime_build_id();
    die "RUNTIME_NOT_QUALIFIED: runtime receipt belongs to another loaded build\n"
        if $receipt->{runtime_build_id} ne $loaded;
    die "RUNTIME_NOT_QUALIFIED: runtime receipt belongs to another boot\n"
        if ($receipt->{boot_id} // '') ne $class->_current_boot_id();
    die "RUNTIME_NOT_QUALIFIED: runtime receipt belongs to another kernel\n"
        if ($receipt->{kernel_release} // '') ne $class->_current_kernel_release();
    return 1;
}

sub _assert_loaded_runtime_identity {
    my ($class, $operation, $path) = @_;
    $path //= $class->_runtime_build_id_path();
    die "runtime build identity path is unsafe\n"
        if $path !~ m{^(/[A-Za-z0-9_.-]+)+$};
    my @identity = lstat($path);
    die "LOADED_RUNTIME_UNVERIFIED: $operation is refused because the installed "
        . "runtime identity is missing or unsafe\n"
        if !@identity || !S_ISREG($identity[2]) || $identity[4] != 0
        || ($identity[2] & 0022);
    open(my $fh, '<', $path)
        or die "LOADED_RUNTIME_UNVERIFIED: cannot read installed runtime identity: $!\n";
    my $installed = <$fh>;
    my $extra = <$fh>;
    close($fh)
        or die "LOADED_RUNTIME_UNVERIFIED: cannot close installed runtime identity: $!\n";
    $installed =~ s/\s+$// if defined($installed);
    my $loaded = $class->_loaded_runtime_build_id();
    die "LOADED_RUNTIME_UNVERIFIED: installed runtime identity is malformed\n"
        if !defined($installed) || defined($extra) || $installed !~ /^[0-9a-f]{64}$/;
    die "LOADED_RUNTIME_UNVERIFIED: loaded runtime $loaded differs from installed "
        . "$installed; restart and verify the relevant PVE services before $operation\n"
        if !defined($loaded) || $loaded !~ /^[0-9a-f]{64}$/ || $loaded ne $installed;
    return 1;
}

# PVE workers run with Perl taint checks.  Every external command crosses this
# single argv-only boundary.  Values derived from the API, LVM, sysfs or DM are
# accepted only as scalar arguments without control characters.  A tainted
# value may never become an option; operation switches must be code constants.
# PVE::Tools::run_command receives an argv array, never a shell command.
sub _validated_exec_argv {
    my ($command) = @_;
    die "external command must be an argv array\n" if ref($command) ne 'ARRAY' || !@$command;
    my @safe;
    for my $index (0 .. $#$command) {
        my $arg = $command->[$index];
        die "external command argument $index is not a scalar\n"
            if !defined($arg) || ref($arg);
        die "tainted external command argument $index may not be an option\n"
            if $index > 0 && tainted($arg) && $arg =~ /^-/;
        die "external command argument $index contains a control character\n"
            if $arg !~ /^([^\x00-\x1f\x7f]*)$/;
        push @safe, $1;
    }
    die "external command executable must be an absolute path\n"
        if $safe[0] !~ m{^/[A-Za-z0-9_./+-]+$};
    return \@safe;
}

sub run_command {
    my ($command, @options) = @_;
    return PVE::Tools::run_command(_validated_exec_argv($command), @options);
}

sub _runtime_storage_api {
    require PVE::Storage;
    return PVE::Storage::APIVER();
}

sub type {
    return 'sharedlvmthin';
}

sub _package_flavor_path {
    return '/usr/share/pve-sharedlvmthin/package-flavor';
}

sub _package_flavor {
    my ($class) = @_;
    my $path = $class->_package_flavor_path();
    die "SharedLvmThin package flavor marker is missing or unsafe\n"
        if !-f $path || -l $path;
    open(my $fh, '<', $path)
        or die "cannot read SharedLvmThin package flavor '$path': $!\n";
    my $flavor = <$fh>;
    my $extra = <$fh>;
    close($fh)
        or die "cannot close SharedLvmThin package flavor '$path': $!\n";
    die "SharedLvmThin package flavor is missing or ambiguous\n"
        if !defined($flavor) || defined($extra);
    $flavor =~ s/\s+$//;
    die "unknown SharedLvmThin package flavor '$flavor'\n"
        if $flavor ne 'dual' && $flavor ne 'thick-only';
    return $flavor;
}

sub _assert_package_operations_released {
    my ($class, $operation, $maintenance_dir, $update_guard_dir, $runtime_id_path,
        $runtime_release_path) = @_;
    die "package maintenance operation name is invalid\n"
        if !defined($operation) || $operation !~ /^([a-z][a-z0-9 -]{0,63})$/;
    $operation = $1;
    $maintenance_dir //= $class->_maintenance_state_dir();
    $update_guard_dir //= $class->_update_guard_state_dir();
    die "package maintenance directory argument is unsafe\n"
        if $maintenance_dir !~ m{^(/[A-Za-z0-9_.-]+)+$};
    die "package update-guard directory argument is unsafe\n"
        if $update_guard_dir !~ m{^(/[A-Za-z0-9_.-]+)+$};

    my @holds = (
        [$maintenance_dir, 'active.json', 'package maintenance',
            'PACKAGE_MAINTENANCE_HOLD',
            'the explicit cluster maintenance transaction is finalized'],
        [$update_guard_dir, 'post-gate-required.json', 'package update-guard',
            'PACKAGE_UPDATE_UNSETTLED',
            'installed, loaded and boot runtime identities are explicitly verified'],
        [$update_guard_dir, 'package-transition.json', 'package baseline transition',
            'PACKAGE_BASELINE_TRANSITION',
            'the exact package baseline and runtime qualification are settled'],
        [$update_guard_dir, 'runtime-qualification-pending.json', 'runtime qualification',
            'RUNTIME_QUALIFICATION_PENDING',
            'the exact post-install PVE operational gate is finalized'],
    );
    for my $hold (@holds) {
        my ($directory_path, $file, $label, $code, $release) = @$hold;
        my @directory = lstat($directory_path);
        if (!@directory) {
            next if $! == ENOENT;
            die "cannot inspect $label directory: $!\n";
        }
        die "$label directory is unsafe; refusing $operation\n"
            if !S_ISDIR($directory[2]) || $directory[4] != 0
            || ($directory[2] & 0777) != 0700;

        my $path = "$directory_path/$file";
        my @manifest = lstat($path);
        if (!@manifest) {
            next if $! == ENOENT;
            die "cannot inspect $label hold: $!\n";
        }

        # Presence alone means HOLD. Runtime admission must never parse a
        # malformed/symlinked receipt, infer expiry, or reinterpret an unknown
        # package outcome as permission to mutate shared storage.
        die "$code: $operation is refused until $release\n";
    }
    $class->_assert_loaded_runtime_identity($operation, $runtime_id_path);
    return $class->_assert_runtime_release_identity($operation, $runtime_release_path);
}

sub _outer_lock_yield {
    my ($class, $milliseconds) = @_;
    return if !$milliseconds;
    select(undef, undef, undef, $milliseconds / 1000);
    return;
}

# PVE wraps vdisk_alloc() and vdisk_free() in cluster_lock_storage() with an
# undefined timeout.  The base implementation consequently uses the CFS
# default (10 seconds on the qualified PVE 9 releases), which is shorter than
# a legitimate per-VM pool creation once a shared VG contains many objects.
# Honour the same bounded administrator-selected timeout at that outer wrapper
# boundary.  Calls made by this plugin already pass an explicit timeout and
# are therefore left untouched.
sub cluster_lock_storage {
    my ($class, $storeid, $shared, $timeout, $code, @params) = @_;
    my $implicit_outer = !defined($timeout);
    my $yield_ms = 0;

    if ($implicit_outer) {
        my $resolved = eval {
            require PVE::Storage;
            my $cfg = PVE::Storage::config();
            my $scfg = PVE::Storage::storage_config($cfg, $storeid);
            die "not a SharedLvmThin storage\n"
                if ($scfg->{type} // '') ne $class->type();
            return [
                $scfg->{'slt-lock-timeout'} // 30,
                $scfg->{'slt-lock-yield-ms'} // 1000,
            ];
        };
        if (!$@ && defined($resolved)) {
            ($timeout, $yield_ms) = @$resolved;
        }
    }

    my $result = PVE::Storage::Plugin::cluster_lock_storage(
        $class, $storeid, $shared, $timeout, $code, @params,
    );
    # pmxcfs lock acquisition is bounded but not a FIFO admission queue.  A
    # successful client that immediately starts another operation can starve
    # an already waiting node.  Yield only after the outer PVE wrapper has
    # released the lock; never sleep inside the critical section and never
    # retry a callback or an ambiguous failure.
    $class->_outer_lock_yield($yield_ms)
        if $implicit_outer && $shared && $yield_ms > 0;
    return $result;
}

sub plugindata {
    return {
        content => [
            { images => 1, rootdir => 1 },
            { images => 1 },
        ],
        format => [
            { raw => 1 },
            'raw',
        ],
        'sensitive-properties' => {},
    };
}

#
# IMPORTANT:
# Prefix custom properties so we never collide with
# native Proxmox storage properties.
#
sub properties {
    my $thick_only = __PACKAGE__->_package_flavor() eq 'thick-only';
    my $properties = {
        'slt-vgname' => {
            description => 'Backing shared LVM volume group.',
            type => 'string',
        },
        'slt-allocation-mode' => {
            description => $thick_only
                ? 'Experimental lab-only Thick Generations backend: eager or guarded lazy-zero allocation; requires disposable storage.'
                : 'Experimental lab-only backend: per-VM Thin pools, eager Thick Generations, or guarded lazy-zero Thick Generations; all modes require disposable storage.',
            type => 'string',
            # PVE parses the cluster-wide storage.cfg on every node before
            # applying `nodes` scope.  Thick-only must therefore understand
            # remote Thin declarations syntactically even though every local
            # Thin operation remains unavailable at the runtime gate below.
            enum => ['thin', 'thick-generations', 'thick-generations-lazy'],
            default => $thick_only ? 'thick-generations' : 'thin',
        },
        'slt-vg-layout' => {
            description => 'Required isolated failure-domain layout. Thin and Thick modes must use separate VGs; Eager and Lazy Thick may share one Thick VG.',
            type => 'string',
            # PVE parses the full cluster storage.cfg before applying node
            # scope. Keep the retired token syntactically readable during a
            # rolling migration; _vg_layout rejects it for every operation.
            enum => ['isolated', 'mixed'],
            default => 'isolated',
        },
        'slt-tg-hydration-timeout' => {
            description => 'Bounded Thick Generations no-progress timeout in seconds. Continuing verified dm-clone progress may run longer for large disks.',
            type => 'integer',
            minimum => 60,
            maximum => 86400,
            default => 3600,
        },
        'slt-tg-command-deadline-sec' => {
            description => 'Userspace observation deadline in seconds for one dm-clone control command. Expiry makes the outcome UNKNOWN; it is not a storage-failure threshold and cannot terminate kernel D-state I/O.',
            type => 'integer',
            minimum => 5,
            maximum => 600,
            default => 30,
        },
        'slt-tg-close-timeout' => {
            description => 'Bounded observation window in seconds for a Thick frontend open count to reach zero during deactivation. Expiry refuses removal.',
            type => 'integer',
            minimum => 1,
            maximum => 300,
            default => 30,
        },
        'slt-lock-timeout' => {
            description => 'Bounded Proxmox cluster storage-lock acquisition timeout in seconds. Size this from measured worst-case serialized metadata operations; it does not configure or replace the PVE HA watchdog.',
            type => 'integer',
            minimum => 10,
            maximum => 86400,
            default => 30,
        },
        'slt-lock-yield-ms' => {
            description => 'Cooperative post-release delay for outer PVE storage mutations. This reduces cross-node lock starvation without retrying an operation.',
            type => 'integer',
            minimum => 0,
            maximum => 5000,
            default => 1000,
        },
        'slt-mutation-admission-timeout' => {
            description => 'Bounded admission wait for an existing Thick DM transition on the same VG. Polls outside the VG lock; never retries a mutation or clears an intent. Does not alter HA watchdog timeouts.',
            type => 'integer',
            minimum => 10,
            maximum => 86400,
            default => 600,
        },
        'slt-bridge-admission-timeout' => {
            description => 'Bounded wait in seconds for the VG-wide materialized-migration admission. Waiting is observable and uses adaptive polling; expiry performs no storage mutation.',
            type => 'integer',
            minimum => 60,
            maximum => 604800,
            default => 86400,
        },
        'slt-thin-leaseguard' => {
            description => 'Opt-in PVE-native single-kernel activation guard. remote-audit proves peer mapper absence; runtime-guard additionally requires the static ThinGuard daemon to arm watchdog-mux before activation.',
            type => 'string',
            enum => ['disabled', 'remote-audit', 'runtime-guard'],
            default => 'disabled',
        },
        'slt-thin-peer-connect-timeout' => {
            description => 'SSH connection timeout in seconds for each Thin peer mapper probe. A timeout is UNKNOWN and always refuses activation.',
            type => 'integer',
            minimum => 1,
            maximum => 120,
            default => 5,
        },
        'slt-thin-peer-probe-timeout' => {
            description => 'Whole-command timeout in seconds for each Thin peer mapper probe. Must exceed the connection timeout. Size from measured loaded-node latency; expiry is UNKNOWN and never proves fencing.',
            type => 'integer',
            minimum => 2,
            maximum => 600,
            default => 15,
        },
        'slt-thin-ha-takeover' => {
            description => 'Opt-in automatic Thin owner takeover only when fresh PVE HA manager state assigns the fenced service to this node. Requires remote-audit or runtime-guard.',
            type => 'string',
            enum => ['disabled', 'pve-ha'],
            default => 'disabled',
        },
        'slt-tg-hydration-threshold' => {
            description => 'Maximum number of Thick Generations regions copied concurrently during background hydration.',
            type => 'integer',
            minimum => 1,
            maximum => 256,
            default => 32,
        },
        'slt-tg-hydration-batch-size' => {
            description => 'Maximum contiguous Thick Generations regions combined into one background copy request.',
            type => 'integer',
            minimum => 1,
            maximum => 256,
            default => 32,
        },
        'slt-tg-region-size-kib' => {
            description => 'Deterministic minimum dm-clone region size in KiB for new Thick Generations transitions; extreme capacities grow it only to retain the region-count bound. Existing transitions always recover their exact signed region.',
            type => 'integer',
            minimum => 64,
            maximum => 4096,
            default => 1024,
        },
        'slt-tg-max-active-materializations' => {
            description => 'VG-wide ceiling for simultaneously published Thick Generations dm-clone transitions. New snapshot/rollback preparation fails closed at the limit; existing guests and workers are untouched.',
            type => 'integer',
            minimum => 1,
            maximum => 64,
            default => 4,
        },
        'slt-tg-online-materialization' => {
            description => 'Materialize an online Thick Generations snapshot after returning control to PVE, or synchronously while the VM remains paused.',
            type => 'string',
            enum => ['asynchronous', 'synchronous'],
            default => 'asynchronous',
        },
        'slt-initial-pool-size' => {
            description => 'Initial physical size of each per-VM thin pool in GiB.',
            type => 'integer',
            minimum => 1,
            maximum => 1024,
            default => 16,
        },
        'slt-initial-pool-mode' => {
            description => 'Allocation headroom policy: fixed, proportional, elastic, or full.',
            type => 'string',
            enum => ['fixed', 'proportional', 'elastic', 'full'],
            default => 'fixed',
        },
        'slt-initial-pool-percent' => {
            description => 'Requested disk percentage reserved as physical headroom in proportional mode.',
            type => 'integer',
            minimum => 1,
            maximum => 100,
            default => 50,
        },
        'slt-initial-pool-max' => {
            description => 'Optional proportional/full allocation target ceiling in GiB.',
            type => 'integer',
            minimum => 1,
            maximum => 1048576,
        },
        'slt-burst-headroom-gib' => {
            description => 'Absolute physical write-burst headroom maintained by elastic allocation and autogrow.',
            type => 'integer', minimum => 1, maximum => 1024, default => 64,
        },
        'slt-expected-vg-uuid' => {
            description => 'Expected backing VG UUID. A mismatch blocks activation and mutations.',
            type => 'string',
            pattern => '[A-Za-z0-9-]+',
        },
        'slt-expected-pv-uuid' => {
            description => 'Expected UUID of the single backing PV.',
            type => 'string',
            pattern => '[A-Za-z0-9-]+',
        },
        'slt-expected-wwid' => {
            description => 'Expected multipath WWID from /dev/mapper/<WWID>.',
            type => 'string',
            pattern => '[0-9A-Fa-f]+',
        },
        'slt-expected-min-paths' => {
            description => 'Diagnostic minimum number of healthy multipath paths. This does not change or gate SAN policy.',
            type => 'integer',
            minimum => 1,
            maximum => 64,
        },
        'slt-vg-reserve-percent' => {
            description => 'Physical VG reserve percentage protected from automatic thin-pool growth.',
            type => 'integer',
            minimum => 0,
            maximum => 50,
        },
        'slt-vg-reserve-gib' => {
            description => 'Fixed physical VG reserve in GiB protected from automatic thin-pool growth.',
            type => 'integer',
            minimum => 0,
            maximum => 1048576,
        },
    };
    return $properties;
}

sub options {
    my $options = {
        'slt-vgname' => { fixed => 1 },
        'slt-allocation-mode' => { fixed => 1, optional => 1 },
        # Mutable only through an explicit maintenance transition. Keeping it
        # non-fixed gives existing experimental mixed deployments a supported
        # path to declare their topology after every node understands the key.
        'slt-vg-layout' => { optional => 1 },
        'slt-tg-hydration-timeout' => { optional => 1 },
        'slt-tg-command-deadline-sec' => { optional => 1 },
        'slt-tg-close-timeout' => { optional => 1 },
        'slt-lock-timeout' => { optional => 1 },
        'slt-lock-yield-ms' => { optional => 1 },
        'slt-mutation-admission-timeout' => { optional => 1 },
        'slt-bridge-admission-timeout' => { optional => 1 },
        'slt-thin-leaseguard' => { optional => 1 },
        'slt-thin-peer-connect-timeout' => { optional => 1 },
        'slt-thin-peer-probe-timeout' => { optional => 1 },
        'slt-thin-ha-takeover' => { optional => 1 },
        'slt-tg-hydration-threshold' => { optional => 1 },
        'slt-tg-hydration-batch-size' => { optional => 1 },
        'slt-tg-region-size-kib' => { optional => 1 },
        'slt-tg-max-active-materializations' => { optional => 1 },
        'slt-tg-online-materialization' => { optional => 1 },
        'slt-initial-pool-size' => { optional => 1 },
        'slt-initial-pool-mode' => { optional => 1 },
        'slt-initial-pool-percent' => { optional => 1 },
        'slt-initial-pool-max' => { optional => 1 },
        'slt-burst-headroom-gib' => { optional => 1 },
        'slt-expected-vg-uuid' => { optional => 1 },
        'slt-expected-pv-uuid' => { optional => 1 },
        'slt-expected-wwid' => { optional => 1 },
        'slt-expected-min-paths' => { optional => 1 },
        'slt-vg-reserve-percent' => { optional => 1 },
        'slt-vg-reserve-gib' => { optional => 1 },

        # These are common properties inherited from PVE::Storage::Plugin.
        nodes => { optional => 1 },
        disable => { optional => 1 },
        content => { optional => 1 },
        shared => { optional => 1 },
    };
    return $options;
}

sub _allocation_mode {
    my ($class, $scfg) = @_;
    my $flavor = $class->_package_flavor();
    my $mode = $scfg->{'slt-allocation-mode'}
        // ($flavor eq 'thick-only' ? 'thick-generations' : 'thin');
    die "unknown SharedLvmThin allocation mode '$mode'\n"
        if $mode ne 'thin' && $mode ne 'thick-generations'
        && $mode ne 'thick-generations-lazy';
    die "Thin allocation mode is unavailable in the Thick-only package\n"
        if $flavor eq 'thick-only' && $mode eq 'thin';
    return $mode;
}

sub _vg_layout {
    my ($class, $scfg) = @_;
    my $layout = $scfg->{'slt-vg-layout'} // 'isolated';
    die "mixed Thin/Thick VG layout is unsupported; use separate Thin and Thick VGs\n"
        if $layout eq 'mixed';
    die "unknown SharedLvmThin VG layout '$layout'\n"
        if $layout ne 'isolated';
    return $layout;
}

sub _is_thick_mode {
    my ($class, $scfg) = @_;
    return $class->_allocation_mode($scfg) ne 'thin';
}

sub _is_lazy_mode {
    my ($class, $scfg) = @_;
    return $class->_allocation_mode($scfg) eq 'thick-generations-lazy';
}

sub _lazy_materialized {
    my ($class, $storeid, $scfg, $volname) = @_;
    return 0 if !$class->_is_lazy_mode($scfg)
        || !defined($storeid) || !defined($volname);
    my $state;
    my $ok = eval {
        ($state) = $class->_thick_read_anchor($storeid, $scfg, $volname);
        1;
    };
    return $ok && ($state->{phase} // '') eq 'MATERIALIZED' ? 1 : 0;
}

sub _lazy_integration_pending {
    my ($class, $operation) = @_;
    $operation //= 'operation';
    die "Lazy Thick $operation requires completed materialization; refusing Thin or Eager fallback for a clone-backed v6 volume\n";
}

sub _thick_new_geometry {
    my ($class, $scfg, $bytes) = @_;
    my $kib = $scfg->{'slt-tg-region-size-kib'} // 1024;
    die "invalid thick-generations region size\n"
        if !defined($kib) || $kib !~ /^\d+$/ || $kib < 64 || $kib > 4096
        || ($kib & ($kib - 1));
    return clone_geometry($bytes, int($kib) * 2, 1);
}

sub _thick_hydration_tuning {
    my ($class, $scfg, $region) = @_;
    my ($threshold, $batch);
    if (exists($scfg->{'slt-tg-hydration-threshold'})
        || exists($scfg->{'slt-tg-hydration-batch-size'})) {
        $threshold = $scfg->{'slt-tg-hydration-threshold'} // 32;
        $batch = $scfg->{'slt-tg-hydration-batch-size'} // 32;
    } elsif (defined($region) && $region >= 128) {
        # Region is in 512-byte sectors. Keep default background requests near
        # 1 MiB and total concurrently hydrating data near 4 MiB, independently
        # of the selected deterministic region geometry.
        $batch = int(2048 / $region);
        $batch = 1 if $batch < 1;
        $batch = 256 if $batch > 256;
        $threshold = int(8192 / $region);
        $threshold = 1 if $threshold < 1;
        $threshold = 256 if $threshold > 256;
    } else {
        # Preserve the exact historical default for legacy/in-flight geometry.
        ($threshold, $batch) = (32, 32);
    }
    die "invalid thick-generations hydration threshold\n"
        if $threshold !~ /^\d+$/ || $threshold < 1 || $threshold > 256;
    die "invalid thick-generations hydration batch size\n"
        if $batch !~ /^\d+$/ || $batch < 1 || $batch > 256;
    die "thick-generations hydration batch size cannot exceed its threshold\n"
        if $batch > $threshold;
    return (int($threshold), int($batch));
}

sub _thick_online_materialization_mode {
    my ($class, $scfg) = @_;
    my $mode = $scfg->{'slt-tg-online-materialization'} // 'asynchronous';
    die "invalid thick-generations online materialization mode\n"
        if $mode ne 'asynchronous' && $mode ne 'synchronous';
    return $mode;
}

sub _thick_close_timeout {
    my ($class, $scfg) = @_;
    my $timeout = $scfg->{'slt-tg-close-timeout'} // 30;
    die "invalid thick-generations frontend close timeout\n"
        if $timeout !~ /^\d+$/ || $timeout < 1 || $timeout > 300;
    return int($timeout);
}

sub _thick_command_deadline {
    my ($class, $scfg) = @_;
    my $timeout = $scfg->{'slt-tg-command-deadline-sec'} // 30;
    die "invalid thick-generations command observation timeout\n"
        if $timeout !~ /^\d+$/ || $timeout < 5 || $timeout > 600;
    return int($timeout);
}

sub _thin_peer_probe_timing {
    my ($class, $scfg) = @_;
    my $connect = $scfg->{'slt-thin-peer-connect-timeout'} // 5;
    my $probe = $scfg->{'slt-thin-peer-probe-timeout'} // 15;
    die "invalid Thin peer SSH connection timeout\n"
        if $connect !~ /^\d+$/ || $connect < 1 || $connect > 120;
    die "invalid Thin peer whole-probe timeout\n"
        if $probe !~ /^\d+$/ || $probe < 2 || $probe > 600;
    die "Thin peer whole-probe timeout must exceed its SSH connection timeout\n"
        if $probe <= $connect;
    return (int($connect), int($probe));
}

sub _require_thick_identity_config {
    my ($class, $storeid, $scfg) = @_;
    die "thick-generations storage '$storeid' must be configured as shared\n"
        if !$scfg->{shared};
    for my $field (qw(slt-expected-vg-uuid slt-expected-pv-uuid slt-expected-wwid)) {
        die "thick-generations storage '$storeid' requires '$field'\n"
            if !defined($scfg->{$field}) || $scfg->{$field} eq '';
    }
    die "thick-generations storage '$storeid' requires a protected VG reserve\n"
        if !defined($scfg->{'slt-vg-reserve-percent'})
        && !defined($scfg->{'slt-vg-reserve-gib'});
    return 1;
}

sub _thick_namespace {
    my ($class, $scfg) = @_;
    my $uuid = $scfg->{'slt-expected-vg-uuid'};
    die "thick-generations requires a pinned VG UUID before path resolution\n"
        if !defined($uuid) || $uuid eq '';
    return lc($uuid);
}

sub _thick_read_anchor {
    my ($class, $storeid, $scfg, $volname, $lvs) = @_;
    my $vg = $scfg->{'slt-vgname'};
    my $namespace = $class->_thick_namespace($scfg);
    my $anchor = anchor_name($namespace, $volname);
    if (!defined($lvs)) {
        my $wwid = $scfg->{'slt-expected-wwid'}
            // die "thick-generations anchor inventory requires a pinned WWID\n";
        $lvs = $class->_thick_list_volumes_scoped($scfg, $vg, "/dev/mapper/$wwid");
    }
    die "thick-generations storage '$storeid' is unavailable: VG '$vg' is not visible\n"
        if !$lvs->{$vg};
    my $info = $lvs->{$vg}->{$anchor};
    die "thick-generations anchor '$vg/$anchor' is missing\n" if !$info;
    my $state = decode_anchor_tags($info->{tags} // '');
    die "thick-generations anchor '$vg/$anchor' belongs to another storage or volume\n"
        if $state->{sid} ne $storeid || $state->{vol} ne $volname;
    my $head = $lvs->{$vg}->{$state->{head}};
    die "thick-generations head '$vg/$state->{head}' is missing\n" if !$head;
    if (int($state->{v} // 0) == 6) {
        validate_lazy_object_tags(
            $head->{tags} // '', sid => $storeid, vol => $volname,
            tx => $state->{tx}, kind => 'data', bytes => $state->{bytes},
            region => $state->{region},
        );
        die "Lazy Thick data size differs from its signed anchor\n"
            if int($head->{lv_size} // 0) != int($state->{bytes});
        die "Lazy Thick data UUID differs from its signed anchor\n"
            if ($head->{lv_uuid} // '') ne $state->{data_uuid};
        my $metadata = $lvs->{$vg}->{$state->{metadata}};
        die "Lazy Thick metadata '$vg/$state->{metadata}' is missing\n"
            if !$metadata;
        my $geometry = clone_geometry(int($state->{bytes}), int($state->{region}));
        validate_lazy_object_tags(
            $metadata->{tags} // '', sid => $storeid, vol => $volname,
            tx => $state->{tx}, kind => 'metadata',
            bytes => $geometry->{metadata_bytes},
            region => $state->{region},
        );
        die "Lazy Thick metadata LV is smaller than its signed geometry\n"
            if int($metadata->{lv_size} // 0) < $geometry->{metadata_bytes};
        die "Lazy Thick metadata UUID differs from its signed anchor\n"
            if ($metadata->{lv_uuid} // '') ne $state->{metadata_uuid};
    } else {
        validate_generation_tags(
            $head->{tags} // '', sid => $storeid, vol => $volname,
            role => 'head', generation => $state->{generation},
        );
    }
    return ($state, $head, $anchor);
}

sub _thick_anchor {
    my ($class, @args) = @_;
    my ($state, $head, $anchor) = $class->_thick_read_anchor(@args);
    my (undef, $scfg) = @args;
    my $vg = $scfg->{'slt-vgname'};
    die "thick-generations anchor '$vg/$anchor' is not materialized; recovery required\n"
        if $state->{phase} ne 'MATERIALIZED';
    return ($state, $head, $anchor);
}

sub _thick_find_snapshot {
    my ($class, $storeid, $scfg, $volname, $snapname, $lvs) = @_;
    $snapname = _thick_snapshot_name($snapname);
    my $vg = $scfg->{'slt-vgname'};
    my $namespace = $class->_thick_namespace($scfg);
    my $key = object_key($namespace, $volname);
    if (!defined($lvs)) {
        my $wwid = $scfg->{'slt-expected-wwid'}
            // die "thick-generations snapshot inventory requires a pinned WWID\n";
        $lvs = $class->_thick_list_volumes_scoped($scfg, $vg, "/dev/mapper/$wwid");
    }
    die "thick-generations storage '$storeid' is unavailable: VG '$vg' is not visible\n"
        if !$lvs->{$vg};
    my @matches;
    for my $name (sort grep { /^sltg-g-\Q$key\E-\d{8}$/ } keys %{$lvs->{$vg}}) {
        my ($generation) = $name =~ /-(\d{8})$/;
        my $valid = eval {
            my $decoded = decode_generation_tags($lvs->{$vg}->{$name}->{tags} // '');
            die "snapshot belongs to another storage\n"
                if defined($storeid) && $decoded->{sid} ne $storeid;
            die "snapshot identity mismatch\n"
                if $decoded->{vol} ne $volname || $decoded->{role} ne 'snapshot'
                || int($decoded->{generation}) != int($generation)
                || $decoded->{snapshot} ne $snapname;
            1;
        };
        push @matches, [$name, int($generation), $lvs->{$vg}->{$name}] if $valid;
    }
    die "snapshot '$snapname' for '$volname' is missing or ambiguous\n" if @matches != 1;
    return @{$matches[0]};
}

sub _thick_verify_snapshot_readonly {
    my ($class, $scfg, $vg, $lv, $device) = @_;
    my $command_timeout = $class->_thick_command_deadline($scfg);
    my @command = ('/usr/bin/timeout', '--foreground', '--kill-after=5s',
        "${command_timeout}s", '/sbin/lvs', '--readonly');
    push @command, ('--devices', $device) if defined($device);
    push @command, ('--noheadings', '-o', 'lv_attr', "$vg/$lv");
    my $lines = _command_lines(
        \@command,
        "reading snapshot permissions of '$vg/$lv' failed",
    );
    die "snapshot permissions of '$vg/$lv' are ambiguous\n" if @$lines != 1;
    die "snapshot '$vg/$lv' is not read-only\n" if $lines->[0] !~ /^.r/;
    return 1;
}

sub _thick_ensure_snapshot_readonly {
    my ($class, $scfg, $vg, $lv, $info, $device) = @_;
    die "snapshot permission inventory for '$vg/$lv' is missing\n"
        if ref($info) ne 'HASH';
    my $attr = $info->{lv_attr} // '';
    die "snapshot permission inventory for '$vg/$lv' is ambiguous\n"
        if $attr !~ /^.[rw]/;
    my $command_error = '';
    if (substr($attr, 1, 1) eq 'w') {
        my $command_timeout = $class->_thick_command_deadline($scfg);
        eval { run_command(
            ['/usr/bin/timeout', '--foreground', '--kill-after=5s', "${command_timeout}s",
                '/sbin/lvchange', '--devices', $device, '-pr', "$vg/$lv"],
            errmsg => "making snapshot generation '$vg/$lv' read-only failed",
        ); };
        $command_error = $@ if $@;
    }
    my $post_error = '';
    eval { $class->_thick_verify_snapshot_readonly($scfg, $vg, $lv, $device); };
    $post_error = $@ if $@;
    if ($post_error ne '') {
        die $command_error ne ''
            ? "making snapshot generation '$vg/$lv' read-only failed; outcome is UNKNOWN "
                . "because the exact permission postcondition is unproven; no retry "
                . "attempted: $command_error$post_error"
            : $post_error;
    }
    warn "making snapshot generation '$vg/$lv' read-only reported an error, but the "
        . "exact permission postcondition is proven; continuing without retry: "
        . "$command_error" if $command_error ne '';
    return 1;
}

sub _thick_filesystem_path {
    my ($class, $scfg, $volname, $snapname) = @_;
    if (defined($snapname)) {
        my ($snapshot) = $class->_thick_find_snapshot(
            undef, $scfg, $volname, $snapname,
        );
        my (undef, undef, $vmid) = $class->parse_volname($volname);
        my $path = "/dev/$scfg->{'slt-vgname'}/$snapshot";
        return wantarray ? ($path, $vmid, 'images') : $path;
    }
    my $mapper = mapper_name($class->_thick_namespace($scfg), $volname);
    my (undef, undef, $vmid) = $class->parse_volname($volname);
    return wantarray ? ("/dev/mapper/$mapper", $vmid, 'images') : "/dev/mapper/$mapper";
}

sub _lazy_runtime_names {
    my ($class, $scfg, $volname) = @_;
    my $key = object_key($class->_thick_namespace($scfg), $volname);
    return ("sltg-z-$key", "sltg-c-$key", mapper_name(
        $class->_thick_namespace($scfg), $volname,
    ));
}

sub _lazy_local_identity {
    my ($class) = @_;
    my $node = $class->_thin_local_node();
    die "invalid local node identity for Lazy Thick ownership\n"
        if !defined($node) || $node !~ /^[A-Za-z0-9][A-Za-z0-9_.-]*$/;
    open(my $fh, '<', '/proc/sys/kernel/random/boot_id')
        or die "cannot read local boot identity for Lazy Thick ownership: $!\n";
    my $boot = <$fh> // '';
    close($fh);
    $boot =~ s/^\s+|\s+$//g;
    $boot = lc($boot);
    die "invalid local boot identity for Lazy Thick ownership\n"
        if $boot !~ /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
    return ($node, $boot);
}

sub _lazy_mapper_identity {
    my ($class, $scfg, $mapper, $uuid, $table) = @_;
    my $deadline = $class->_thick_command_deadline($scfg);
    my $info = _command_lines(
        ['/usr/bin/timeout', '--foreground', '--kill-after=5s', "${deadline}s",
            '/sbin/dmsetup', 'info', '-c', '--noheadings', '--separator', '|',
            '-o', 'uuid,readonly,major,minor,suspended', $mapper],
        "reading Lazy Thick mapper '$mapper' identity failed",
    );
    die "Lazy Thick mapper '$mapper' identity is ambiguous\n" if @$info != 1;
    my ($actual_uuid, $readonly, $major, $minor, $suspended) = split(/\|/, $info->[0], -1);
    for ($actual_uuid, $readonly, $major, $minor, $suspended) { s/^\s+|\s+$//g; }
    die "Lazy Thick mapper '$mapper' UUID mismatch\n" if $actual_uuid ne $uuid;
    my $expected_access = $uuid =~ /^SLT-TG6-ZERO-/ ? 'read-only' : 'writeable';
    die "Lazy Thick mapper '$mapper' access mode mismatch\n"
        if lc($readonly) ne $expected_access;
    die "Lazy Thick mapper '$mapper' is unexpectedly suspended\n"
        if lc($suspended) ne 'active';
    my $tables = _command_lines(
        ['/usr/bin/timeout', '--foreground', '--kill-after=5s', "${deadline}s",
            '/sbin/dmsetup', 'table', $mapper],
        "reading Lazy Thick mapper '$mapper' table failed",
    );
    die "Lazy Thick mapper '$mapper' table mismatch\n"
        if @$tables != 1 || $tables->[0] ne $table;
    $class->_thick_verify_mapper_node_ready($scfg, $mapper, $major, $minor);
    my $diskseq_path = "/sys/dev/block/$major:$minor/diskseq";
    die "Lazy Thick mapper '$mapper' has no exact diskseq identity\n"
        if !-f $diskseq_path || -l $diskseq_path;
    open(my $diskseq_fh, '<', $diskseq_path)
        or die "cannot read Lazy Thick mapper '$mapper' diskseq: $!\n";
    my $diskseq = <$diskseq_fh> // '';
    close($diskseq_fh);
    $diskseq =~ s/^\s+|\s+$//g;
    die "Lazy Thick mapper '$mapper' returned invalid diskseq identity\n"
        if $diskseq !~ /^[1-9][0-9]*$/;
    return wantarray ? ("$major:$minor", $diskseq) : "$major:$minor";
}

sub _lazy_install_discard_guard {
    my ($class, $scfg, $mapper, $uuid, $table) = @_;
    my ($devno, $diskseq) =
        $class->_lazy_mapper_identity($scfg, $mapper, $uuid, $table);
    for my $attribute (qw(discard_max_bytes write_zeroes_max_bytes)) {
        my $queue = "/sys/dev/block/$devno/queue/$attribute";
        die "Lazy Thick I/O guard path is not an exact regular sysfs attribute\n"
            if !-f $queue || -l $queue;
        my $write_error = '';
        eval {
            open(my $out, '>', $queue)
                or die "cannot set Lazy Thick $attribute guard on unpublished mapper '$mapper': $!\n";
            print {$out} "0\n"
                or die "cannot write Lazy Thick $attribute guard for '$mapper': $!\n";
            close($out)
                or die "cannot close Lazy Thick $attribute guard for '$mapper': $!\n";
        };
        $write_error = $@;
        open(my $in, '<', $queue)
            or die "cannot verify Lazy Thick $attribute guard for '$mapper': $!\n";
        my $value = <$in> // '';
        close($in);
        $value =~ s/^\s+|\s+$//g;
        die "Lazy Thick $attribute guard did not read back zero for '$mapper'"
            . ($write_error ne '' ? " after setter refusal: $write_error" : "\n")
            if $value ne '0';
        warn "setting Lazy Thick $attribute guard was refused, but the exact zero "
            . "postcondition is already proven; continuing without retry: $write_error"
            if $write_error ne '';
    }
    my ($after, $after_diskseq) =
        $class->_lazy_mapper_identity($scfg, $mapper, $uuid, $table);
    die "Lazy Thick mapper incarnation changed while installing discard guard\n"
        if $after ne $devno || $after_diskseq ne $diskseq;
    return $devno;
}

sub _lazy_verify_private_io_guard {
    my ($class, $scfg, $mapper, $uuid, $table) = @_;
    my ($devno, $diskseq) =
        $class->_lazy_mapper_identity($scfg, $mapper, $uuid, $table);
    for my $attribute (qw(discard_max_bytes write_zeroes_max_bytes)) {
        my $queue = "/sys/dev/block/$devno/queue/$attribute";
        die "Lazy Thick private I/O guard attribute '$attribute' is unavailable\n"
            if !-f $queue || -l $queue;
        open(my $in, '<', $queue)
            or die "cannot read Lazy Thick private $attribute: $!\n";
        my $value = <$in> // '';
        close($in);
        $value =~ s/^\s+|\s+$//g;
        die "Lazy Thick private guard verification found $attribute=$value instead of zero\n"
            if $value ne '0';
    }
    my ($after, $after_diskseq) =
        $class->_lazy_mapper_identity($scfg, $mapper, $uuid, $table);
    die "Lazy Thick private mapper incarnation changed during verify-only guard proof\n"
        if $after ne $devno || $after_diskseq ne $diskseq;
    return $devno;
}

sub _lazy_verify_public_io_guard {
    my ($class, $scfg, $mapper, $uuid, $table) = @_;
    my ($devno, $diskseq) =
        $class->_lazy_mapper_identity($scfg, $mapper, $uuid, $table);
    for my $attribute (qw(
        discard_max_bytes discard_max_hw_bytes write_zeroes_max_bytes
    )) {
        my $queue = "/sys/dev/block/$devno/queue/$attribute";
        die "Lazy Thick public I/O guard attribute '$attribute' is unavailable\n"
            if !-f $queue || -l $queue;
        open(my $in, '<', $queue)
            or die "cannot read Lazy Thick public $attribute: $!\n";
        my $value = <$in> // '';
        close($in);
        $value =~ s/^\s+|\s+$//g;
        die "Lazy Thick public frontend was born with $attribute=$value instead of zero\n"
            if $value ne '0';
    }
    my ($after, $after_diskseq) =
        $class->_lazy_mapper_identity($scfg, $mapper, $uuid, $table);
    die "Lazy Thick public mapper incarnation changed during I/O guard proof\n"
        if $after ne $devno || $after_diskseq ne $diskseq;
    return $devno;
}

sub _lazy_front_pivot_state {
    my ($class, $scfg, $mapper, $uuid, $clone_table, $linear_table) = @_;
    my $deadline = $class->_thick_command_deadline($scfg);
    my $info = _command_lines(
        ['/usr/bin/timeout', '--foreground', '--kill-after=5s', "${deadline}s",
            '/sbin/dmsetup', 'info', '-c', '--noheadings', '--separator', '|',
            '-o', 'uuid,major,minor,suspended,tables_loaded', $mapper],
        "reading Lazy Thick pivot frontend '$mapper' identity failed",
    );
    die "Lazy Thick pivot frontend '$mapper' identity is ambiguous\n" if @$info != 1;
    my ($actual_uuid, $major, $minor, $suspended, $tables_loaded) =
        split(/\|/, $info->[0], -1);
    for ($actual_uuid, $major, $minor, $suspended, $tables_loaded) {
        s/^\s+|\s+$//g;
    }
    die "Lazy Thick pivot frontend '$mapper' UUID mismatch\n"
        if $actual_uuid ne $uuid;
    $class->_thick_verify_mapper_node_ready($scfg, $mapper, $major, $minor);
    my $live = _command_lines(
        ['/usr/bin/timeout', '--foreground', '--kill-after=5s', "${deadline}s",
            '/sbin/dmsetup', 'table', $mapper],
        "reading Lazy Thick pivot frontend '$mapper' live table failed",
    );
    die "Lazy Thick pivot frontend '$mapper' live table is ambiguous\n"
        if @$live != 1;
    my $is_suspended = lc($suspended) eq 'suspended';
    die "Lazy Thick pivot frontend '$mapper' has invalid suspend state\n"
        if !$is_suspended && lc($suspended) ne 'active';
    if (!$is_suspended && $live->[0] eq $linear_table
        && $tables_loaded eq 'Live') {
        return ('LINEAR_ACTIVE', "$major:$minor");
    }
    if (!$is_suspended && $live->[0] eq $clone_table) {
        return ('CLONE_ACTIVE', "$major:$minor") if $tables_loaded eq 'Live';
        if ($tables_loaded eq 'Both') {
            my $inactive = _command_lines(
                ['/usr/bin/timeout', '--foreground', '--kill-after=5s', "${deadline}s",
                    '/sbin/dmsetup', 'table', '--inactive', $mapper],
                "reading Lazy Thick pivot frontend '$mapper' inactive table failed",
            );
            return ('CLONE_ACTIVE_LINEAR_PENDING', "$major:$minor")
                if @$inactive == 1 && $inactive->[0] eq $linear_table;
        }
    }
    if ($is_suspended && $live->[0] eq $clone_table
        && $tables_loaded eq 'Both') {
        my $inactive = _command_lines(
            ['/usr/bin/timeout', '--foreground', '--kill-after=5s', "${deadline}s",
                '/sbin/dmsetup', 'table', '--inactive', $mapper],
            "reading Lazy Thick pivot frontend '$mapper' inactive table failed",
        );
        return ('CLONE_SUSPENDED_LINEAR_PENDING', "$major:$minor")
            if @$inactive == 1 && $inactive->[0] eq $linear_table;
    }
    die "Lazy Thick pivot frontend '$mapper' is outside the exact recoverable graph states\n";
}

my %lazy_move_allocation;

sub _lazy_move_context {
    my ($class, $storeid, $scfg, $vmid, $volname, $stage, $frames) = @_;
    my @names = (__PACKAGE__ . ($stage eq 'allocate' ? '::alloc_image' : '::activate_volume'),
        $stage eq 'allocate' ? 'PVE::Storage::vdisk_alloc' : 'PVE::Storage::activate_volumes',
        'PVE::QemuServer::clone_disk', 'PVE::AbstractConfig::lock_config_full',
        'PVE::AbstractConfig::lock_config');
    my (@rows, $previous);
    for my $name (@names) {
        my @indices = grep { ($frames->[$_]->{sub} // '') eq $name } 0 .. $#$frames;
        die "Lazy move requires exact upstream caller '$name'\n"
            if @indices != 1 || (defined($previous) && $indices[0] <= $previous);
        $previous = $indices[0];
        push @rows, $frames->[$indices[0]];
    }
    my ($hook, $storage, $clone, $full_lock, $lock) = map { $_->{args} } @rows;
    die "Lazy move clone callsite is unqualified\n"
        if ($rows[2]->{package} // '') ne 'PVE::API2::Qemu'
        || ($rows[2]->{file} // '') ne '/usr/share/perl5/PVE/API2/Qemu.pm';
    die "Lazy move clone signature is unqualified\n"
        if @$clone != 9 || ref($clone->[0]) ne 'HASH' || ref($clone->[1]) ne 'HASH'
        || ref($clone->[2]) ne 'HASH' || ($clone->[3] // '') ne '1'
        || ref($clone->[4]) ne 'ARRAY' || defined($clone->[5]) || defined($clone->[6]);
    my ($storecfg, $source, $dest, undef, $newvols) = @$clone;
    my $drive = $source->{drive};
    die "Lazy move requires stopped same-VM ordinary-disk full copy\n"
        if ($source->{vmid} // '') ne "$vmid" || ($dest->{vmid} // '') ne "$vmid"
        || $source->{running} || defined($source->{snapname}) || ref($drive) ne 'HASH'
        || ($source->{drivename} // '') !~ /^(?:scsi|virtio|sata|ide)[0-9]+$/
        || ($dest->{drivename} // '') ne $source->{drivename}
        || ($dest->{storage} // '') ne $storeid || ($dest->{format} // '') ne 'raw'
        || defined($dest->{efisize}) || ($drive->{media} // '') eq 'cdrom'
        || ($drive->{file} // '') !~ /^([A-Za-z][A-Za-z0-9_.-]*):vm-\Q$vmid\E-disk-[0-9]+$/
        || ($drive->{discard} // 'ignore') ne 'ignore'
        || ($drive->{detect_zeroes} // '0') ne '0'
        || ($source->{size} // '') !~ /^[1-9][0-9]*$/;
    my ($source_sid, $source_vol) = split(/:/, $drive->{file}, 2);
    die "Lazy move source and destination storage must differ\n" if $source_sid eq $storeid;
    die "Lazy move requires exact held VM config lock\n"
        if @$lock != 3 || $lock->[0] ne 'PVE::QemuConfig' || $lock->[1] ne "$vmid"
        || ref($lock->[2]) ne 'CODE' || @$full_lock != 4
        || $full_lock->[0] ne $lock->[0] || $full_lock->[1] ne $lock->[1]
        || $full_lock->[2] ne '10' || $full_lock->[3] ne $lock->[2];
    die "Lazy move storage configuration identity changed\n"
        if ref($storecfg->{ids}) ne 'HASH' || ($storecfg->{ids}->{$storeid} // '') ne $scfg
        || $storage->[0] ne $storecfg || @$hook != 7 || $hook->[0] ne __PACKAGE__
        || $hook->[1] ne $storeid || $hook->[2] ne $scfg;
    if ($stage eq 'allocate') {
        die "Lazy move allocation arguments changed\n"
            if @$storage != 6 || $storage->[1] ne $storeid || $storage->[2] ne "$vmid"
            || $storage->[3] ne 'raw' || defined($storage->[4])
            || $storage->[5] * 1024 != $source->{size}
            || $hook->[3] ne "$vmid" || $hook->[4] ne 'raw' || defined($hook->[5])
            || $hook->[6] != $storage->[5] || @$newvols || defined($dest->{volid});
    } else {
        die "Lazy move target activation arguments changed\n"
            if @$storage != 2 || ref($storage->[1]) ne 'ARRAY' || @{$storage->[1]} != 1
            || $storage->[1]->[0] ne "$storeid:$volname" || $hook->[3] ne $volname
            || defined($hook->[4]) || defined($hook->[6])
            || ($dest->{volid} // '') ne "$storeid:$volname"
            || @$newvols != 1 || $newvols->[0] ne "$storeid:$volname";
    }
    return { source => $source, dest => $dest, newvols => $newvols, lock => $lock->[2],
        source_sid => $source_sid, source_vol => $source_vol, vmid => "$vmid",
        slot => $source->{drivename}, bytes => $source->{size} };
}

sub _lazy_move_source_policy {
    my ($class, $context) = @_;
    my $node = $class->_thin_local_node();
    die "Lazy move node identity is ambiguous\n" if $node !~ /^[A-Za-z0-9][A-Za-z0-9_.-]*$/;
    my $path = "/etc/pve/nodes/$node/qemu-server/$context->{vmid}.conf";
    my $refs = $class->_thick_pve_reference_files($context->{source_sid}, $context->{source_vol});
    my $text = $class->_thick_owner_reference_text({ path => $path, vmid => $context->{vmid} }, $refs);
    die "Lazy move source config has snapshots, pending or special sections\n" if $text =~ /^\[/m;
    $class->_thick_validate_destroy_config($context->{source_sid}, $context->{source_vol}, $text, {});
    my $volid = "$context->{source_sid}:$context->{source_vol}";
    my @matches = grep { /\Q$volid\E(?:,|\s|$)/ } split(/\n/, $text);
    die "Lazy move cannot prove exact source slot policy\n"
        if @matches != 1 || $matches[0] !~ /^\Q$context->{slot}\E:\s*\Q$volid\E(?:,|\s|$)/
        || ($matches[0] =~ /(?:^|,)discard=([^,\s]+)/ && $1 ne 'ignore')
        || ($matches[0] =~ /(?:^|,)detect_zeroes=([^,\s]+)/ && $1 ne '0');
    return sha256_hex($text);
}

sub _lazy_prepare_move_allocation {
    my ($class, $storeid, $scfg, $vmid) = @_;
    my $frames = $class->_thick_destroy_call_frames();
    return undef if !grep { ($_->{sub} // '') eq 'PVE::QemuServer::clone_disk' } @$frames;
    my $context = $class->_lazy_move_context($storeid, $scfg, $vmid, undef, 'allocate', $frames);
    $context->{config_digest} = $class->_lazy_move_source_policy($context);
    return $context;
}

sub _lazy_efi_allocation_requires_eager {
    my ($class) = @_;
    my $frames = $class->_thick_destroy_call_frames();
    my @efi = grep {
        ($_->{sub} // '') eq 'PVE::QemuServer::OVMF::create_efidisk'
    } @$frames;
    return 0 if !@efi;
    die "Lazy EFI allocation caller is ambiguous\n" if @efi != 1;
    die "Lazy EFI allocation callsite is unqualified\n"
        if ($efi[0]->{package} // '') ne 'PVE::API2::Qemu'
        || ($efi[0]->{file} // '') ne '/usr/share/perl5/PVE/API2/Qemu.pm';
    return 1;
}

sub _lazy_record_move_allocation {
    my ($class, $storeid, $volname, $context, $state) = @_;
    return if !$context;
    die "Lazy move publication is not the exact fresh dormant allocation\n"
        if $state->{phase} ne 'LAZY_DORMANT' || $state->{publication} != 1
        || $state->{generation} != 0 || $state->{bytes} != $context->{bytes};
    my $now = $class->_thick_progress_clock();
    for my $key (keys %lazy_move_allocation) {
        delete $lazy_move_allocation{$key}
            if $lazy_move_allocation{$key}->{pid} != $$ || $lazy_move_allocation{$key}->{expires} < $now;
    }
    die "Lazy move capability budget exceeded\n" if keys(%lazy_move_allocation) >= 16;
    my $key = "$storeid:$volname";
    die "Lazy move capability already exists\n" if exists($lazy_move_allocation{$key});
    $lazy_move_allocation{$key} = { %$context, pid => $$, expires => $now + 120,
        anchor_digest => sha256_hex(join('|', map { "$_=$state->{$_}" } sort keys %$state)) };
    return;
}

sub _lazy_recheck_move_capability {
    my ($class, $storeid, $scfg, $volname, $state, $cap) = @_;
    die "Lazy move capability is absent, expired or from another process\n"
        if !$cap || $cap->{pid} != $$ || $class->_thick_progress_clock() > $cap->{expires};
    die "Lazy move target allocation identity changed\n"
        if sha256_hex(join('|', map { "$_=$state->{$_}" } sort keys %$state)) ne $cap->{anchor_digest};
    die "Lazy move target is already referenced\n"
        if @{$class->_thick_pve_reference_files($storeid, $volname)};
    my $current = $class->_lazy_move_context($storeid, $scfg, $cap->{vmid}, $volname,
        'activate', $class->_thick_destroy_call_frames());
    for my $field (qw(source dest newvols lock source_sid source_vol vmid slot bytes)) {
        die "Lazy move capability context changed at '$field'\n" if $current->{$field} ne $cap->{$field};
    }
    die "Lazy move source configuration changed\n"
        if $class->_lazy_move_source_policy($current) ne $cap->{config_digest};
    return $cap;
}

sub _lazy_consume_move_allocation {
    my ($class, $storeid, $scfg, $volname, $state) = @_;
    # Consumed before validation/effects: even a failed attempt cannot replay.
    my $cap = delete $lazy_move_allocation{"$storeid:$volname"};
    return $class->_lazy_recheck_move_capability($storeid, $scfg, $volname, $state, $cap);
}

sub _lazy_verify_guest_discard_config {
    my ($class, $storeid, $volname, $scfg, $state) = @_;
    my $volid = "$storeid:$volname";
    my $matched = 0;
    for my $file (@{$class->_thick_pve_reference_files($storeid, $volname)}) {
        open(my $fh, '<', $file) or die "reading PVE disk policy '$file' failed: $!\n";
        while (my $line = <$fh>) {
            next if $line !~ /\Q$volid\E(?:,|\s|$)/;
            $matched++;
            die "Lazy Thick activation refuses non-ignore discard policy for '$volid' in '$file'\n"
                if $line =~ /(?:^|,)discard=([^,\s]+)/ && $1 ne 'ignore';
            die "Lazy Thick activation refuses nonzero detect_zeroes policy for '$volid' in '$file'\n"
                if $line =~ /(?:^|,)detect_zeroes=([^,\s]+)/ && $1 ne '0';
        }
        close($fh) or die "closing PVE disk policy '$file' failed: $!\n";
    }
    return $class->_lazy_consume_move_allocation($storeid, $scfg, $volname, $state)
        if !$matched && $scfg && $state;
    die "Lazy Thick activation cannot prove an exact PVE disk policy for '$volid'\n"
        if $matched != 1;
    return 1;
}

sub _thick_list_images {
    my ($class, $storeid, $scfg, $vmid, $vollist, $cache) = @_;
    $class->_require_thick_identity_config($storeid, $scfg);
    my $vg = $scfg->{'slt-vgname'};
    my $device = "/dev/mapper/$scfg->{'slt-expected-wwid'}";
    my $lvs = $class->_thick_list_volumes_scoped($scfg, $vg, $device);
    my $res = [];
    return $res if !$lvs->{$vg};
    for my $anchor (sort grep { /^sltg-a-[0-9a-f]{24}$/ } keys %{$lvs->{$vg}}) {
        my $state = decode_anchor_tags($lvs->{$vg}->{$anchor}->{tags} // '');
        next if $state->{sid} ne $storeid;
        my (undef, $name, $owner) = $class->parse_volname($state->{vol});
        next if defined($vmid) && $owner != $vmid;
        my $expected_anchor = anchor_name($class->_thick_namespace($scfg), $name);
        die "thick-generations anchor name mismatch for '$vg/$anchor'\n"
            if $anchor ne $expected_anchor;
        my $lazy = "$state->{v}" eq '6';
        if ($lazy) {
            die "Lazy Thick object '$vg/$anchor' is not publishable\n"
                if $state->{phase} eq 'LAZY_PREPARED'
                || $state->{phase} eq 'LAZY_CLAIMED';
        } else {
            die "thick-generations object '$vg/$anchor' requires recovery\n"
                if $state->{phase} ne 'MATERIALIZED'
                && $state->{phase} ne 'HYDRATING'
                && $state->{phase} ne 'HYDRATION_COMPLETE'
                && $state->{phase} ne 'LINEAR_PIVOTED';
        }
        my (undef, $head) = $class->_thick_read_anchor(
            $storeid, $scfg, $name, $lvs,
        );
        my $volid = "$storeid:$name";
        next if $vollist && !grep { $_ eq $volid } @$vollist;
        push @$res, {
            volid => $volid, format => 'raw', size => $head->{lv_size},
            vmid => $owner, ctime => $head->{ctime},
        };
    }
    return $res;
}

sub _thick_verify_frontend {
    my ($class, $scfg, $volname, $head_name, $expected_sectors, $expected_runtime,
        $node_policy) = @_;
    $expected_runtime //= 'active';
    $node_policy //= 'ready';
    die "invalid expected Thick frontend runtime state\n"
        if $expected_runtime ne 'active' && $expected_runtime ne 'suspended';
    die "invalid Thick frontend node policy\n"
        if $node_policy ne 'ready' && $node_policy ne 'kernel-only';
    my $vg = $scfg->{'slt-vgname'};
    my $namespace = $class->_thick_namespace($scfg);
    my $mapper = mapper_name($namespace, $volname);
    my $uuid = 'SLT-TG2-' . object_key($namespace, $volname);
    my $command_timeout = $class->_thick_command_deadline($scfg);
    my $info = _command_lines(
        ['/usr/bin/timeout', '--foreground', '--kill-after=5s', "${command_timeout}s",
            '/sbin/dmsetup', 'info', '-c', '--noheadings', '--separator', '|',
            '-o', 'uuid,readonly,major,minor,suspended', $mapper],
        "reading thick-generations frontend '$mapper' failed",
    );
    die "thick-generations frontend '$mapper' identity is ambiguous\n" if @$info != 1;
    my ($actual_uuid, $readonly, $major, $minor, $suspended) = split(/\|/, $info->[0], -1);
    for ($actual_uuid, $readonly, $major, $minor, $suspended) { s/^\s+|\s+$//g; }
    die "thick-generations frontend '$mapper' UUID mismatch\n" if $actual_uuid ne $uuid;
    die "thick-generations frontend '$mapper' is unexpectedly read-only\n"
        if lc($readonly) ne 'writeable';
    die "thick-generations frontend '$mapper' runtime state mismatch\n"
        if lc($suspended) ne $expected_runtime;
    $class->_thick_verify_mapper_node_ready($scfg, $mapper, $major, $minor)
        if $node_policy eq 'ready';
    my $table = _command_lines(
        ['/usr/bin/timeout', '--foreground', '--kill-after=5s', "${command_timeout}s",
            '/sbin/dmsetup', 'table', $mapper],
        "reading thick-generations frontend table '$mapper' failed",
    );
    die "thick-generations frontend '$mapper' table is not one linear segment\n"
        if @$table != 1 || $table->[0] !~ /^0\s+(\d+)\s+linear\s+(\d+:\d+)\s+0$/;
    my ($actual_sectors, $actual_head_devno) =
        $table->[0] =~ /^0\s+(\d+)\s+linear\s+(\d+:\d+)\s+0$/;
    die "thick-generations frontend '$mapper' size mismatch\n"
        if defined($expected_sectors) && $actual_sectors != $expected_sectors;
    my $wwid = $scfg->{'slt-expected-wwid'};
    die "thick-generations frontend verification requires an expected WWID\n"
        if !defined($wwid) || $wwid !~ /^[0-9A-Fa-f]+$/;
    my $device = "/dev/mapper/$wwid";
    my $expected_head_devno = $class->_thick_verify_active_lv_identity(
        $scfg, $vg, $head_name, $device, $command_timeout,
        $node_policy eq 'ready' ? 1 : 0,
    );
    die "thick-generations frontend '$mapper' backing device mismatch\n"
        if $actual_head_devno ne $expected_head_devno;
    my $deps = _command_lines(
        ['/usr/bin/timeout', '--foreground', '--kill-after=5s', "${command_timeout}s",
            '/sbin/dmsetup', 'deps', '-o', 'devname', $mapper],
        "reading thick-generations frontend dependencies '$mapper' failed",
    );
    my $head_dm = $vg;
    $head_dm =~ s/-/--/g;
    my $escaped_head = $head_name;
    $escaped_head =~ s/-/--/g;
    my $expected = "$head_dm-$escaped_head";
    die "thick-generations frontend '$mapper' does not depend only on '$vg/$head_name'\n"
        if @$deps != 1 || $deps->[0] !~ /^1\s+dependencies\s*:\s*\(\Q$expected\E\)$/;
    return 1;
}

sub _thick_block_node_devno {
    my ($class, $scfg, $path, $description) = @_;
    die "invalid Thick block-device path\n"
        if !defined($path)
        || $path !~ m{^/dev/(?:mapper/[A-Za-z0-9+_.-]+|[A-Za-z0-9+_.-]+/[A-Za-z0-9+_.-]+)$};
    $description //= "Thick block-device node '$path'";
    my $command_timeout = $class->_thick_command_deadline($scfg);
    my $lines = _command_lines(
        ['/usr/bin/timeout', '--foreground', '--kill-after=5s', "${command_timeout}s",
            '/usr/bin/stat', '-Lc', '%f|%t|%T', $path],
        "reading $description identity failed",
    );
    die "$description identity is ambiguous\n"
        if @$lines != 1 || $lines->[0] !~ /^([0-9a-fA-F]+)\|([0-9a-fA-F]+)\|([0-9a-fA-F]+)$/;
    my ($mode, $major, $minor) = (hex($1), hex($2), hex($3));
    die "$description is not a block device\n" if ($mode & 0170000) != 0060000;
    return "$major:$minor";
}

sub _thick_verify_mapper_node_ready {
    my ($class, $scfg, $mapper, $major, $minor) = @_;
    die "invalid managed Thick mapper name\n"
        if !defined($mapper) || $mapper !~ /^[A-Za-z0-9+_.-]+$/;
    die "invalid managed Thick mapper kernel device number\n"
        if !defined($major) || $major !~ /^\d+$/ || !defined($minor) || $minor !~ /^\d+$/;
    my $actual = $class->_thick_block_node_devno(
        $scfg, "/dev/mapper/$mapper", "managed Thick mapper '$mapper' node",
    );
    die "managed Thick mapper '$mapper' node does not match kernel device $major:$minor\n"
        if $actual ne "$major:$minor";
    return 1;
}

sub _thick_mapper_name_present {
    my ($class, $scfg, $mapper) = @_;
    die "invalid managed Thick mapper name\n"
        if !defined($mapper) || $mapper !~ /^[A-Za-z0-9+_.-]+$/;
    my $inventory = _dm_kernel_inventory($class->_thick_command_deadline($scfg));
    return exists($inventory->{$mapper}) ? 1 : 0;
}

sub _thick_managed_mapper_present {
    my ($class, $scfg, $mapper, $expected_uuid, $description) = @_;
    die "invalid managed Thick mapper name\n"
        if !defined($mapper) || $mapper !~ /^[A-Za-z0-9+_.-]+$/;
    die "invalid managed Thick mapper UUID\n"
        if !defined($expected_uuid) || $expected_uuid !~ /^SLT-TG[23]-[A-Za-z0-9-]+$/;
    $description //= "managed Thick mapper '$mapper'";
    # `/dev/mapper/$mapper` is a udev-created userspace node, not the
    # authoritative kernel mapping.  During PVE migration cleanup udev may
    # remove that node before the source deactivate callback while the exact
    # zero-open DM device still exists.  Inventory kernel DM state directly so
    # the dependency is removed before lvchange -an reaches the backing LV.
    my $inventory = _dm_kernel_inventory($class->_thick_command_deadline($scfg));
    return 0 if !exists($inventory->{$mapper});
    die "$description kernel UUID mismatch\n"
        if $inventory->{$mapper} ne $expected_uuid;
    return 1;
}

sub _thick_frontend_present {
    my ($class, $scfg, $volname) = @_;
    my $namespace = $class->_thick_namespace($scfg);
    my $mapper = mapper_name($namespace, $volname);
    my $expected_uuid = 'SLT-TG2-' . object_key($namespace, $volname);
    return $class->_thick_managed_mapper_present(
        $scfg, $mapper, $expected_uuid, "thick-generations frontend '$mapper'",
    );
}

sub _dm_kernel_inventory {
    my ($command_timeout) = @_;
    my @command = ('/sbin/dmsetup', 'info', '-c', '--noheadings', '--separator', '|',
        '-o', 'name,uuid');
    if (defined($command_timeout)) {
        die "invalid kernel device-mapper inventory command deadline\n"
            if $command_timeout !~ /^\d+$/ || $command_timeout < 5 || $command_timeout > 600;
        unshift @command,
            '/usr/bin/timeout', '--foreground', '--kill-after=5s', "${command_timeout}s";
    }
    my $lines = _command_lines(
        \@command,
        'reading authoritative kernel device-mapper inventory failed',
    );
    my $inventory = {};
    for my $line (@$lines) {
        die "kernel device-mapper inventory contains a malformed row\n"
            if $line !~ /^[^|]+\|[^|]*$/;
        my ($name, $uuid) = split(/\|/, $line, 2);
        for ($name, $uuid) {
            $_ //= '';
            s/^\s+|\s+$//g;
        }
        die "kernel device-mapper inventory contains a malformed row\n"
            if $name eq '';
        die "kernel device-mapper inventory contains duplicate name '$name'\n"
            if exists($inventory->{$name});
        # UUID is optional for unrelated DM users. Preserve empty UUIDs as
        # present devices; exact managed-object identity gates still reject
        # them. An unrelated UUID-less map must not disable all plugin I/O.
        $inventory->{$name} = $uuid;
    }
    return $inventory;
}

sub _thick_verify_mapper_absent {
    my ($class, $scfg, $mapper, $errmsg) = @_;
    die "invalid Thick mapper absence identity\n"
        if !defined($mapper) || $mapper !~ /^[A-Za-z0-9+_.-]+$/;
    my $inventory = _dm_kernel_inventory($class->_thick_command_deadline($scfg));
    die "$errmsg\n" if exists($inventory->{$mapper});
    return 1;
}

sub _thick_source_mapper_name {
    my ($class, $scfg, $volname, $generation) = @_;
    return mapper_name($class->_thick_namespace($scfg), $volname)
        . sprintf('-src-%08d', $generation);
}

sub _thick_verify_source_mapper {
    my ($class, $scfg, $mapper, $source, $sectors, $tx) = @_;
    my $vg = $scfg->{'slt-vgname'};
    my $command_timeout = $class->_thick_command_deadline($scfg);
    my $info = _command_lines(
        ['/usr/bin/timeout', '--foreground', '--kill-after=5s', "${command_timeout}s",
            '/sbin/dmsetup', 'info', '-c', '--noheadings', '--separator', '|',
            '-o', 'uuid,readonly,major,minor,suspended', $mapper],
        "reading thick-generations source mapper '$mapper' failed",
    );
    die "thick-generations source mapper '$mapper' identity is ambiguous\n"
        if @$info != 1;
    my ($uuid, $readonly, $major, $minor, $suspended) = split(/\|/, $info->[0], -1);
    for ($uuid, $readonly, $major, $minor, $suspended) { s/^\s+|\s+$//g; }
    die "thick-generations source mapper '$mapper' UUID mismatch\n"
        if $uuid ne "SLT-TG3-SOURCE-$tx";
    die "thick-generations source mapper '$mapper' is not read-only\n"
        if lc($readonly) ne 'read-only';
    die "thick-generations source mapper '$mapper' is not live\n"
        if lc($suspended) ne 'active';
    $class->_thick_verify_mapper_node_ready($scfg, $mapper, $major, $minor);
    my $table = _command_lines(
        ['/usr/bin/timeout', '--foreground', '--kill-after=5s', "${command_timeout}s",
            '/sbin/dmsetup', 'table', $mapper],
        "reading thick-generations source mapper '$mapper' table failed",
    );
    die "thick-generations source mapper '$mapper' table mismatch\n"
        if @$table != 1
        || $table->[0] !~ /^0\s+\Q$sectors\E\s+linear\s+(\d+:\d+)\s+0$/;
    my $actual_source_devno = $1;
    my $wwid = $scfg->{'slt-expected-wwid'};
    die "thick-generations source verification requires an expected WWID\n"
        if !defined($wwid) || $wwid !~ /^[0-9A-Fa-f]+$/;
    my $device = "/dev/mapper/$wwid";
    my $expected_source_devno = $class->_thick_verify_active_lv_identity(
        $scfg, $vg, $source, $device, $command_timeout,
    );
    die "thick-generations source mapper '$mapper' backing device mismatch\n"
        if $actual_source_devno ne $expected_source_devno;
    my $deps = _command_lines(
        ['/usr/bin/timeout', '--foreground', '--kill-after=5s', "${command_timeout}s",
            '/sbin/dmsetup', 'deps', '-o', 'devname', $mapper],
        "reading thick-generations source mapper '$mapper' dependencies failed",
    );
    my $vg_dm = $vg;
    $vg_dm =~ s/-/--/g;
    my $source_dm = $source;
    $source_dm =~ s/-/--/g;
    my $expected = "$vg_dm-$source_dm";
    die "thick-generations source mapper '$mapper' dependency mismatch\n"
        if @$deps != 1 || $deps->[0] !~ /^1\s+dependencies\s*:\s*\(\Q$expected\E\)$/;
    return "$major:$minor";
}

sub _thick_mapper_is_suspended {
    my ($class, $mapper, $command_timeout) = @_;
    $command_timeout //= 30;
    die "invalid thick-generations suspend-state command deadline\n"
        if $command_timeout !~ /^\d+$/ || $command_timeout < 5 || $command_timeout > 600;
    my $state = _command_lines(
        ['/usr/bin/timeout', '--foreground', '--kill-after=5s', "${command_timeout}s",
            '/sbin/dmsetup', 'info', '-c', '--noheadings', '-o', 'suspended', $mapper],
        "reading device-mapper suspend state for '$mapper' failed",
    );
    die "device-mapper suspend state for '$mapper' is ambiguous\n" if @$state != 1;
    my $value = $state->[0];
    $value =~ s/^\s+|\s+$//g;
    return 0 if $value eq 'Active';
    return 1 if $value eq 'Suspended';
    die "device-mapper suspend state for '$mapper' is unknown: '$value'\n";
}

sub _thick_verify_active_lv_identity {
    my ($class, $scfg, $vg, $lv, $device, $command_timeout, $require_node) = @_;
    $require_node //= 1;
    die "active Thick LV identity requires storage configuration\n"
        if ref($scfg) ne 'HASH';
    die "active Thick LV identity requires a valid VG/LV name\n"
        if !defined($vg) || $vg !~ /^[A-Za-z0-9+_.-]+$/
        || !defined($lv) || $lv !~ /^[A-Za-z0-9+_.-]+$/;
    die "active Thick LV identity requires an exact mapper device\n"
        if !defined($device) || $device !~ m{^/dev/mapper/[0-9A-Fa-f]+$};
    die "invalid active Thick LV identity command deadline\n"
        if !defined($command_timeout) || $command_timeout !~ /^\d+$/
        || $command_timeout < 5 || $command_timeout > 600;

    my $identity = _command_lines(
        ['/usr/bin/timeout', '--foreground', '--kill-after=5s', "${command_timeout}s",
            '/sbin/lvs', '--readonly', '--devices', $device, '--noheadings',
            '--separator', '|', '-o', 'vg_uuid,lv_uuid,lv_name', "$vg/$lv"],
        "reading active Thick LV identity of '$vg/$lv' failed",
    );
    die "active Thick LV identity of '$vg/$lv' is ambiguous\n" if @$identity != 1;
    my ($vg_uuid, $lv_uuid, $actual_name) = split(/\|/, $identity->[0], -1);
    for ($vg_uuid, $lv_uuid, $actual_name) {
        $_ //= '';
        s/^\s+|\s+$//g;
    }
    die "active Thick LV identity returned unexpected object '$actual_name'\n"
        if $actual_name ne $lv;
    for ($vg_uuid, $lv_uuid) {
        die "active Thick LV identity contains an invalid UUID\n"
            if !/^[A-Za-z0-9-]+$/;
        s/-//g;
    }

    my $vg_dm = $vg;
    my $lv_dm = $lv;
    $vg_dm =~ s/-/--/g;
    $lv_dm =~ s/-/--/g;
    my $kernel = _command_lines(
        ['/usr/bin/timeout', '--foreground', '--kill-after=5s', "${command_timeout}s",
            '/sbin/dmsetup', 'info', '-c', '--noheadings', '--separator', '|',
            '-o', 'uuid,major,minor,suspended', "$vg_dm-$lv_dm"],
        "reading kernel identity of active Thick LV '$vg/$lv' failed",
    );
    die "kernel identity of active Thick LV '$vg/$lv' is ambiguous\n" if @$kernel != 1;
    my ($actual_uuid, $major, $minor, $suspended) = split(/\|/, $kernel->[0], -1);
    for ($actual_uuid, $major, $minor, $suspended) {
        $_ //= '';
        s/^\s+|\s+$//g;
    }
    my $expected_uuid = "LVM-$vg_uuid$lv_uuid";
    die "kernel identity of active Thick LV '$vg/$lv' does not match its scoped LVM UUID\n"
        if $actual_uuid ne $expected_uuid;
    die "active Thick LV '$vg/$lv' is not live\n" if lc($suspended) ne 'active';
    die "active Thick LV '$vg/$lv' has an invalid kernel device number\n"
        if $major !~ /^\d+$/ || $minor !~ /^\d+$/;
    my $devno = "$major:$minor";
    if ($require_node) {
        my $node_devno = $class->_thick_block_node_devno(
            $scfg, "/dev/$vg/$lv", "active Thick LV '$vg/$lv' node",
        );
        die "active Thick LV '$vg/$lv' node does not match kernel device $devno\n"
            if $node_devno ne $devno;
    }
    return $devno;
}

sub _thick_activate_exact_lvs {
    my ($class, $scfg, $vg, $device, $errmsg, @lvs) = @_;
    die "exact Thick activation requires at least one LV\n" if !@lvs;
    my %seen;
    for my $lv (@lvs) {
        die "exact Thick activation contains an invalid or duplicate LV name\n"
            if !defined($lv) || $lv !~ /^[A-Za-z0-9+_.-]+$/ || $seen{$lv}++;
    }
    my $command_timeout = $class->_thick_command_deadline($scfg);
    my $command_error = '';
    eval { run_command(
        ['/usr/bin/timeout', '--foreground', '--kill-after=5s', "${command_timeout}s",
            '/sbin/lvchange', '--devices', $device, '--activationmode', 'complete',
            '-ay', '-K', map { "$vg/$_" } @lvs],
        errmsg => $errmsg,
    ); };
    $command_error = $@ if $@;

    my $verify_error = '';
    eval {
        $class->_thick_verify_active_lv_identity(
            $scfg, $vg, $_, $device, $command_timeout,
        ) for @lvs;
    };
    $verify_error = $@ if $@;
    if ($verify_error ne '') {
        die $command_error ne ''
            ? "$errmsg; activation result is UNKNOWN because complete exact identity is unproven: "
                . "$command_error$verify_error"
            : $verify_error;
    }
    warn "$errmsg reported an error, but every complete exact LV identity is proven; "
        . "continuing without retry: $command_error" if $command_error ne '';
    return 1;
}

sub _thick_lv_mapper_name {
    my ($vg, $lv) = @_;
    my $vg_dm = $vg;
    my $lv_dm = $lv;
    $vg_dm =~ s/-/--/g;
    $lv_dm =~ s/-/--/g;
    return "$vg_dm-$lv_dm";
}

sub _thick_deactivate_exact_lvs {
    my ($class, $scfg, $vg, $device, $errmsg, @lvs) = @_;
    die "exact Thick deactivation requires at least one LV\n" if !@lvs;
    my %seen;
    for my $lv (@lvs) {
        die "exact Thick deactivation contains an invalid or duplicate LV name\n"
            if !defined($lv) || $lv !~ /^[A-Za-z0-9+_.-]+$/ || $seen{$lv}++;
    }

    my $command_timeout = $class->_thick_command_deadline($scfg);
    my $before = _dm_kernel_inventory($command_timeout);
    for my $lv (@lvs) {
        my $mapper = _thick_lv_mapper_name($vg, $lv);
        $class->_thick_verify_active_lv_identity(
            $scfg, $vg, $lv, $device, $command_timeout, 0,
        )
            if exists($before->{$mapper});
    }
    my $command_error = '';
    eval { run_command(
        ['/usr/bin/timeout', '--foreground', '--kill-after=5s', "${command_timeout}s",
            '/sbin/lvchange', '--devices', $device, '-an', map { "$vg/$_" } @lvs],
        errmsg => $errmsg,
    ); };
    $command_error = $@ if $@;

    my ($after, $verify_error);
    eval {
        $after = _dm_kernel_inventory($command_timeout);
        for my $lv (@lvs) {
            my $mapper = _thick_lv_mapper_name($vg, $lv);
            die "exact Thick LV '$vg/$lv' remains active after deactivation\n"
                if exists($after->{$mapper});
        }
    };
    $verify_error = $@ if $@;
    if ($verify_error) {
        die $command_error ne ''
            ? "$errmsg; deactivation result is UNKNOWN because exact kernel absence "
                . "is unproven; no retry attempted: $command_error$verify_error"
            : $verify_error;
    }
    warn "$errmsg reported an error, but exact kernel absence is proven; "
        . "continuing without retry: $command_error" if $command_error ne '';
    return 1;
}

sub _thick_remove_exact_lv {
    my ($class, $scfg, $vg, $device, $lv, $errmsg) = @_;
    die "exact Thick LV removal requires a valid LV name\n"
        if !defined($lv) || $lv !~ /^[A-Za-z0-9+_.-]+$/;
    die "exact Thick LV removal requires an exact mapper device\n"
        if !defined($device) || $device !~ m{^/dev/mapper/[0-9A-Fa-f]+$};
    my $command_timeout = $class->_thick_command_deadline($scfg);
    my $command_error = '';
    eval { run_command(
        ['/usr/bin/timeout', '--foreground', '--kill-after=5s', "${command_timeout}s",
            '/sbin/lvremove', '--devices', $device, '-f', "$vg/$lv"],
        errmsg => $errmsg,
    ); };
    $command_error = $@ if $@;

    my ($after, $post_error);
    eval {
        $after = $class->_thick_list_volumes_scoped($scfg, $vg, $device);
        die "exact Thick LV removal cannot confirm VG '$vg'\n" if !$after->{$vg};
        die "exact Thick LV '$vg/$lv' still exists after removal\n"
            if exists($after->{$vg}->{$lv});
    };
    $post_error = $@ if $@;
    if ($post_error) {
        die $command_error ne ''
            ? "$errmsg; removal result is UNKNOWN because exact absence is unproven; "
                . "no retry attempted: $command_error$post_error"
            : $post_error;
    }
    warn "$errmsg reported an error, but exact LV absence is proven; "
        . "continuing without retry: $command_error" if $command_error ne '';
    return $after;
}

# Intentionally inert production hook. Qualification drivers may locally
# override this method to terminate only their own disposable worker at an
# exact persisted crash boundary. No configuration or environment variable can
# enable fault injection in the packaged plugin.
sub _thick_fault_point {
    return;
}

sub _thick_transition_anchor {
    my ($class, $scfg, $vg, $anchor, $state, %change) = @_;
    my $device = delete $change{_device};
    my $old = PVE::SharedLvmThinThick::anchor_tags(%$state);
    my %next = (%$state, %change);
    validate_anchor_transition($state, \%next);
    my $new = PVE::SharedLvmThinThick::anchor_tags(%next);
    $class->_change_exact_tags($scfg,
        $vg, $anchor, $old, $new,
        "advancing thick-generations anchor '$vg/$anchor' failed",
        $device,
    );
    return \%next;
}

sub _thick_verify_clone_status {
    my ($class, $mapper, $must_be_complete, $expected_sectors, $expected_region,
        $command_timeout, $observed_status) = @_;
    die "invalid thick-generations command observation timeout\n"
        if !defined($command_timeout) || $command_timeout !~ /^\d+$/
        || $command_timeout < 5 || $command_timeout > 600;
    die "dm-clone status verification for '$mapper' has no authoritative sector count\n"
        if !defined($expected_sectors) || $expected_sectors !~ /^\d+$/ || !$expected_sectors;
    die "dm-clone status verification for '$mapper' has no authoritative region size\n"
        if !defined($expected_region) || $expected_region !~ /^\d+$/ || !$expected_region;
    my $status;
    if (defined($observed_status)) {
        die "dm-clone status for '$mapper' is ambiguous\n"
            if ref($observed_status) || $observed_status =~ /[\r\n]/;
        $status = $observed_status;
    } else {
        my $lines = _command_lines(
            ['/usr/bin/timeout', '--foreground', '--kill-after=5s', "${command_timeout}s",
                '/sbin/dmsetup', 'status', '--noflush', $mapper],
            "reading dm-clone status for '$mapper' failed",
        );
        die "dm-clone status for '$mapper' is ambiguous\n" if @$lines != 1;
        $status = $lines->[0];
    }
    die "dm-clone frontend '$mapper' entered the kernel Fail metadata state; automatic hydration or pivot is unsafe\n"
        if $status =~ /^0\s+\d+\s+clone\s+Fail\s*$/i;
    my ($status_sectors, $metadata_block, $metadata_used, $metadata_total,
        $region, $hydrated, $total, $hydrating) =
        $status =~ /^0\s+(\d+)\s+clone\s+(\d+)\s+(\d+)\/(\d+)\s+(\d+)\s+(\d+)\/(\d+)\s+(\d+)(?:\s|$)/;
    die "dm-clone status for '$mapper' is malformed\n"
        if !defined($status_sectors) || !defined($metadata_block)
        || !defined($metadata_used) || !defined($metadata_total)
        || !defined($region) || !defined($hydrated) || !defined($total)
        || !defined($hydrating);
    die "dm-clone status counters for '$mapper' are internally impossible\n"
        if !$metadata_block || !$metadata_total || !$total
        || $metadata_used > $metadata_total
        || $hydrated > $total || $hydrating > $total - $hydrated;
    die "dm-clone status geometry for '$mapper' does not match the signed transition\n"
        if $status_sectors != $expected_sectors || $region != $expected_region
        || $total != int(($expected_sectors + $expected_region - 1) / $expected_region);
    my ($metadata_mode) = $status =~ /\s+(rw|ro)\s*$/i;
    die "dm-clone status for '$mapper' does not report an authoritative metadata mode\n"
        if !defined($metadata_mode);
    die "dm-clone frontend '$mapper' metadata is read-only; automatic hydration or pivot is unsafe\n"
        if lc($metadata_mode) ne 'rw';
    die "dm-clone hydration for '$mapper' is incomplete ($hydrated/$total, $hydrating active)\n"
        if $must_be_complete && ($hydrated != $total || $hydrating != 0);
    return (int($hydrated), int($total), int($hydrating));
}

sub _thick_set_background_hydration {
    my ($class, $mapper, $command_timeout, $expected_sectors, $expected_region,
        $enabled) = @_;
    die "invalid thick-generations command observation timeout\n"
        if !defined($command_timeout) || $command_timeout !~ /^\d+$/
        || $command_timeout < 5 || $command_timeout > 600;
    die "invalid requested dm-clone hydration state\n"
        if !defined($enabled) || $enabled !~ /^(?:0|1)$/;
    my $message = $enabled ? 'enable_hydration' : 'disable_hydration';
    my $description = $enabled ? 'enabling' : 'disabling';
    my $command_error = '';
    eval {
        run_command(
            ['/usr/bin/timeout', '--foreground', '--kill-after=5s',
                "${command_timeout}s", '/sbin/dmsetup', 'message', $mapper,
                '0', $message],
            errmsg => "$description dm-clone background hydration failed",
        );
    };
    $command_error = $@ if $@;

    # Read exactly once after the message and validate that same observation
    # both structurally and semantically.  A successful dmsetup client exit is
    # not proof that the kernel accepted the requested state change.
    my $verify_error = '';
    eval {
        my $lines = _command_lines(
            ['/usr/bin/timeout', '--foreground', '--kill-after=5s',
                "${command_timeout}s", '/sbin/dmsetup', 'status', '--noflush',
                $mapper],
            "confirming dm-clone background hydration state failed",
        );
        die "dm-clone status for '$mapper' is ambiguous after $message\n"
            if @$lines != 1;
        my $status = $lines->[0];
        $class->_thick_verify_clone_status(
            $mapper, 0, $expected_sectors, $expected_region, $command_timeout,
            $status,
        );
        my $disabled = $status =~ /(?:^|\s)no_hydration(?:\s|$)/ ? 1 : 0;
        die "dm-clone '$mapper' did not confirm enabled background hydration\n"
            if $enabled && $disabled;
        die "dm-clone '$mapper' did not confirm disabled background hydration\n"
            if !$enabled && !$disabled;
    };
    $verify_error = $@ if $@;
    if ($verify_error ne '') {
        die $command_error ne ''
            ? "$description dm-clone background hydration result is UNKNOWN because "
                . "the exact kernel status is unproven; no retry attempted: "
                . "$command_error$verify_error"
            : $verify_error;
    }
    warn "$description dm-clone background hydration reported an error, but the "
        . "exact kernel postcondition is proven; continuing without retry: "
        . $command_error if $command_error ne '';
    return 1;
}

sub _thick_disable_background_hydration {
    my ($class, $mapper, $command_timeout, $expected_sectors, $expected_region) = @_;
    return $class->_thick_set_background_hydration(
        $mapper, $command_timeout, $expected_sectors, $expected_region, 0,
    );
}

sub _thick_enable_background_hydration {
    my ($class, $mapper, $command_timeout, $expected_sectors, $expected_region) = @_;
    return $class->_thick_set_background_hydration(
        $mapper, $command_timeout, $expected_sectors, $expected_region, 1,
    );
}

sub _thick_fail_stalled_hydration {
    my ($class, $mapper, $timeout, $command_timeout, $expected_sectors,
        $expected_region) = @_;
    my $disable_error = '';
    eval {
        $class->_thick_disable_background_hydration(
            $mapper, $command_timeout, $expected_sectors, $expected_region,
        );
    };
    $disable_error = $@ if $@;
    if ($disable_error ne '') {
        die "dm-clone hydration for '$mapper' made no verified progress for ${timeout}s; "
            . "background hydration state is unknown and requires operator recovery: "
            . $disable_error;
    }
    die "dm-clone hydration for '$mapper' made no verified progress for ${timeout}s; "
        . "background hydration is confirmed disabled and the persistent transition "
        . "requires explicit recovery\n";
}

sub _thick_verify_transition_metadata {
    my ($class, $storeid, $scfg, $volname, $info, %expected) = @_;
    my $device = delete $expected{device};
    die "transition metadata inventory is missing\n" if ref($info) ne 'HASH';
    validate_transition_tags(
        $info->{tags} // '', sid => $storeid, vol => $volname,
        tx => $expected{tx}, kind => 'metadata',
        generation => $expected{generation}, region => $expected{region},
    );
    die "transition metadata size is unknown or smaller than planned\n"
        if !defined($info->{lv_size}) || $info->{lv_size} !~ /^\d+$/
        || $info->{lv_size} < $expected{metadata_bytes};
    $class->_thick_verify_autoactivation_disabled(
        $scfg, $scfg->{'slt-vgname'}, $expected{name}, $device,
    );
    return 1;
}

sub _thick_verify_clone_frontend {
    my ($class, $scfg, $volname, %expected) = @_;
    my $expected_runtime = delete($expected{runtime}) // 'active';
    die "invalid expected dm-clone frontend runtime state\n"
        if $expected_runtime ne 'active' && $expected_runtime ne 'suspended';
    my ($threshold, $batch) = $class->_thick_hydration_tuning(
        $scfg, $expected{region},
    );
    my $namespace = $class->_thick_namespace($scfg);
    my $mapper = mapper_name($namespace, $volname);
    my $uuid = 'SLT-TG2-' . object_key($namespace, $volname);
    my $command_timeout = $class->_thick_command_deadline($scfg);
    my $info = _command_lines(
        ['/usr/bin/timeout', '--foreground', '--kill-after=5s', "${command_timeout}s",
            '/sbin/dmsetup', 'info', '-c', '--noheadings', '--separator', '|',
            '-o', 'uuid,readonly,major,minor,suspended', $mapper],
        "reading dm-clone frontend '$mapper' failed",
    );
    die "dm-clone frontend '$mapper' identity is ambiguous\n" if @$info != 1;
    my ($actual_uuid, $readonly, $major, $minor, $suspended) = split(/\|/, $info->[0], -1);
    for ($actual_uuid, $readonly, $major, $minor, $suspended) { s/^\s+|\s+$//g; }
    die "dm-clone frontend '$mapper' UUID mismatch\n" if $actual_uuid ne $uuid;
    die "dm-clone frontend '$mapper' is unexpectedly read-only\n"
        if lc($readonly) ne 'writeable';
    die "dm-clone frontend '$mapper' runtime state mismatch\n"
        if lc($suspended) ne $expected_runtime;
    $class->_thick_verify_mapper_node_ready($scfg, $mapper, $major, $minor);

    my $table = _command_lines(
        ['/usr/bin/timeout', '--foreground', '--kill-after=5s', "${command_timeout}s",
            '/sbin/dmsetup', 'table', $mapper],
        "reading dm-clone frontend table '$mapper' failed",
    );
    die "dm-clone frontend '$mapper' table mismatch\n"
        if @$table != 1
        || $table->[0] !~ /^0\s+\Q$expected{sectors}\E\s+clone\s+(\S+)\s+(\S+)\s+(\S+)\s+\Q$expected{region}\E\s+2\s+no_hydration\s+no_discard_passdown\s+4\s+hydration_threshold\s+\Q$threshold\E\s+hydration_batch_size\s+\Q$batch\E$/;
    my ($table_meta, $table_destination, $table_source) = ($1, $2, $3);
    my $vg = $scfg->{'slt-vgname'};
    die "dm-clone frontend verification lacks transaction source identity\n"
        if !defined($expected{source}) || !defined($expected{tx});
    my $wwid = $scfg->{'slt-expected-wwid'};
    die "dm-clone frontend verification requires an expected WWID\n"
        if !defined($wwid) || $wwid !~ /^[0-9A-Fa-f]+$/;
    my $device = "/dev/mapper/$wwid";
    my @expected_roles = (
        $class->_thick_verify_active_lv_identity(
            $scfg, $vg, $expected{meta}, $device, $command_timeout,
        ),
        $class->_thick_verify_active_lv_identity(
            $scfg, $vg, $expected{new}, $device, $command_timeout,
        ),
        $class->_thick_verify_source_mapper(
            $scfg, $expected{source_map}, $expected{source}, $expected{sectors},
            $expected{tx},
        ),
    );
    my @actual_roles = ($table_meta, $table_destination, $table_source);
    die "dm-clone frontend '$mapper' ordered device roles mismatch\n"
        if grep { $actual_roles[$_] ne $expected_roles[$_] } 0 .. 2;

    my $deps = _command_lines(
        ['/usr/bin/timeout', '--foreground', '--kill-after=5s', "${command_timeout}s",
            '/sbin/dmsetup', 'deps', '-o', 'devname', $mapper],
        "reading dm-clone frontend dependencies '$mapper' failed",
    );
    die "dm-clone frontend '$mapper' dependency report is ambiguous\n" if @$deps != 1;
    my @actual = sort($deps->[0] =~ /\(([^()]+)\)/g);
    my $vg_dm = $vg;
    $vg_dm =~ s/-/--/g;
    my @wanted;
    for my $name ($expected{meta}, $expected{new}) {
        my $escaped = $name;
        $escaped =~ s/-/--/g;
        push @wanted, "$vg_dm-$escaped";
    }
    push @wanted, $expected{source_map};
    @wanted = sort @wanted;
    die "dm-clone frontend '$mapper' dependency graph mismatch\n"
        if @actual != @wanted || grep { $actual[$_] ne $wanted[$_] } 0 .. $#wanted;
    return 1;
}

sub _thick_wait_for_hydration {
    my ($class, $mapper, $timeout, $command_timeout, $expected_sectors,
        $expected_region) = @_;
    die "invalid thick-generations hydration timeout\n"
        if !defined($timeout) || $timeout !~ /^\d+$/ || $timeout < 60 || $timeout > 86400;
    my ($hydrated, $total, $hydrating) = $class->_thick_verify_clone_status(
        $mapper, 0, $expected_sectors, $expected_region, $command_timeout,
    );
    return 1 if $hydrated == $total && $hydrating == 0;

    my $last_hydrated = $hydrated;
    my $last_total = $total;
    my $progress_at = $class->_thick_progress_clock();

    while (1) {
        my $events = _command_lines(
            ['/usr/bin/timeout', '--foreground', '--kill-after=5s', "${command_timeout}s",
                '/sbin/dmsetup', 'info', '-c', '--noheadings', '-o', 'events', $mapper],
            "reading dm-clone event counter for '$mapper' failed",
        );
        die "dm-clone event counter for '$mapper' is ambiguous\n"
            if @$events != 1 || $events->[0] !~ /^\d+$/;
        my $event = int($events->[0]);

        # Close the completion-before-wait race after capturing the event
        # number.  A changed total or regressing progress is ambiguous and
        # never resets the observation window.
        ($hydrated, $total, $hydrating) = $class->_thick_verify_clone_status(
            $mapper, 0, $expected_sectors, $expected_region, $command_timeout,
        );
        return 1 if $hydrated == $total && $hydrating == 0;
        die "dm-clone hydration geometry changed for '$mapper'\n"
            if $total != $last_total || $hydrated < $last_hydrated;
        if ($hydrated > $last_hydrated) {
            $last_hydrated = $hydrated;
            $progress_at = $class->_thick_progress_clock();
        }

        my $remaining = $timeout - int($class->_thick_progress_clock() - $progress_at);
        $class->_thick_fail_stalled_hydration(
            $mapper, $timeout, $command_timeout, $expected_sectors, $expected_region,
        ) if $remaining <= 0;
        my $slice = $remaining < 60 ? $remaining : 60;
        $slice = $command_timeout if $command_timeout < $slice;

        # A slice timeout is an observation boundary, not a retry of a storage
        # mutation.  Re-read exact kernel status and extend the window only
        # after the hydrated-region counter advances.
        my $wait_started = $class->_thick_progress_clock();
        my $wait_error = '';
        eval {
            run_command(
                ['/usr/bin/timeout', '--kill-after=5s', "${slice}s",
                    '/sbin/dmsetup', 'wait', $mapper, "$event"],
                errmsg => "waiting for dm-clone hydration event failed",
            );
        };
        $wait_error = $@ if $@;
        my $wait_elapsed = $class->_thick_progress_clock() - $wait_started;
        ($hydrated, $total, $hydrating) = $class->_thick_verify_clone_status(
            $mapper, 0, $expected_sectors, $expected_region, $command_timeout,
        );
        return 1 if $hydrated == $total && $hydrating == 0;
        die "dm-clone hydration geometry changed for '$mapper'\n"
            if $total != $last_total || $hydrated < $last_hydrated;
        if ($hydrated > $last_hydrated) {
            $last_hydrated = $hydrated;
            $progress_at = $class->_thick_progress_clock();
        } elsif ($wait_error && $slice > 1 && $wait_elapsed < 1) {
            die "dm-clone event wait for '$mapper' failed before the observation boundary: "
                . $wait_error;
        } elsif (!$wait_error && $wait_elapsed < 1) {
            # A stale/racing event number can make dmsetup wait return success
            # immediately without a new hydrated region. Re-read on the next
            # iteration, but yield first so repeated spurious wakeups cannot
            # turn one slow large-disk hydration into a CPU busy loop. This is
            # observation only and never retries a storage mutation.
            $class->_thick_observation_pause(250);
        } elsif ($class->_thick_progress_clock() - $progress_at >= $timeout) {
            $class->_thick_fail_stalled_hydration(
                $mapper, $timeout, $command_timeout, $expected_sectors, $expected_region,
            );
        }
    }
}

sub _thick_progress_clock {
    # Wall-clock corrections must neither expire a healthy long hydration
    # early nor keep a stalled clone alive past its no-progress interval.
    return Time::HiRes::clock_gettime(Time::HiRes::CLOCK_MONOTONIC());
}

sub _thick_observation_pause {
    my ($class, $milliseconds) = @_;
    die "invalid Thick observation pause\n"
        if !defined($milliseconds) || $milliseconds !~ /^\d+$/
        || $milliseconds < 1 || $milliseconds > 1000;
    select(undef, undef, undef, $milliseconds / 1000);
    return 1;
}

sub _thick_frontend_open_count {
    my ($class, $mapper, $command_timeout) = @_;
    die "invalid thick-generations open-count command deadline\n"
        if !defined($command_timeout) || $command_timeout !~ /^\d+$/
        || $command_timeout < 5 || $command_timeout > 600;
    my $lines = _command_lines(
        ['/usr/bin/timeout', '--foreground', '--kill-after=5s', "${command_timeout}s",
            '/sbin/dmsetup', 'info', '-c', '--noheadings', '-o', 'open', $mapper],
        "reading thick-generations frontend open count '$mapper' failed",
    );
    die "thick-generations frontend '$mapper' open count is ambiguous\n"
        if @$lines != 1 || $lines->[0] !~ /^\s*\d+\s*$/;
    return int($lines->[0]);
}

sub _thick_verify_created_lv_exact {
    my ($class, $scfg, $vg, $lv, $device, $minimum_bytes, $verify_tags) = @_;
    die "invalid Thick created-LV VG or name\n"
        if !defined($vg) || !defined($lv)
        || $vg !~ /^[A-Za-z0-9+_.-]+$/ || $lv !~ /^[A-Za-z0-9+_.-]+$/;
    die "invalid Thick created-LV minimum size\n"
        if !defined($minimum_bytes) || $minimum_bytes !~ /^\d+$/ || !$minimum_bytes;
    die "missing Thick created-LV tag verifier\n" if ref($verify_tags) ne 'CODE';
    my $lvs = $class->_thick_list_volumes_scoped($scfg, $vg, $device);
    my $info = $lvs->{$vg} && $lvs->{$vg}->{$lv};
    die "created Thick LV '$vg/$lv' is missing\n" if ref($info) ne 'HASH';
    die "created Thick LV '$vg/$lv' is undersized\n"
        if !defined($info->{lv_size}) || $info->{lv_size} !~ /^\d+$/
        || $info->{lv_size} < $minimum_bytes;
    $verify_tags->($info->{tags} // '');
    $class->_thick_verify_autoactivation_disabled($scfg, $vg, $lv, $device);
    return $info;
}

sub _thick_create_lv_exact {
    my ($class, $scfg, $device, $command, $errmsg, $verify) = @_;
    die "invalid Thick LV create mapper device\n"
        if !defined($device) || $device !~ m{^/dev/mapper/[0-9A-Fa-f]+$};
    die "invalid Thick LV create command\n"
        if ref($command) ne 'ARRAY' || @$command < 4
        || $command->[0] ne '/sbin/lvcreate';
    my @device_options = grep { $command->[$_] eq '--devices' } 0 .. $#$command;
    die "invalid Thick LV create device scope\n"
        if @device_options != 1 || $device_options[0] == $#$command
        || $command->[$device_options[0] + 1] ne $device;
    die "invalid Thick LV create verifier\n" if ref($verify) ne 'CODE';

    my $command_timeout = $class->_thick_command_deadline($scfg);
    my @bounded = ('/usr/bin/timeout', '--foreground', '--kill-after=5s',
        "${command_timeout}s", @$command);
    my $command_error = '';
    eval { run_command(\@bounded, errmsg => $errmsg); };
    $command_error = $@ if $@;

    my $verify_error = '';
    eval { $verify->(); };
    $verify_error = $@ if $@;
    if ($verify_error ne '') {
        die $command_error ne ''
            ? "$errmsg; creation result is UNKNOWN because the exact signed "
                . "postcondition is unproven; no retry attempted: "
                . "$command_error$verify_error"
            : "$errmsg; exact signed creation postcondition failed: $verify_error";
    }
    warn "$errmsg reported an error, but the exact signed creation postcondition "
        . "is proven; continuing without retry: $command_error"
        if $command_error ne '';
    return 1;
}

sub _thick_create_mapper_exact {
    my ($class, $scfg, $command, $errmsg, $verify) = @_;
    die "invalid Thick mapper create command\n"
        if ref($command) ne 'ARRAY' || @$command < 4
        || $command->[0] ne '/sbin/dmsetup'
        || !grep { defined($_) && $_ eq 'create' } @$command;
    die "invalid Thick mapper create verifier\n" if ref($verify) ne 'CODE';

    my $command_timeout = $class->_thick_command_deadline($scfg);
    my @bounded = ('/usr/bin/timeout', '--foreground', '--kill-after=5s',
        "${command_timeout}s", @$command);
    my $command_error = '';
    eval { run_command(\@bounded, errmsg => $errmsg); };
    $command_error = $@ if $@;

    my $verify_error = '';
    eval { $verify->(); };
    $verify_error = $@ if $@;
    if ($verify_error ne '') {
        die $command_error ne ''
            ? "$errmsg; create result is UNKNOWN because its exact postcondition is unproven: "
                . "$command_error$verify_error"
            : $verify_error;
    }
    warn "$errmsg reported an error, but the complete exact mapper postcondition is proven; "
        . "continuing without retry: $command_error" if $command_error ne '';
    return 1;
}

sub _thick_remove_mapper_exact {
    my ($class, $scfg, $command, $errmsg, $verify_absent) = @_;
    die "invalid Thick mapper remove command\n"
        if ref($command) ne 'ARRAY' || @$command < 3
        || $command->[0] ne '/sbin/dmsetup'
        || !grep { defined($_) && $_ eq 'remove' } @$command;
    die "invalid Thick mapper removal verifier\n" if ref($verify_absent) ne 'CODE';

    my $command_timeout = $class->_thick_command_deadline($scfg);
    my @bounded = ('/usr/bin/timeout', '--foreground', '--kill-after=5s',
        "${command_timeout}s", @$command);
    my $command_error = '';
    eval { run_command(\@bounded, errmsg => $errmsg); };
    $command_error = $@ if $@;

    my $verify_error = '';
    eval { $verify_absent->(); };
    $verify_error = $@ if $@;
    if ($verify_error ne '') {
        die $command_error ne ''
            ? "$errmsg; removal result is UNKNOWN because exact absence is unproven: "
                . "$command_error$verify_error"
            : $verify_error;
    }
    warn "$errmsg reported an error, but exact mapper absence is proven; "
        . "continuing without retry: $command_error" if $command_error ne '';
    return 1;
}

sub _thick_load_inactive_table_exact {
    my ($class, $scfg, $operation, $mapper, $table, $verify, $errmsg) = @_;
    die "invalid Thick inactive-table operation\n"
        if !defined($operation) || $operation !~ /^(?:load|reload)$/;
    die "invalid managed Thick mapper name\n"
        if !defined($mapper) || $mapper !~ /^[A-Za-z0-9+_.-]+$/;
    die "invalid Thick inactive table\n"
        if !defined($table) || $table eq '' || $table =~ /[\r\n\0]/;
    die "missing Thick inactive-table verifier\n" if ref($verify) ne 'CODE';
    $errmsg //= "loading inactive table for managed Thick mapper '$mapper' failed";
    my $command_timeout = $class->_thick_command_deadline($scfg);
    my $command_error = '';
    eval {
        run_command(
            ['/usr/bin/timeout', '--foreground', '--kill-after=5s',
                "${command_timeout}s", '/sbin/dmsetup', '--verifyudev',
                $operation, $mapper, '--table', $table],
            errmsg => $errmsg,
        );
    };
    $command_error = $@ if $@;

    my $inactive;
    my $verify_error = '';
    eval {
        $inactive = _command_lines(
            ['/usr/bin/timeout', '--foreground', '--kill-after=5s',
                "${command_timeout}s", '/sbin/dmsetup', 'table', '--inactive',
                $mapper],
            "reading inactive table for managed Thick mapper '$mapper' failed",
        );
        $verify->($inactive);
    };
    $verify_error = $@ if $@;
    if ($verify_error ne '') {
        die $command_error ne ''
            ? "$errmsg; result is UNKNOWN because the exact inactive-table "
                . "postcondition is unproven; no retry attempted: "
                . "$command_error$verify_error"
            : "$errmsg; exact inactive-table postcondition failed: $verify_error";
    }
    warn "$errmsg reported an error, but the exact inactive-table postcondition "
        . "is proven; continuing without retry: $command_error"
        if $command_error ne '';
    return $inactive;
}

sub _thick_suspend_mapper_exact {
    my ($class, $scfg, $mapper, $errmsg) = @_;
    die "invalid managed Thick mapper name\n"
        if !defined($mapper) || $mapper !~ /^[A-Za-z0-9+_.-]+$/;
    $errmsg //= "suspending managed Thick mapper '$mapper' failed";
    my $command_timeout = $class->_thick_command_deadline($scfg);
    my $command_error = '';
    eval {
        run_command(
            ['/usr/bin/timeout', '--foreground', '--kill-after=5s',
                "${command_timeout}s", '/sbin/dmsetup', '--verifyudev',
                'suspend', $mapper],
            errmsg => $errmsg,
        );
    };
    $command_error = $@ if $@;

    my $verify_error = '';
    eval {
        die "managed Thick mapper '$mapper' remains active after suspend\n"
            if !$class->_thick_mapper_is_suspended($mapper, $command_timeout);
    };
    $verify_error = $@ if $@;
    if ($verify_error ne '') {
        die $command_error ne ''
            ? "$errmsg; result is UNKNOWN because the exact suspended postcondition "
                . "is unproven; no retry attempted: $command_error$verify_error"
            : "$errmsg; exact suspended postcondition failed: $verify_error";
    }
    warn "$errmsg reported an error, but the exact suspended postcondition is "
        . "proven; continuing without retry: $command_error" if $command_error ne '';
    return 1;
}

sub _thick_resume_mapper_exact {
    my ($class, $scfg, $mapper, $verify, $errmsg) = @_;
    die "invalid managed Thick mapper name\n"
        if !defined($mapper) || $mapper !~ /^[A-Za-z0-9+_.-]+$/;
    die "missing Thick mapper resume verifier\n" if ref($verify) ne 'CODE';
    $errmsg //= "resuming managed Thick mapper '$mapper' failed";
    my $command_timeout = $class->_thick_command_deadline($scfg);
    my $command_error = '';
    eval {
        run_command(
            ['/usr/bin/timeout', '--foreground', '--kill-after=5s',
                "${command_timeout}s", '/sbin/dmsetup', '--verifyudev',
                'resume', $mapper],
            errmsg => $errmsg,
        );
    };
    $command_error = $@ if $@;

    my $verify_error = '';
    eval {
        die "managed Thick mapper '$mapper' remains suspended after resume\n"
            if $class->_thick_mapper_is_suspended($mapper, $command_timeout);
        $verify->();
    };
    $verify_error = $@ if $@;
    if ($verify_error ne '') {
        die $command_error ne ''
            ? "$errmsg; result is UNKNOWN because the exact live postcondition "
                . "is unproven; no retry attempted: $command_error$verify_error"
            : "$errmsg; exact live postcondition failed: $verify_error";
    }
    warn "$errmsg reported an error, but the exact live postcondition is proven; "
        . "continuing without retry: $command_error" if $command_error ne '';
    return 1;
}

sub _thick_schedule_materialization {
    my ($class, $scfg, $storeid, $volname, $snap, $operation, $tx, $timeout) = @_;
    for my $arg ($storeid, $volname) {
        die "invalid thick-generations worker argument\n"
            if !defined($arg) || $arg !~ /^[A-Za-z0-9][A-Za-z0-9_.+-]*$/;
    }
    die "invalid thick-generations worker transaction UUID\n"
        if !defined($tx) || $tx !~ /^[0-9a-f]{32}$/;
    die "invalid thick-generations worker operation\n"
        if !defined($operation) || $operation ne 'SNAPSHOT';
    $snap = _thick_snapshot_name($snap);
    die "invalid thick-generations hydration timeout\n"
        if !defined($timeout) || $timeout !~ /^\d+$/ || $timeout < 60 || $timeout > 86400;

    my $unit = "pve-sharedlvmthin-tg-$tx";
    my $worker = '/usr/libexec/pve-sharedlvmthin/sharedlvmthin-thick-materialize';
    my $command_timeout = $class->_thick_command_deadline($scfg);
    my $schedule_error = '';
    eval {
        run_command(
            ['/usr/bin/timeout', '--foreground', '--kill-after=5s',
                "${command_timeout}s", '/usr/bin/systemd-run', '--quiet', '--collect',
                "--unit=$unit", '--on-active=3s',
                '--timer-property=AccuracySec=100ms', '--property=Type=exec',
                '--property=Restart=no', '--property=Nice=10',
                '--property=IOSchedulingClass=best-effort',
                '--property=IOSchedulingPriority=7',
                '--property=TimeoutStartSec=infinity',
                $worker, $storeid, $volname, $snap, $operation, $tx],
            errmsg => "scheduling asynchronous Thick Generations materialization failed",
        );
    };
    $schedule_error = $@ if $@;

    my $verify_error = '';
    eval {
        my %units;
        for my $suffix ('service', 'timer') {
            my $lines = _command_lines(
                ['/usr/bin/timeout', '--foreground', '--kill-after=5s',
                    "${command_timeout}s", '/usr/bin/systemctl', 'show',
                    "$unit.$suffix",
                    '--property=Id,LoadState,ActiveState,Transient,ExecStart,Triggers'],
                "reading scheduled Thick Generations $suffix identity failed",
            );
            my %properties;
            for my $line (@$lines) {
                my ($key, $value) = split(/=/, $line, 2);
                die "ambiguous scheduled Thick Generations $suffix property report\n"
                    if !defined($value) || exists($properties{$key});
                $properties{$key} = $value;
            }
            $units{$suffix} = \%properties;
        }
        my $service = $units{service};
        my $timer = $units{timer};
        die "scheduled Thick Generations service identity mismatch\n"
            if ($service->{Id} // '') ne "$unit.service"
            || ($service->{LoadState} // '') ne 'loaded'
            || ($service->{Transient} // '') ne 'yes';
        my $argv = join(' ', $worker, $storeid, $volname, $snap, $operation, $tx);
        die "scheduled Thick Generations service command mismatch\n"
            if ($service->{ExecStart} // '')
            !~ /(?:^|[;{]\s*)path=\Q$worker\E\s*;\s*argv\[\]=\Q$argv\E(?:\s*;|$)/;
        die "scheduled Thick Generations timer identity mismatch\n"
            if ($timer->{Id} // '') ne "$unit.timer"
            || ($timer->{LoadState} // '') ne 'loaded'
            || ($timer->{Transient} // '') ne 'yes'
            || ($timer->{Triggers} // '') ne "$unit.service";
        my $timer_running = ($timer->{ActiveState} // '') =~ /^(?:active|activating)$/;
        my $service_running = ($service->{ActiveState} // '')
            =~ /^(?:active|activating|reloading)$/;
        die "scheduled Thick Generations worker is neither queued nor running\n"
            if !$timer_running && !$service_running;
    };
    $verify_error = $@ if $@;
    if ($verify_error ne '') {
        die $schedule_error ne ''
            ? "asynchronous Thick Generations scheduling result is UNKNOWN; exact "
                . "service/timer identity is unproven; no retry attempted: "
                . "$schedule_error$verify_error"
            : "asynchronous Thick Generations scheduling postcondition failed: "
                . $verify_error;
    }
    warn "systemd-run reported an error, but the exact transaction service/timer "
        . "is proven queued or running; continuing without retry: $schedule_error"
        if $schedule_error ne '';
    return $unit;
}

sub _thick_snapshot_name {
    my ($snap) = @_;
    die "snapshot name is missing\n" if !defined($snap) || $snap eq '';
    die "snapshot name contains characters unsafe for persistent LVM metadata\n"
        if $snap !~ /^[A-Za-z0-9_.+-]+$/;
    return $snap;
}

sub _thick_activate_volume {
    my ($class, $storeid, $scfg, $volname, $snapname, $cache) = @_;
    $class->_require_thick_identity_config($storeid, $scfg);
    my $device = "/dev/mapper/$scfg->{'slt-expected-wwid'}";
    $class->_verify_mutation_quorum($storeid, $scfg);
    $class->_verify_storage_identity($storeid, $scfg, $device);
    my $lvs = $class->_thick_list_volumes_scoped($scfg, $scfg->{'slt-vgname'}, $device);
    if (defined($snapname)) {
        my ($snapshot) = $class->_thick_find_snapshot(
            $storeid, $scfg, $volname, $snapname, $lvs,
        );
        my $vg = $scfg->{'slt-vgname'};
        $class->_thick_verify_snapshot_readonly($scfg, $vg, $snapshot, $device);
        $class->_thick_verify_autoactivation_disabled($scfg, $vg, $snapshot, $device);
        $class->_thick_activate_exact_lvs(
            $scfg, $vg, $device,
            "activating thick-generations snapshot '$vg/$snapshot' failed",
            $snapshot,
        );
        $class->_thick_verify_snapshot_readonly($scfg, $vg, $snapshot, $device);
        return 1;
    }
    my ($state, undef, $anchor) =
        $class->_thick_read_anchor($storeid, $scfg, $volname, $lvs);
    my $vg = $scfg->{'slt-vgname'};
    my $namespace = $class->_thick_namespace($scfg);
    my $mapper = mapper_name($namespace, $volname);
    if ($state->{phase} ne 'MATERIALIZED') {
        # An asynchronous materializer may pivot the published clone frontend
        # while a later VM start is verifying it.  Serialize only this
        # transitional activation with the exact materializer.  After the
        # bounded wait, discard all pre-lock observations and classify the
        # anchor and runtime again.
        return $class->_with_thick_transition_executor_lock(
            $storeid, $scfg, $volname,
            sub {
                my ($locked_state) =
                    $class->_thick_read_anchor($storeid, $scfg, $volname);
                return $class->_thick_activate_volume(
                    $storeid, $scfg, $volname, $snapname, $cache,
                ) if $locked_state->{phase} eq 'MATERIALIZED';
                die "thick-generations volume '$storeid:$volname' is materializing and its exact frontend is missing; recovery required\n"
                    if !$class->_thick_frontend_present($scfg, $volname);
                $class->_thick_verify_published_transition_frontend(
                    $storeid, $scfg, $volname, $locked_state,
                );
                return 1;
            },
        );
    }
    if ($class->_thick_frontend_present($scfg, $volname)) {
        $class->_thick_verify_frontend($scfg, $volname, $state->{head});
        return 1;
    }
    $class->_thick_activate_exact_lvs(
        $scfg, $vg, $device,
        "activating thick-generations state for '$vg/$volname' failed",
        $state->{head}, $anchor,
    );
    $class->_thick_verify_autoactivation_disabled($scfg, $vg, $state->{head}, $device);
    my $sectors = _command_lines(
        ['/usr/bin/timeout', '--foreground', '--kill-after=5s',
            $class->_thick_command_deadline($scfg) . 's',
            '/sbin/blockdev', '--getsz', "/dev/$vg/$state->{head}"],
        "reading thick-generations head size '$vg/$state->{head}' failed",
    );
    die "thick-generations head size is ambiguous\n"
        if @$sectors != 1 || $sectors->[0] !~ /^\d+$/ || $sectors->[0] == 0;
    my $uuid = 'SLT-TG2-' . object_key($namespace, $volname);
    $class->_thick_create_mapper_exact(
        $scfg,
        ['/sbin/dmsetup', '--verifyudev', 'create', $mapper, '--uuid', $uuid,
            '--table', "0 $sectors->[0] linear /dev/$vg/$state->{head} 0"],
        "creating stable thick-generations frontend '$mapper' failed",
        sub { $class->_thick_verify_frontend(
            $scfg, $volname, $state->{head}, $sectors->[0],
        ) },
    );
    return 1;
}

sub _lazy_activate_volume {
    my ($class, $storeid, $scfg, $volname, @rest) = @_;
    return $class->_with_lazy_volume_executor_lock(
        $storeid, $volname,
        sub { $class->_lazy_activate_volume_locked(
            $storeid, $scfg, $volname, @rest,
        ) },
    );
}

sub _lazy_require_local_owner {
    my ($class, $storeid, $volname, $state, $node, $boot) = @_;
    my $owner_node = $state->{owner_node} // '';
    my $owner_boot = $state->{owner_boot} // '';

    if ($owner_node ne $node) {
        die "Unmaterialized Lazy Thick volume '$storeid:$volname' is owned by node "
            . "'$owner_node' and cannot be activated concurrently on '$node'. "
            . "For live migration, run 'sharedlvmthin thick-lazy-materialize "
            . "$storeid $volname' on the active owner while the VM is running, "
            . "verify completed materialization, then retry the migration. "
            . "No target activation effect was issued.\n";
    }
    if ($owner_boot ne $boot) {
        die "Unmaterialized Lazy Thick volume '$storeid:$volname' has a stale boot "
            . "epoch on local node '$node'. Do not materialize or reclaim it blindly; "
            . "stop and use the documented ownership recovery procedure. "
            . "No activation effect was issued.\n";
    }
    return 1;
}

sub _lazy_activate_volume_locked {
    my ($class, $storeid, $scfg, $volname, $snapname, $cache) = @_;
    $class->_require_thick_identity_config($storeid, $scfg);
    my $vg = $scfg->{'slt-vgname'};
    my $device = "/dev/mapper/$scfg->{'slt-expected-wwid'}";
    my ($initial_state) =
        $class->_thick_read_anchor($storeid, $scfg, $volname);
    return $class->_thick_activate_volume(
        $storeid, $scfg, $volname, $snapname, $cache,
    ) if $initial_state->{phase} eq 'MATERIALIZED'
        || ($initial_state->{op} // '') =~ /^(?:SNAPSHOT|ROLLBACK)$/;
    die "Lazy Thick snapshots are unavailable until explicit materialization\n"
        if defined($snapname);
    my $move_cap = $class->_lazy_verify_guest_discard_config($storeid, $volname, $scfg, $initial_state);
    $class->_assert_no_active_storage_worker($vg);
    my ($node, $boot) = $class->_lazy_local_identity();
    my $owner_epoch = $class->_new_transaction_id();
    my ($claimed, $anchor);

    $class->_with_vg_lock($storeid, $scfg, sub {
        $class->_require_no_vg_intent($scfg, $vg, $device);
        my ($state, undef, $anchor_name) =
            $class->_thick_read_anchor($storeid, $scfg, $volname);
        $class->_lazy_recheck_move_capability($storeid, $scfg, $volname, $state, $move_cap)
            if ref($move_cap) eq 'HASH';
        $anchor = $anchor_name;
        if ($state->{phase} eq 'LAZY_ACTIVE') {
            $class->_lazy_require_local_owner(
                $storeid, $volname, $state, $node, $boot,
            );
            $claimed = $state;
            return;
        }
        if ($state->{phase} eq 'LAZY_CLAIMED') {
            $class->_lazy_require_local_owner(
                $storeid, $volname, $state, $node, $boot,
            );
            $claimed = $state;
            return;
        }
        if ($state->{phase} eq 'MATERIALIZED') {
            $claimed = $state;
            return;
        }
        die "Lazy Thick activation requires LAZY_DORMANT; found '$state->{phase}'\n"
            if $state->{phase} ne 'LAZY_DORMANT';
        $claimed = $class->_thick_transition_anchor(
            $scfg, $vg, $anchor, $state,
            phase => 'LAZY_CLAIMED',
            publication => int($state->{publication}) + 1,
            owner_node => $node, owner_boot => $boot,
            owner_epoch => $owner_epoch, _device => $device,
        );
        return;
    }, $device);

    # Materialization deliberately converges to the ordinary Thick
    # Generations on-disk/runtime contract.  Keep the Lazy storage policy as
    # the provisioning choice, but use the mature linear lifecycle after the
    # irreversible verified pivot; rebuilding a clone graph is both invalid
    # (its metadata LV is gone) and unnecessary.
    return $class->_thick_activate_volume(
        $storeid, $scfg, $volname, $snapname, $cache,
    ) if $claimed->{phase} eq 'MATERIALIZED';

    my ($zero, $clone, $front) = $class->_lazy_runtime_names($scfg, $volname);
    my $sectors = int($claimed->{bytes} / 512);
    my ($threshold, $batch) = $class->_thick_hydration_tuning(
        $scfg, int($claimed->{region}),
    );
    my $zero_uuid = "SLT-TG6-ZERO-$claimed->{tx}";
    my $clone_uuid = "SLT-TG6-CLONE-$claimed->{tx}";
    my $front_uuid = 'SLT-TG2-' . object_key($class->_thick_namespace($scfg), $volname);
    my $zero_table = "0 $sectors zero";
    my ($clone_table, $front_table);

    eval {
        if ($claimed->{phase} eq 'LAZY_ACTIVE') {
            # An idempotent PVE activation is accepted only by proving the
            # complete already-published graph below; no object is recreated.
        } else {
            $class->_thick_activate_exact_lvs(
                $scfg, $vg, $device,
                "activating exact Lazy Thick backing objects failed",
                $claimed->{head}, $claimed->{metadata}, $anchor,
            );
            my $meta_devno = $class->_thick_verify_active_lv_identity(
                $scfg, $vg, $claimed->{metadata}, $device,
                $class->_thick_command_deadline($scfg),
            );
            my $data_devno = $class->_thick_verify_active_lv_identity(
                $scfg, $vg, $claimed->{head}, $device,
                $class->_thick_command_deadline($scfg),
            );
            $class->_thick_create_mapper_exact(
                $scfg,
                ['/sbin/dmsetup', '--verifyudev', 'create', $zero,
                    '--readonly', '--uuid', $zero_uuid, '--table', $zero_table],
                "creating private Lazy Thick zero source '$zero' failed",
                sub { $class->_lazy_mapper_identity(
                    $scfg, $zero, $zero_uuid, $zero_table,
                ) },
            );
            my $zero_devno = $class->_lazy_mapper_identity(
                $scfg, $zero, $zero_uuid, $zero_table,
            );
            $clone_table = "0 $sectors clone $meta_devno $data_devno $zero_devno "
                . "$claimed->{region} 2 no_hydration no_discard_passdown "
                . "4 hydration_threshold $threshold hydration_batch_size $batch";
            $class->_thick_create_mapper_exact(
                $scfg,
                ['/sbin/dmsetup', '--verifyudev', 'create', $clone,
                    '--uuid', $clone_uuid, '--table', $clone_table],
                "creating private guarded Lazy Thick clone '$clone' failed",
                sub { $class->_lazy_mapper_identity(
                    $scfg, $clone, $clone_uuid, $clone_table,
                ) },
            );
            my $clone_devno = $class->_lazy_install_discard_guard(
                $scfg, $clone, $clone_uuid, $clone_table,
            );
            $class->_thick_verify_clone_status(
                $clone, 0, $sectors, int($claimed->{region}),
                $class->_thick_command_deadline($scfg),
            );
            $front_table = "0 $sectors linear $clone_devno 0";
            $class->_thick_create_mapper_exact(
                $scfg,
                ['/sbin/dmsetup', '--verifyudev', 'create', $front,
                    '--uuid', $front_uuid, '--table', $front_table],
                "publishing stable Lazy Thick frontend '$front' failed",
                sub { $class->_lazy_mapper_identity(
                    $scfg, $front, $front_uuid, $front_table,
                ) },
            );
            $class->_lazy_verify_public_io_guard(
                $scfg, $front, $front_uuid, $front_table,
            );
        }

        my $zero_devno = $class->_lazy_mapper_identity(
            $scfg, $zero, $zero_uuid, $zero_table,
        );
        my $meta_devno = $class->_thick_verify_active_lv_identity(
            $scfg, $vg, $claimed->{metadata}, $device,
            $class->_thick_command_deadline($scfg),
        );
        my $data_devno = $class->_thick_verify_active_lv_identity(
            $scfg, $vg, $claimed->{head}, $device,
            $class->_thick_command_deadline($scfg),
        );
        $clone_table //= "0 $sectors clone $meta_devno $data_devno $zero_devno "
            . "$claimed->{region} 2 no_hydration no_discard_passdown "
            . "4 hydration_threshold $threshold hydration_batch_size $batch";
        my $clone_devno = $class->_lazy_verify_private_io_guard(
            $scfg, $clone, $clone_uuid, $clone_table,
        );
        $front_table //= "0 $sectors linear $clone_devno 0";
        $class->_lazy_verify_public_io_guard(
            $scfg, $front, $front_uuid, $front_table,
        );
        $class->_thick_verify_clone_status(
            $clone, 0, $sectors, int($claimed->{region}),
            $class->_thick_command_deadline($scfg),
        );
    };
    die "Lazy Thick activation outcome is UNKNOWN; persistent owner retained and no retry attempted: $@"
        if $@;

    if ($claimed->{phase} eq 'LAZY_CLAIMED') {
        $class->_with_vg_lock($storeid, $scfg, sub {
            my ($state) = $class->_thick_read_anchor($storeid, $scfg, $volname);
            die "Lazy Thick owner changed before runtime publication\n"
                if $state->{phase} ne 'LAZY_CLAIMED'
                || $state->{owner_node} ne $node || $state->{owner_boot} ne $boot
                || $state->{owner_epoch} ne $claimed->{owner_epoch};
            $class->_thick_transition_anchor(
                $scfg, $vg, $anchor, $state,
                phase => 'LAZY_ACTIVE', _device => $device,
            );
            return;
        }, $device);
    }
    return 1;
}

sub _thick_deactivate_volume {
    my ($class, $storeid, $scfg, $volname, $snapname, $cache) = @_;
    $class->_require_thick_identity_config($storeid, $scfg);
    my $device = "/dev/mapper/$scfg->{'slt-expected-wwid'}";
    $class->_verify_storage_identity($storeid, $scfg, $device);
    my $lvs = $class->_thick_list_volumes_scoped($scfg, $scfg->{'slt-vgname'}, $device);
    if (defined($snapname)) {
        my ($snapshot) = $class->_thick_find_snapshot(
            $storeid, $scfg, $volname, $snapname, $lvs,
        );
        my $vg = $scfg->{'slt-vgname'};
        $class->_thick_verify_snapshot_readonly($scfg, $vg, $snapshot, $device);
        $class->_thick_deactivate_exact_lvs(
            $scfg, $vg, $device,
            "deactivating thick-generations snapshot '$vg/$snapshot' failed",
            $snapshot,
        );
        return 1;
    }
    my ($state, undef, $anchor) =
        $class->_thick_read_anchor($storeid, $scfg, $volname, $lvs);
    my $vg = $scfg->{'slt-vgname'};
    my $mapper = mapper_name($class->_thick_namespace($scfg), $volname);
    if ($state->{phase} ne 'MATERIALIZED') {
        # Snapshot materialization may pivot the published frontend between
        # the anchor read above and exact table verification.  Serialize with
        # that one per-volume executor, then discard every pre-lock
        # observation.  This mirrors transitional activation and prevents a
        # normal guest stop from comparing a post-pivot mapper with a stale
        # pre-pivot anchor.  If a worker died, acquiring the lock does not
        # invent completion: the exact surviving transition is verified and
        # retained for explicit recovery.
        return $class->_with_thick_transition_executor_lock(
            $storeid, $scfg, $volname,
            sub {
                my ($locked_state) =
                    $class->_thick_read_anchor($storeid, $scfg, $volname);
                return $class->_thick_deactivate_volume(
                    $storeid, $scfg, $volname, $snapname, $cache,
                ) if $locked_state->{phase} eq 'MATERIALIZED';
                die "thick-generations volume '$storeid:$volname' is materializing and its exact frontend is missing; recovery required\n"
                    if !$class->_thick_frontend_present($scfg, $volname);
                $class->_thick_verify_published_transition_frontend(
                    $storeid, $scfg, $volname, $locked_state,
                );
                my $opens;
                my $close_timeout = $class->_thick_close_timeout($scfg);
                my $close_deadline = $class->_thick_progress_clock() + $close_timeout;
                while (1) {
                    $opens = $class->_thick_frontend_open_count(
                        $mapper, $class->_thick_command_deadline($scfg),
                    );
                    last if $opens == 0;
                    last if $class->_thick_progress_clock() >= $close_deadline;
                    my $remaining = $close_deadline - $class->_thick_progress_clock();
                    my $pause = $remaining < 0.1 ? $remaining : 0.1;
                    select(undef, undef, undef, $pause) if $pause > 0;
                }
                die "refusing to deactivate open thick-generations frontend '$mapper' after ${close_timeout}s close wait\n"
                    if $opens != 0;
                $class->_thick_verify_published_transition_frontend(
                    $storeid, $scfg, $volname, $locked_state,
                );
                return 1;
            },
        );
    }
    if ($class->_thick_frontend_present($scfg, $volname)) {
        if ($state->{phase} eq 'MATERIALIZED') {
            $class->_thick_verify_frontend(
                $scfg, $volname, $state->{head}, undef, 'active', 'kernel-only',
            );
        } else {
            $class->_thick_verify_published_transition_frontend(
                $storeid, $scfg, $volname, $state,
            );
        }
        my $opens;
        my $close_timeout = $class->_thick_close_timeout($scfg);
        my $close_deadline = $class->_thick_progress_clock() + $close_timeout;
        while (1) {
            $opens = $class->_thick_frontend_open_count(
                $mapper, $class->_thick_command_deadline($scfg),
            );
            last if $opens == 0;
            last if $class->_thick_progress_clock() >= $close_deadline;
            my $remaining = $close_deadline - $class->_thick_progress_clock();
            my $pause = $remaining < 0.1 ? $remaining : 0.1;
            select(undef, undef, undef, $pause) if $pause > 0;
        }
        die "refusing to deactivate open thick-generations frontend '$mapper' after ${close_timeout}s close wait\n"
            if $opens != 0;
        # A close can race qmeventd cleanup. Revalidate the exact table after
        # the bounded wait so a name reuse or table change cannot be mistaken
        # for the frontend verified before waiting.
        if ($state->{phase} eq 'MATERIALIZED') {
            $class->_thick_verify_frontend(
                $scfg, $volname, $state->{head}, undef, 'active', 'kernel-only',
            );
        } else {
            $class->_thick_verify_published_transition_frontend(
                $storeid, $scfg, $volname, $state,
            );
        }
        # A materialization worker owns the published transition mapping and
        # its dependencies.  A guest stop may close the frontend while that
        # worker is still hydrating or finalizing.  Leaving the exact verified
        # zero-open mapping in place is safe; removing any part of it here
        # would turn a normal stop into a partial transaction.
        return 1 if $state->{phase} ne 'MATERIALIZED';
        $class->_thick_remove_mapper_exact(
            $scfg,
            ['/sbin/dmsetup', 'remove', '--retry', $mapper],
            "removing stable thick-generations frontend '$mapper' failed",
            sub {
                return $class->_thick_verify_mapper_absent(
                    $scfg, $mapper,
                    "stable thick-generations frontend '$mapper' removal is unconfirmed; underlying LVs remain active",
                );
            },
        );
    }
    $class->_thick_deactivate_exact_lvs(
        $scfg, $vg, $device,
        "deactivating thick-generations state for '$vg/$volname' failed",
        $anchor, $state->{head},
    );
    return 1;
}

sub _with_lazy_volume_executor_lock {
    my ($class, $storeid, $volname, $code) = @_;
    die "invalid Lazy Thick executor lock callback\n" if ref($code) ne 'CODE';
    die "invalid Lazy Thick executor lock identity\n"
        if !defined($storeid) || $storeid !~ /^[A-Za-z0-9][A-Za-z0-9_.-]*$/
        || !defined($volname) || $volname !~ /^(?:vm|base)-\d+-disk-\d+$/;
    my $lock_path = "/run/lock/pve-sharedlvmthin-lazy-$storeid-$volname.lock";
    sysopen(my $executor_lock, $lock_path, O_CREAT | O_RDWR, 0600)
        or die "cannot open Lazy Thick per-volume executor lock: $!\n";
    flock($executor_lock, LOCK_EX | LOCK_NB)
        or die "another Lazy Thick executor is still live for '$storeid:$volname'\n";
    defined(fcntl($executor_lock, F_SETFD, 0))
        or die "cannot make Lazy Thick executor latch child-persistent: $!\n";
    return $code->();
}

sub _with_thick_transition_executor_lock {
    my ($class, $storeid, $scfg, $volname, $code) = @_;
    die "invalid Thick transition executor lock callback\n" if ref($code) ne 'CODE';
    die "invalid Thick transition executor lock identity\n"
        if !defined($storeid) || $storeid !~ /^[A-Za-z0-9][A-Za-z0-9_.-]*$/
        || !defined($volname) || $volname !~ /^(?:vm|base)-\d+-disk-\d+$/;
    my $timeout = $scfg->{'slt-mutation-admission-timeout'} // 600;
    die "invalid Thick transition executor lock timeout\n"
        if $timeout !~ /^\d+$/ || $timeout < 10 || $timeout > 86400;
    my $lock_path = "/run/lock/pve-sharedlvmthin-thick-transition-$storeid-$volname.lock";
    sysopen(my $executor_lock, $lock_path, O_CREAT | O_RDWR, 0600)
        or die "cannot open Thick transition executor lock: $!\n";
    my $deadline = $class->_thick_progress_clock() + $timeout;
    while (!flock($executor_lock, LOCK_EX | LOCK_NB)) {
        die "timed out waiting for the exact Thick transition executor for '$storeid:$volname'; no activation effect was issued\n"
            if $class->_thick_progress_clock() >= $deadline;
        select(undef, undef, undef, 0.1);
    }
    defined(fcntl($executor_lock, F_SETFD, 0))
        or die "cannot make Thick transition executor latch child-persistent: $!\n";
    return $code->();
}

sub _lazy_deactivate_volume {
    my ($class, $storeid, $scfg, $volname, @rest) = @_;
    return $class->_with_lazy_volume_executor_lock(
        $storeid, $volname,
        sub { $class->_lazy_deactivate_volume_locked(
            $storeid, $scfg, $volname, @rest,
        ) },
    );
}

sub _lazy_deactivate_volume_locked {
    my ($class, $storeid, $scfg, $volname, $snapname, $cache) = @_;
    $class->_require_thick_identity_config($storeid, $scfg);
    my $vg = $scfg->{'slt-vgname'};
    my $device = "/dev/mapper/$scfg->{'slt-expected-wwid'}";
    my ($state, undef, $anchor) =
        $class->_thick_read_anchor($storeid, $scfg, $volname);
    # A Lazy-default storage alias may reference a v5 object after explicit
    # materialization.  Snapshot/rollback then use the ordinary Thick v5
    # transition protocol.  Dispatch by the authenticated on-disk protocol,
    # not by the alias or only by the final MATERIALIZED phase: otherwise a
    # normal guest stop during HYDRATING/LINEAR_PIVOTED is misrouted into the
    # v6 LAZY_ACTIVE teardown path.
    return $class->_thick_deactivate_volume(
        $storeid, $scfg, $volname, $snapname, $cache,
    ) if int($state->{v} // 0) != 6;
    die "Lazy Thick snapshots are unavailable until explicit materialization\n"
        if defined($snapname);
    $class->_assert_no_active_storage_worker($vg);
    my ($node, $boot) = $class->_lazy_local_identity();
    my ($zero, $clone, $front) = $class->_lazy_runtime_names($scfg, $volname);
    if ($state->{phase} eq 'LAZY_DORMANT') {
        for my $mapper ($front, $clone, $zero) {
            $class->_thick_verify_mapper_absent(
                $scfg, $mapper,
                "dormant Lazy Thick volume retains mapper '$mapper'",
            );
        }
        return 1;
    }
    die "Lazy Thick deactivation requires LAZY_ACTIVE; found '$state->{phase}'\n"
        if $state->{phase} ne 'LAZY_ACTIVE';
    die "Lazy Thick deactivation refuses foreign owner '$state->{owner_node}'\n"
        if $state->{owner_node} ne $node || $state->{owner_boot} ne $boot;

    my $sectors = int($state->{bytes} / 512);
    my ($threshold, $batch) = $class->_thick_hydration_tuning(
        $scfg, int($state->{region}),
    );
    my $zero_uuid = "SLT-TG6-ZERO-$state->{tx}";
    my $clone_uuid = "SLT-TG6-CLONE-$state->{tx}";
    my $front_uuid = 'SLT-TG2-' . object_key($class->_thick_namespace($scfg), $volname);
    my $zero_table = "0 $sectors zero";
    my $zero_devno = $class->_lazy_mapper_identity(
        $scfg, $zero, $zero_uuid, $zero_table,
    );
    my $meta_devno = $class->_thick_verify_active_lv_identity(
        $scfg, $vg, $state->{metadata}, $device,
        $class->_thick_command_deadline($scfg),
    );
    my $data_devno = $class->_thick_verify_active_lv_identity(
        $scfg, $vg, $state->{head}, $device,
        $class->_thick_command_deadline($scfg),
    );
    my $clone_table = "0 $sectors clone $meta_devno $data_devno $zero_devno "
        . "$state->{region} 2 no_hydration no_discard_passdown "
        . "4 hydration_threshold $threshold hydration_batch_size $batch";
    my $clone_devno = $class->_lazy_verify_private_io_guard(
        $scfg, $clone, $clone_uuid, $clone_table,
    );
    my $front_table = "0 $sectors linear $clone_devno 0";
    my $linear_table = "0 $sectors linear $data_devno 0";
    my ($front_state) = $class->_lazy_front_pivot_state(
        $scfg, $front, $front_uuid, $front_table, $linear_table,
    );
    if ($front_state eq 'CLONE_ACTIVE') {
        $class->_lazy_verify_public_io_guard(
            $scfg, $front, $front_uuid, $front_table,
        );
    } elsif ($front_state eq 'LINEAR_ACTIVE') {
        $class->_lazy_mapper_identity(
            $scfg, $front, $front_uuid, $linear_table,
        );
    }
    $class->_thick_verify_clone_status(
        $clone, 0, $sectors, int($state->{region}),
        $class->_thick_command_deadline($scfg),
    );
    die "Lazy materialization found a pivoted frontend before hydration completion\n"
        if $state->{phase} ne 'HYDRATION_COMPLETE'
        && $front_state ne 'CLONE_ACTIVE';

    my $opens;
    my $timeout = $class->_thick_close_timeout($scfg);
    my $deadline = $class->_thick_progress_clock() + $timeout;
    while (1) {
        $opens = $class->_thick_frontend_open_count(
            $front, $class->_thick_command_deadline($scfg),
        );
        last if $opens == 0 || $class->_thick_progress_clock() >= $deadline;
        select(undef, undef, undef, 0.1);
    }
    die "refusing to deactivate open Lazy Thick frontend '$front' after ${timeout}s\n"
        if $opens != 0;
    my ($fresh_before_close) =
        $class->_thick_read_anchor($storeid, $scfg, $volname);
    die "Lazy Thick authority changed before runtime close\n"
        if $fresh_before_close->{phase} ne 'LAZY_ACTIVE'
        || $fresh_before_close->{tx} ne $state->{tx}
        || $fresh_before_close->{owner_node} ne $node
        || $fresh_before_close->{owner_boot} ne $boot
        || $fresh_before_close->{owner_epoch} ne $state->{owner_epoch}
        || $fresh_before_close->{data_uuid} ne $state->{data_uuid}
        || $fresh_before_close->{metadata_uuid} ne $state->{metadata_uuid};
    run_command(['/sbin/blockdev', '--flushbufs', "/dev/mapper/$front"],
        errmsg => "flushing Lazy Thick frontend '$front' failed");

    # The bounded close wait is an observation window. Re-prove the complete
    # mapper identities and I/O guards after it, immediately before the first
    # removal effect, so a replaced mapper incarnation can never inherit an
    # earlier proof.
    $class->_lazy_verify_public_io_guard(
        $scfg, $front, $front_uuid, $front_table,
    );
    $class->_lazy_verify_private_io_guard(
        $scfg, $clone, $clone_uuid, $clone_table,
    );
    $class->_lazy_mapper_identity($scfg, $zero, $zero_uuid, $zero_table);

    for my $entry (
        [$front, $front_uuid, 'stable frontend'],
        [$clone, $clone_uuid, 'private clone'],
        [$zero, $zero_uuid, 'private zero source'],
    ) {
        my ($mapper, undef, $description) = @$entry;
        $class->_thick_remove_mapper_exact(
            $scfg,
            ['/sbin/dmsetup', '--verifyudev', 'remove', '--retry', $mapper],
            "removing Lazy Thick $description '$mapper' failed",
            sub { $class->_thick_verify_mapper_absent(
                $scfg, $mapper,
                "Lazy Thick $description '$mapper' removal is unconfirmed",
            ) },
        );
    }
    $class->_thick_deactivate_exact_lvs(
        $scfg, $vg, $device,
        "deactivating exact Lazy Thick persistent objects failed",
        $anchor, $state->{metadata}, $state->{head},
    );

    $class->_with_vg_lock($storeid, $scfg, sub {
        my ($current) = $class->_thick_read_anchor($storeid, $scfg, $volname);
        die "Lazy Thick owner changed before dormant publication\n"
            if $current->{phase} ne 'LAZY_ACTIVE'
            || $current->{owner_node} ne $node || $current->{owner_boot} ne $boot
            || $current->{owner_epoch} ne $state->{owner_epoch};
        $class->_thick_transition_anchor(
            $scfg, $vg, $anchor, $current,
            phase => 'LAZY_DORMANT', owner_node => 'none',
            owner_boot => 'none', owner_epoch => 'none', _device => $device,
        );
        return;
    }, $device);
    return 1;
}

sub _lazy_materialize_volume {
    my ($class, $scfg, $storeid, $volname) = @_;
    return $class->_with_lazy_volume_executor_lock(
        $storeid, $volname,
        sub { $class->_lazy_materialize_volume_locked(
            $scfg, $storeid, $volname,
        ) },
    );
}

sub _lazy_materialize_volume_locked {
    my ($class, $scfg, $storeid, $volname) = @_;
    $class->_require_thick_identity_config($storeid, $scfg);
    my $vg = $scfg->{'slt-vgname'};
    my $device = "/dev/mapper/$scfg->{'slt-expected-wwid'}";
    $class->_assert_no_active_storage_worker($vg);
    my ($node, $boot) = $class->_lazy_local_identity();
    my ($state, undef, $anchor) =
        $class->_thick_read_anchor($storeid, $scfg, $volname);
    die "Lazy materialization requires a locally owned active or resumable volume\n"
        if $state->{phase} !~ /^(?:LAZY_ACTIVE|MATERIALIZING|HYDRATION_COMPLETE)$/
        || $state->{owner_node} ne $node || $state->{owner_boot} ne $boot;
    my ($zero, $clone, $front) = $class->_lazy_runtime_names($scfg, $volname);
    my $sectors = int($state->{bytes} / 512);
    my ($threshold, $batch) = $class->_thick_hydration_tuning(
        $scfg, int($state->{region}),
    );
    my $zero_uuid = "SLT-TG6-ZERO-$state->{tx}";
    my $clone_uuid = "SLT-TG6-CLONE-$state->{tx}";
    my $front_uuid = 'SLT-TG2-' . object_key($class->_thick_namespace($scfg), $volname);
    my $zero_table = "0 $sectors zero";
    my $zero_devno = $class->_lazy_mapper_identity(
        $scfg, $zero, $zero_uuid, $zero_table,
    );
    my $meta_devno = $class->_thick_verify_active_lv_identity(
        $scfg, $vg, $state->{metadata}, $device,
        $class->_thick_command_deadline($scfg),
    );
    my $data_devno = $class->_thick_verify_active_lv_identity(
        $scfg, $vg, $state->{head}, $device,
        $class->_thick_command_deadline($scfg),
    );
    my $clone_table = "0 $sectors clone $meta_devno $data_devno $zero_devno "
        . "$state->{region} 2 no_hydration no_discard_passdown "
        . "4 hydration_threshold $threshold hydration_batch_size $batch";
    my $clone_devno = $class->_lazy_verify_private_io_guard(
        $scfg, $clone, $clone_uuid, $clone_table,
    );
    my $front_table = "0 $sectors linear $clone_devno 0";
    my $linear_table = "0 $sectors linear $data_devno 0";
    my ($front_state) = $class->_lazy_front_pivot_state(
        $scfg, $front, $front_uuid, $front_table, $linear_table,
    );
    if ($front_state eq 'CLONE_ACTIVE') {
        $class->_lazy_verify_public_io_guard(
            $scfg, $front, $front_uuid, $front_table,
        );
    } elsif ($front_state eq 'LINEAR_ACTIVE') {
        $class->_lazy_mapper_identity(
            $scfg, $front, $front_uuid, $linear_table,
        );
    }
    $class->_thick_verify_clone_status(
        $clone, 0, $sectors, int($state->{region}),
        $class->_thick_command_deadline($scfg),
    );
    die "Lazy materialization found a pivoted frontend before hydration completion\n"
        if $state->{phase} ne 'HYDRATION_COMPLETE'
        && $front_state ne 'CLONE_ACTIVE';

    if ($state->{phase} eq 'LAZY_ACTIVE') {
    $class->_with_vg_lock($storeid, $scfg, sub {
        $class->_require_no_vg_intent($scfg, $vg, $device);
        my $lvs = $class->_thick_list_volumes_scoped($scfg, $vg, $device);
        $class->_thick_materialization_admission($scfg, $lvs);
        my ($current) = $class->_thick_read_anchor($storeid, $scfg, $volname, $lvs);
        die "Lazy owner changed before materialization admission\n"
            if $current->{phase} ne 'LAZY_ACTIVE'
            || $current->{owner_epoch} ne $state->{owner_epoch};
        $state = $class->_thick_transition_anchor(
            $scfg, $vg, $anchor, $current,
            phase => 'MATERIALIZING', _device => $device,
        );
        $class->_thick_fault_point('LZ0', 'LAZY_MATERIALIZE', $storeid, $volname);
        return;
    }, $device);
    }

    if ($state->{phase} eq 'MATERIALIZING') {
    eval {
        $class->_thick_enable_background_hydration(
            $clone, $class->_thick_command_deadline($scfg),
            $sectors, int($state->{region}),
        );
        $class->_thick_wait_for_hydration(
            $clone, $scfg->{'slt-tg-hydration-timeout'} // 3600,
            $class->_thick_command_deadline($scfg),
            $sectors, int($state->{region}),
        );
    };
    die "Lazy materialization requires recovery; owner and MATERIALIZING state retained: $@"
        if $@;
    }

    if ($state->{phase} eq 'MATERIALIZING') {
    $class->_with_vg_lock($storeid, $scfg, sub {
        my ($current) = $class->_thick_read_anchor($storeid, $scfg, $volname);
        die "Lazy state changed before hydration completion publication\n"
            if $current->{phase} ne 'MATERIALIZING'
            || $current->{owner_epoch} ne $state->{owner_epoch};
        $class->_thick_verify_clone_status(
            $clone, 1, $sectors, int($current->{region}),
            $class->_thick_command_deadline($scfg),
        );
        $state = $class->_thick_transition_anchor(
            $scfg, $vg, $anchor, $current,
            phase => 'HYDRATION_COMPLETE', _device => $device,
        );
        $class->_thick_fault_point('LZ1', 'LAZY_MATERIALIZE', $storeid, $volname);
        return;
    }, $device);
    }

    die "Lazy materialization cannot pivot outside HYDRATION_COMPLETE\n"
        if $state->{phase} ne 'HYDRATION_COMPLETE';
    $class->_thick_verify_clone_status(
        $clone, 1, $sectors, int($state->{region}),
        $class->_thick_command_deadline($scfg),
    );
    my %pivot_intent;
    $class->_with_vg_lock($storeid, $scfg, sub {
        # Admission, the kernel cutover, and its durable publication are one
        # serialized unit.  In particular a resumed HYDRATION_COMPLETE object
        # must not mutate DM or its anchor while an unrelated VG transaction
        # is open, nor leave a lock-release window in which one can start.
        $class->_require_no_vg_intent($scfg, $vg, $device);
        my ($current) = $class->_thick_read_anchor($storeid, $scfg, $volname);
        die "Lazy materialization authority changed before pivot\n"
            if $current->{phase} ne 'HYDRATION_COMPLETE'
            || $current->{tx} ne $state->{tx}
            || $current->{owner_node} ne $state->{owner_node}
            || $current->{owner_boot} ne $state->{owner_boot}
            || $current->{owner_epoch} ne $state->{owner_epoch}
            || $current->{data_uuid} ne $state->{data_uuid}
            || $current->{metadata_uuid} ne $state->{metadata_uuid};

        if ($front_state ne 'LINEAR_ACTIVE') {
            if ($front_state eq 'CLONE_ACTIVE') {
                $class->_thick_load_inactive_table_exact(
                    $scfg, 'reload', $front, $linear_table,
                    sub {
                        my ($inactive) = @_;
                        die "Lazy materialization inactive linear table mismatch\n"
                            if @$inactive != 1 || $inactive->[0] ne $linear_table;
                        return 1;
                    },
                    "loading hydrated Lazy Thick linear pivot failed",
                );
                $class->_thick_fault_point(
                    'LZ2', 'LAZY_MATERIALIZE', $storeid, $volname,
                );
                $front_state = 'CLONE_ACTIVE_LINEAR_PENDING';
            }
            if ($front_state eq 'CLONE_ACTIVE_LINEAR_PENDING') {
                $class->_thick_suspend_mapper_exact(
                    $scfg, $front, "suspending hydrated Lazy Thick frontend failed",
                );
                my ($suspended_state) = $class->_lazy_front_pivot_state(
                    $scfg, $front, $front_uuid, $front_table, $linear_table,
                );
                die "Lazy materialization suspended frontend lost its exact pending linear table\n"
                    if $suspended_state ne 'CLONE_SUSPENDED_LINEAR_PENDING';
            }
            $class->_thick_verify_clone_status(
                $clone, 1, $sectors, int($current->{region}),
                $class->_thick_command_deadline($scfg),
            );
            $class->_thick_resume_mapper_exact(
                $scfg, $front,
                sub { $class->_lazy_mapper_identity(
                    $scfg, $front, $front_uuid, $linear_table,
                ) },
                "publishing hydrated Lazy Thick linear pivot failed",
            );
            $class->_thick_fault_point(
                'LZ3', 'LAZY_MATERIALIZE', $storeid, $volname,
            );
            $front_state = 'LINEAR_ACTIVE';
        }
        $class->_lazy_mapper_identity(
            $scfg, $front, $front_uuid, $linear_table,
        );

        $state = $class->_thick_transition_anchor(
            $scfg, $vg, $anchor, $current,
            phase => 'LINEAR_PIVOTED', _device => $device,
        );
        $class->_thick_fault_point(
            'LZ4_NO_INTENT', 'LAZY_MATERIALIZE', $storeid, $volname,
        );
        %pivot_intent = (
            tx => $class->_new_transaction_id(), state => 'OPEN',
            op => 'DM_PIVOT', object => $anchor,
            before => $class->_vg_state_digest($scfg, $vg, $device),
        );
        $class->_set_vg_intent(
            $scfg, $vg, %pivot_intent, _device => $device,
        );
        $class->_thick_fault_point('LZ4', 'LAZY_MATERIALIZE', $storeid, $volname);
        return;
    }, $device);

    for my $entry (
        [$clone, 'private clone'], [$zero, 'private zero source'],
    ) {
        my ($mapper, $description) = @$entry;
        $class->_thick_remove_mapper_exact(
            $scfg,
            ['/sbin/dmsetup', '--verifyudev', 'remove', '--retry', $mapper],
            "removing Lazy Thick $description after pivot failed",
            sub { $class->_thick_verify_mapper_absent(
                $scfg, $mapper, "Lazy Thick $description remains after pivot",
            ) },
        );
    }
    $class->_thick_deactivate_exact_lvs(
        $scfg, $vg, $device,
        "deactivating detached Lazy Thick metadata failed",
        $state->{metadata},
    );

    $class->_with_vg_lock($storeid, $scfg, sub {
        my ($current) = $class->_thick_read_anchor($storeid, $scfg, $volname);
        die "Lazy state changed before v5 representation conversion\n"
            if $current->{phase} ne 'LINEAR_PIVOTED'
            || $current->{owner_epoch} ne $state->{owner_epoch};
        $class->_require_exact_vg_intent(
            $scfg, $vg, %pivot_intent, _device => $device,
        );
        $class->_thick_fault_point('LZ5', 'LAZY_MATERIALIZE', $storeid, $volname);
        my $lazy_data_tags = lazy_object_tags(
            sid => $storeid, vol => $volname, tx => $current->{tx},
            kind => 'data', bytes => $current->{bytes}, region => $current->{region},
        );
        my $v5_data_tags = PVE::SharedLvmThinThick::generation_tags(
            sid => $storeid, vol => $volname, role => 'head', generation => 0,
        );
        $class->_change_exact_tags(
            $scfg, $vg, $current->{head}, $lazy_data_tags, $v5_data_tags,
            "converting hydrated Lazy Thick HEAD to v5 ownership failed", $device,
        );
        $class->_thick_fault_point('LZ6', 'LAZY_MATERIALIZE', $storeid, $volname);
        my $v5_anchor = PVE::SharedLvmThinThick::anchor_tags(
            sid => $storeid, vol => $volname, phase => 'MATERIALIZED',
            tx => $current->{tx}, op => 'ALLOC', snapshot => 'none',
            source => $current->{head}, old => $current->{head},
            new => $current->{head}, head => $current->{head}, generation => 0,
            region => $current->{region},
        );
        $class->_change_exact_tags(
            $scfg, $vg, $anchor,
            PVE::SharedLvmThinThick::anchor_tags(%$current), $v5_anchor,
            "publishing hydrated Lazy Thick object as canonical v5 failed", $device,
        );
        $class->_thick_fault_point('LZ7', 'LAZY_MATERIALIZE', $storeid, $volname);
        my $after = $class->_thick_remove_exact_lv(
            $scfg, $vg, $device, $current->{metadata},
            "removing detached Lazy Thick metadata after v5 publication failed",
        );
        $class->_thick_fault_point('LZ8', 'LAZY_MATERIALIZE', $storeid, $volname);
        my ($v5) = $class->_thick_anchor($storeid, $scfg, $volname, $after);
        die "Lazy Thick v5 conversion did not preserve authoritative HEAD\n"
            if $v5->{head} ne $current->{head} || $v5->{phase} ne 'MATERIALIZED';
        $class->_clear_vg_intent(
            $scfg, $vg, %pivot_intent, _device => $device,
        );
        return;
    }, $device);
    return 'LAZY_MATERIALIZED_V5';
}

sub _lazy_recover_v5_pivot {
    my ($class, $scfg, $storeid, $volname) = @_;
    return $class->_with_lazy_volume_executor_lock(
        $storeid, $volname,
        sub { $class->_lazy_recover_v5_pivot_locked(
            $scfg, $storeid, $volname,
        ) },
    );
}

sub _lazy_recover_v5_pivot_locked {
    my ($class, $scfg, $storeid, $volname) = @_;
    $class->_require_thick_identity_config($storeid, $scfg);
    my $vg = $scfg->{'slt-vgname'};
    my $device = "/dev/mapper/$scfg->{'slt-expected-wwid'}";
    my $namespace = $class->_thick_namespace($scfg);
    my $anchor = anchor_name($namespace, $volname);
    my $data = generation_name($namespace, $volname, 0);
    my $metadata = sprintf('sltg-m-%s-%08d', object_key($namespace, $volname), 0);
    return $class->_with_vg_lock($storeid, $scfg, sub {
        my $intent = $class->_read_vg_intent($scfg, $vg, $device);
        my $lvs = $class->_thick_list_volumes_scoped($scfg, $vg, $device);
        my $objects = $lvs->{$vg} // {};
        die "Lazy v5-pivot recovery is missing anchor or data LV\n"
            if !$objects->{$anchor} || !$objects->{$data};
        my $anchor_state = decode_anchor_tags($objects->{$anchor}->{tags} // '');
        if (int($anchor_state->{v} // 0) == 6) {
            die "Lazy v5-pivot recovery signed identity mismatch\n"
                if $anchor_state->{sid} ne $storeid
                || $anchor_state->{vol} ne $volname
                || $anchor_state->{head} ne $data
                || $anchor_state->{metadata} ne $metadata;
            my $data_info = $objects->{$data};
            my $metadata_info = $objects->{$metadata};
            die "Lazy v5-pivot recovery is missing signed data or metadata\n"
                if !$data_info || !$metadata_info;
            die "Lazy v5-pivot recovery data identity differs from signed anchor\n"
                if ($data_info->{lv_uuid} // '') ne $anchor_state->{data_uuid}
                || int($data_info->{lv_size} // 0) != int($anchor_state->{bytes});
            my $geometry = clone_geometry(
                int($anchor_state->{bytes}), int($anchor_state->{region}),
            );
            die "Lazy v5-pivot recovery metadata identity differs from signed anchor\n"
                if ($metadata_info->{lv_uuid} // '') ne $anchor_state->{metadata_uuid}
                || int($metadata_info->{lv_size} // 0) < $geometry->{metadata_bytes};
            validate_lazy_object_tags(
                $metadata_info->{tags} // '', sid => $storeid, vol => $volname,
                tx => $anchor_state->{tx}, kind => 'metadata',
                bytes => $geometry->{metadata_bytes}, region => $anchor_state->{region},
            );
            my $data_tags_valid = eval {
                validate_lazy_object_tags(
                    $data_info->{tags} // '', sid => $storeid, vol => $volname,
                    tx => $anchor_state->{tx}, kind => 'data',
                    bytes => $anchor_state->{bytes}, region => $anchor_state->{region},
                ); 1;
            } || eval {
                validate_generation_tags(
                    $data_info->{tags} // '', sid => $storeid, vol => $volname,
                    role => 'head', generation => 0,
                ); 1;
            };
            die "Lazy v5-pivot recovery data tags match neither signed v6 nor exact v5 state\n"
                if !$data_tags_valid;
        }
        if (!$intent) {
            die "VG '$vg' has no recoverable Lazy v5-pivot state\n"
                if int($anchor_state->{v} // 0) != 6
                || $anchor_state->{phase} ne 'LINEAR_PIVOTED'
                || $anchor_state->{head} ne $data;
            my ($node, $boot) = $class->_lazy_local_identity();
            die "Lazy no-intent pivot recovery refuses a foreign owner epoch\n"
                if $anchor_state->{owner_node} ne $node
                || $anchor_state->{owner_boot} ne $boot;
            my (undef, undef, $front) =
                $class->_lazy_runtime_names($scfg, $volname);
            my $data_devno = $class->_thick_verify_active_lv_identity(
                $scfg, $vg, $data, $device,
                $class->_thick_command_deadline($scfg),
            );
            my $sectors = int($anchor_state->{bytes} / 512);
            my $front_uuid = 'SLT-TG2-' . object_key($namespace, $volname);
            my $linear_table = "0 $sectors linear $data_devno 0";
            my ($cutover_state) = $class->_lazy_front_pivot_state(
                $scfg, $front, $front_uuid, '__no_clone_table__', $linear_table,
            );
            die "Lazy no-intent recovery frontend is not a clean active linear cutover\n"
                if $cutover_state ne 'LINEAR_ACTIVE';
            $class->_lazy_mapper_identity(
                $scfg, $front, $front_uuid, $linear_table,
            );
            my %recovery_intent = (
                tx => $class->_new_transaction_id(), state => 'OPEN',
                op => 'DM_PIVOT', object => $anchor,
                before => $class->_vg_state_digest($scfg, $vg, $device),
            );
            $class->_set_vg_intent(
                $scfg, $vg, %recovery_intent, _device => $device,
            );
            $intent = $class->_read_vg_intent($scfg, $vg, $device);
        }
        die "VG '$vg' intent is not the exact Lazy DM_PIVOT transaction\n"
            if !$intent || $intent->{state} ne 'OPEN'
            || $intent->{op} ne 'DM_PIVOT' || $intent->{object} ne $anchor;
        if (int($anchor_state->{v} // 0) == 6) {
            die "Lazy v5-pivot recovery requires LINEAR_PIVOTED state\n"
                if $anchor_state->{phase} ne 'LINEAR_PIVOTED'
                || $anchor_state->{head} ne $data;
            my ($node, $boot) = $class->_lazy_local_identity();
            die "Lazy v5-pivot recovery refuses a foreign owner epoch\n"
                if $anchor_state->{owner_node} ne $node
                || $anchor_state->{owner_boot} ne $boot;
            my ($zero, $clone, $front) =
                $class->_lazy_runtime_names($scfg, $volname);
            my $sectors = int($anchor_state->{bytes} / 512);
            my $data_devno = $class->_thick_verify_active_lv_identity(
                $scfg, $vg, $data, $device,
                $class->_thick_command_deadline($scfg),
            );
            my $front_uuid = 'SLT-TG2-' . object_key($namespace, $volname);
            my $linear_table = "0 $sectors linear $data_devno 0";
            my ($cutover_state) = $class->_lazy_front_pivot_state(
                $scfg, $front, $front_uuid, '__no_clone_table__', $linear_table,
            );
            die "Lazy v5-pivot recovery frontend is not a clean active linear cutover\n"
                if $cutover_state ne 'LINEAR_ACTIVE';
            $class->_lazy_mapper_identity(
                $scfg, $front, $front_uuid, $linear_table,
            );
            my $inventory = _dm_kernel_inventory(
                $class->_thick_command_deadline($scfg),
            );
            if (exists($inventory->{$clone})) {
                die "Lazy v5-pivot recovery found clone without zero source\n"
                    if !exists($inventory->{$zero});
                my $meta_devno = $class->_thick_verify_active_lv_identity(
                    $scfg, $vg, $metadata, $device,
                    $class->_thick_command_deadline($scfg),
                );
                my $zero_uuid = "SLT-TG6-ZERO-$anchor_state->{tx}";
                my $zero_table = "0 $sectors zero";
                my $zero_devno = $class->_lazy_mapper_identity(
                    $scfg, $zero, $zero_uuid, $zero_table,
                );
                my ($threshold, $batch) = $class->_thick_hydration_tuning(
                    $scfg, int($anchor_state->{region}),
                );
                my $clone_uuid = "SLT-TG6-CLONE-$anchor_state->{tx}";
                my $clone_table = "0 $sectors clone $meta_devno $data_devno $zero_devno "
                    . "$anchor_state->{region} 2 no_hydration no_discard_passdown "
                    . "4 hydration_threshold $threshold hydration_batch_size $batch";
                $class->_lazy_verify_private_io_guard(
                    $scfg, $clone, $clone_uuid, $clone_table,
                );
                $class->_thick_verify_clone_status(
                    $clone, 1, $sectors, int($anchor_state->{region}),
                    $class->_thick_command_deadline($scfg),
                );
                die "Lazy v5-pivot recovery found an open detached clone\n"
                    if $class->_thick_frontend_open_count(
                        $clone, $class->_thick_command_deadline($scfg),
                    ) != 0;
                $class->_thick_remove_mapper_exact(
                    $scfg,
                    ['/sbin/dmsetup', '--verifyudev', 'remove', '--retry', $clone],
                    "removing recovered detached Lazy clone failed",
                    sub { $class->_thick_verify_mapper_absent(
                        $scfg, $clone, "recovered Lazy clone remains",
                    ) },
                );
                $inventory = _dm_kernel_inventory(
                    $class->_thick_command_deadline($scfg),
                );
            }
            if (exists($inventory->{$zero})) {
                my $zero_uuid = "SLT-TG6-ZERO-$anchor_state->{tx}";
                my $zero_table = "0 $sectors zero";
                $class->_lazy_mapper_identity(
                    $scfg, $zero, $zero_uuid, $zero_table,
                );
                die "Lazy v5-pivot recovery found an open detached zero source\n"
                    if $class->_thick_frontend_open_count(
                        $zero, $class->_thick_command_deadline($scfg),
                    ) != 0;
                $class->_thick_remove_mapper_exact(
                    $scfg,
                    ['/sbin/dmsetup', '--verifyudev', 'remove', '--retry', $zero],
                    "removing recovered detached Lazy zero source failed",
                    sub { $class->_thick_verify_mapper_absent(
                        $scfg, $zero, "recovered Lazy zero source remains",
                    ) },
                );
            }
            $class->_thick_verify_mapper_absent(
                $scfg, $clone, "recovered Lazy clone absence is unproven",
            );
            $class->_thick_verify_mapper_absent(
                $scfg, $zero, "recovered Lazy zero-source absence is unproven",
            );
            my $data_is_v5 = eval {
                validate_generation_tags(
                    $objects->{$data}->{tags} // '', sid => $storeid,
                    vol => $volname, role => 'head', generation => 0,
                ); 1;
            };
            if (!$data_is_v5) {
                my $lazy_tags = lazy_object_tags(
                    sid => $storeid, vol => $volname, tx => $anchor_state->{tx},
                    kind => 'data', bytes => $anchor_state->{bytes},
                    region => $anchor_state->{region},
                );
                my $v5_tags = PVE::SharedLvmThinThick::generation_tags(
                    sid => $storeid, vol => $volname, role => 'head', generation => 0,
                );
                $class->_change_exact_tags(
                    $scfg, $vg, $data, $lazy_tags, $v5_tags,
                    "recovering hydrated Lazy Thick HEAD v5 ownership failed", $device,
                );
            }
            my $v5_anchor = PVE::SharedLvmThinThick::anchor_tags(
                sid => $storeid, vol => $volname, phase => 'MATERIALIZED',
                tx => $anchor_state->{tx}, op => 'ALLOC', snapshot => 'none',
                source => $data, old => $data, new => $data, head => $data,
                generation => 0, region => $anchor_state->{region},
            );
            $class->_change_exact_tags(
                $scfg, $vg, $anchor,
                PVE::SharedLvmThinThick::anchor_tags(%$anchor_state), $v5_anchor,
                "recovering hydrated Lazy Thick v5 anchor publication failed", $device,
            );
            $lvs = $class->_thick_list_volumes_scoped($scfg, $vg, $device);
            $objects = $lvs->{$vg} // {};
        } else {
            die "Lazy v5-pivot recovery found a non-canonical v5 anchor\n"
                if int($anchor_state->{v} // 0) != 5
                || $anchor_state->{sid} ne $storeid
                || $anchor_state->{vol} ne $volname
                || $anchor_state->{phase} ne 'MATERIALIZED'
                || $anchor_state->{op} ne 'ALLOC'
                || $anchor_state->{snapshot} ne 'none'
                || int($anchor_state->{generation}) != 0
                || $anchor_state->{source} ne $data
                || $anchor_state->{old} ne $data
                || $anchor_state->{new} ne $data
                || $anchor_state->{head} ne $data;
            validate_generation_tags(
                $objects->{$data}->{tags} // '', sid => $storeid,
                vol => $volname, role => 'head', generation => 0,
            );
        }
        if ($objects->{$metadata}) {
            my $data_bytes = int($objects->{$data}->{lv_size} // 0);
            my $geometry = clone_geometry($data_bytes, int($anchor_state->{region}));
            validate_lazy_object_tags(
                $objects->{$metadata}->{tags} // '', sid => $storeid,
                vol => $volname, tx => $anchor_state->{tx}, kind => 'metadata',
                bytes => $geometry->{metadata_bytes}, region => $anchor_state->{region},
            );
            my $metadata_mapper = _thick_lv_mapper_name($vg, $metadata);
            my $runtime = _dm_kernel_inventory(
                $class->_thick_command_deadline($scfg),
            );
            if (exists($runtime->{$metadata_mapper})) {
                $class->_thick_deactivate_exact_lvs(
                    $scfg, $vg, $device,
                    "deactivating recovered detached Lazy metadata failed", $metadata,
                );
            } else {
                $class->_thick_verify_mapper_absent(
                    $scfg, $metadata_mapper,
                    "recovered detached Lazy metadata absence is unproven",
                );
            }
            $class->_thick_remove_exact_lv(
                $scfg, $vg, $device, $metadata,
                "removing recovered detached Lazy metadata failed",
            );
        }
        my ($final) = $class->_thick_anchor($storeid, $scfg, $volname);
        die "Lazy v5-pivot recovery final representation mismatch\n"
            if $final->{phase} ne 'MATERIALIZED' || $final->{head} ne $data
            || $final->{tx} ne $anchor_state->{tx};
        $class->_clear_vg_intent($scfg, $vg, %$intent, _device => $device);
        return 'LAZY_V5_PIVOT_RECOVERED';
    }, $device);
}

sub _command_lines {
    my ($command, $errmsg) = @_;
    my @lines;

    run_command(
        $command,
        outfunc => sub {
            my ($line) = @_;
            $line =~ s/^\s+|\s+$//g;
            push @lines, $line if length($line);
        },
        errmsg => $errmsg,
    );

    return \@lines;
}

sub _thick_list_volumes_scoped {
    my ($class, $scfg, $vg, $device) = @_;
    die "scoped Thick Generations inventory requires an exact mapper device\n"
        if !defined($device) || $device !~ m{^/dev/mapper/[0-9A-Fa-f]+$};
    my @json;
    my $command_timeout = $class->_thick_command_deadline($scfg);
    run_command(
        ['/usr/bin/timeout', '--foreground', '--kill-after=5s', "${command_timeout}s",
            '/sbin/lvs', '--readonly', '--reportformat', 'json', '--units', 'b',
            '--nosuffix', '--devices', $device,
            '-o', 'vg_name,lv_name,lv_uuid,lv_size,lv_attr,lv_tags', $vg],
        outfunc => sub { push @json, $_[0]; },
        errmsg => "reading scoped Thick Generations inventory of VG '$vg' failed",
    );
    my $report = eval { decode_json(join("\n", @json)) };
    die "scoped Thick Generations inventory of VG '$vg' is malformed: $@\n"
        if $@ || ref($report) ne 'HASH'
        || ref($report->{report}) ne 'ARRAY' || @{$report->{report}} != 1
        || ref($report->{report}->[0]->{lv}) ne 'ARRAY';
    my $result = {$vg => {}};
    for my $row (@{$report->{report}->[0]->{lv}}) {
        die "scoped Thick Generations inventory contains a malformed row\n"
            if ref($row) ne 'HASH' || ($row->{vg_name} // '') ne $vg
            || ($row->{lv_name} // '') eq '' || ($row->{lv_uuid} // '') eq ''
            || ($row->{lv_size} // '') !~ /^\d+$/
            || ($row->{lv_attr} // '') eq '';
        my $name = $row->{lv_name};
        die "scoped Thick Generations inventory contains duplicate LV '$name'\n"
            if exists($result->{$vg}->{$name});
        $result->{$vg}->{$name} = {
            lv_size => int($row->{lv_size}),
            lv_uuid => $row->{lv_uuid},
            lv_attr => $row->{lv_attr},
            lv_state => substr($row->{lv_attr}, 4, 1),
            lv_type => substr($row->{lv_attr}, 0, 1),
            tags => $row->{lv_tags} // '',
        };
    }
    return $result;
}

sub _block_device_exists {
    my ($path) = @_;

    # Device-mapper nodes under /dev are published asynchronously by udev and
    # can disappear before the corresponding kernel mapping.  Safety-critical
    # callers use this helper to decide whether cleanup, reconstruction, or a
    # new transition is legal, so a pathname lookup is not authoritative for
    # these namespaces.  Resolve their canonical DM name and query the kernel
    # inventory instead.  Keep the ordinary block-device fallback for paths
    # outside device-mapper/LVM namespaces.
    my $name;
    if (defined($path) && $path =~ m{^/dev/mapper/([^/]+)$}) {
        $name = $1;
    } elsif (defined($path) && $path =~ m{^/dev/([^/]+)/([^/]+)$}) {
        my ($vg, $lv) = ($1, $2);
        for ($vg, $lv) { s/-/--/g; }
        $name = "$vg-$lv";
    }
    if (defined($name)) {
        my $inventory = _dm_kernel_inventory();
        return exists($inventory->{$name}) ? 1 : 0;
    }
    return -b $path;
}

sub _canonical_vg_lock_id {
    my ($class, $scfg) = @_;
    my $uuid = $scfg->{'slt-expected-vg-uuid'};
    die "shared VG mutation requires a pinned VG UUID\n"
        if !defined($uuid) || $uuid eq '';
    return 'slt-vg-' . substr(sha256_hex(lc($uuid)), 0, 32);
}

sub _canonical_node_scope {
    my ($nodes) = @_;
    return '' if !defined($nodes);

    my @nodes;
    if (!ref($nodes)) {
        @nodes = split(/,/, $nodes);
    } elsif (ref($nodes) eq 'HASH') {
        @nodes = keys %$nodes;
    } elsif (ref($nodes) eq 'ARRAY') {
        @nodes = @$nodes;
    } else {
        die "same-VG alias has an unsupported PVE node-scope representation\n";
    }

    for my $node (@nodes) {
        die "same-VG alias has an ambiguous PVE node scope\n"
            if !defined($node) || ref($node);
        $node =~ s/^\s+|\s+$//g;
        die "same-VG alias has an ambiguous PVE node scope\n" if $node eq '';
    }
    @nodes = sort @nodes;
    die "same-VG alias has an ambiguous PVE node scope\n"
        if do { my %seen; grep { $seen{$_}++ } @nodes };
    return join(',', @nodes);
}

sub _verify_same_vg_alias_configuration {
    my ($class, $storeid, $scfg) = @_;
    my $vg = $scfg->{'slt-vgname'} // die "storage '$storeid' has no VG name\n";
    my $cfg = PVE::Storage::config();
    my $ids = $cfg->{ids};
    die "PVE storage configuration inventory is unavailable\n"
        if ref($ids) ne 'HASH';

    my @foreign_vg_references = sort grep {
        my $candidate = $ids->{$_};
        ref($candidate) eq 'HASH'
            && ($candidate->{type} // '') ne 'sharedlvmthin'
            && defined($candidate->{vgname})
            && $candidate->{vgname} eq $vg;
    } keys %$ids;
    die "shared VG '$vg' is also referenced by non-SharedLvmThin storage '"
        . join("', '", @foreign_vg_references)
        . "'; canonical mutation locking cannot be guaranteed\n"
        if @foreign_vg_references;

    my @aliases = sort grep {
        my $candidate = $ids->{$_};
        ref($candidate) eq 'HASH'
            && ($candidate->{type} // '') eq 'sharedlvmthin'
            && ($candidate->{'slt-vgname'} // '') eq $vg;
    } keys %$ids;
    # Validate the selected package/layout boundary even when this is the only
    # alias.  The early return must not let a hand-edited Thick-only
    # configuration smuggle in the Dual package's mixed failure-domain mode.
    $class->_vg_layout($scfg);
    return 1 if @aliases <= 1;
    die "shared VG '$vg' has more than two SharedLvmThin aliases; only one Eager "
        . "and one Lazy Thick alias may share an isolated Thick VG\n"
        if @aliases > 2;

    my %mode;
    my %layout;
    my %identity;
    my %reserve;
    my %minimum_paths;
    my %lock_timeout;
    my %lock_yield;
    my %bridge_timeout;
    my %thick_close_timeout;
    my %thick_command_deadline;
    my %thick_materialization_limit;
    my %thick_region_size;
    my %peer_connect_timeout;
    my %peer_probe_timeout;
    my %node_scope;
    for my $alias (@aliases) {
        my $candidate = $ids->{$alias};
        die "same-VG alias '$alias' is not configured as shared storage\n"
            if !$candidate->{shared};
        my $allocation = $class->_allocation_mode($candidate);
        my $vg_layout = $class->_vg_layout($candidate);
        die "shared VG '$vg' has duplicate '$allocation' allocation aliases\n"
            if $mode{$allocation}++;
        $layout{$vg_layout} = 1;
        for my $field (qw(slt-expected-vg-uuid slt-expected-pv-uuid slt-expected-wwid)) {
            my $value = $candidate->{$field};
            die "same-VG alias '$alias' must pin '$field'\n"
                if !defined($value) || $value eq '';
            $identity{$field}->{lc($value)} = 1;
        }
        my $reserve_key = join('|',
            $candidate->{'slt-vg-reserve-percent'} // '',
            $candidate->{'slt-vg-reserve-gib'} // '',
        );
        $reserve{$reserve_key} = 1;
        $minimum_paths{$candidate->{'slt-expected-min-paths'} // ''} = 1;
        $lock_timeout{$candidate->{'slt-lock-timeout'} // 30} = 1;
        $lock_yield{$candidate->{'slt-lock-yield-ms'} // 1000} = 1;
        $bridge_timeout{$candidate->{'slt-bridge-admission-timeout'} // 86400} = 1;
        $thick_close_timeout{$candidate->{'slt-tg-close-timeout'} // 30} = 1;
        $thick_command_deadline{$candidate->{'slt-tg-command-deadline-sec'} // 30} = 1;
        $thick_materialization_limit{$candidate->{'slt-tg-max-active-materializations'} // 4} = 1;
        $thick_region_size{$candidate->{'slt-tg-region-size-kib'} // 1024} = 1;
        $peer_connect_timeout{$candidate->{'slt-thin-peer-connect-timeout'} // 5} = 1;
        $peer_probe_timeout{$candidate->{'slt-thin-peer-probe-timeout'} // 15} = 1;
        $node_scope{_canonical_node_scope($candidate->{nodes})} = 1;
    }
    die "same-VG aliases must declare the same 'slt-vg-layout'\n"
        if keys(%layout) != 1;
    my $vg_layout = (keys %layout)[0];
    my $contains_thin = $mode{thin} ? 1 : 0;
    my $contains_thick = $mode{'thick-generations'}
        || $mode{'thick-generations-lazy'} ? 1 : 0;
    die "VG_MODE_CONFLICT: shared VG '$vg' contains both Thin and Thick aliases; "
        . "TG48 requires physically separate Thin and Thick VGs\n"
        if $contains_thin && $contains_thick;
    if (@aliases == 2) {
        my $eager_lazy = $mode{'thick-generations'}
            && $mode{'thick-generations-lazy'};
        die "shared VG '$vg' supports two aliases only as Eager+Lazy Thick\n"
            if !$eager_lazy;
    }
    for my $field (sort keys %identity) {
        die "same-VG aliases disagree on '$field'\n"
            if keys(%{$identity{$field}}) != 1;
    }
    die "same-VG aliases must use identical protected VG reserve settings\n"
        if keys(%reserve) != 1;
    die "same-VG aliases must use the same expected minimum path count\n"
        if keys(%minimum_paths) != 1;
    die "same-VG aliases must use the same cluster lock timeout\n"
        if keys(%lock_timeout) != 1;
    die "same-VG aliases must use the same cooperative lock yield\n"
        if keys(%lock_yield) != 1;
    die "same-VG aliases must use the same migration-bridge admission timeout\n"
        if keys(%bridge_timeout) != 1;
    die "same-VG aliases must use the same Thick frontend close timeout\n"
        if keys(%thick_close_timeout) != 1;
    die "same-VG aliases must use the same Thick command observation timeout\n"
        if keys(%thick_command_deadline) != 1;
    die "same-VG aliases must use the same Thick materialization concurrency limit\n"
        if keys(%thick_materialization_limit) != 1;
    die "same-VG aliases must use the same Thick region-size policy\n"
        if keys(%thick_region_size) != 1;
    die "same-VG aliases must use the same Thin peer SSH connection timeout\n"
        if $contains_thin && keys(%peer_connect_timeout) != 1;
    die "same-VG aliases must use the same Thin peer whole-probe timeout\n"
        if $contains_thin && keys(%peer_probe_timeout) != 1;
    die "same-VG aliases must use the same PVE node scope\n"
        if keys(%node_scope) != 1;
    return 1;
}

sub _verify_vg_failure_domain_inventory {
    my ($class, $storeid, $scfg, $device) = @_;
    return 1 if $class->_vg_layout($scfg) eq 'mixed';
    my $vg = $scfg->{'slt-vgname'} // die "storage '$storeid' has no VG name\n";
    my $inventory = $class->_thick_list_volumes_scoped($scfg, $vg, $device);
    die "isolated VG '$vg' inventory is unavailable\n"
        if ref($inventory) ne 'HASH' || ref($inventory->{$vg}) ne 'HASH';

    my (@thin_domains, @thick_domains);
    for my $name (sort keys %{$inventory->{$vg}}) {
        my $info = $inventory->{$vg}->{$name};
        die "isolated VG '$vg' contains malformed LV evidence\n"
            if ref($info) ne 'HASH';
        my $type = $info->{lv_type} // '';
        my $tags = $info->{tags} // '';
        push @thin_domains, $name if $type eq 't';
        push @thick_domains, $name
            if $name =~ /^sltg-/
            || $tags =~ /(?:^|,)slt_(?:tg|tgo|tgt|tgl)_[^,]+(?:,|$)/;
    }

    if ($class->_allocation_mode($scfg) eq 'thin') {
        die "VG_MODE_CONFLICT: isolated Thin VG '$vg' contains Thick Generations objects: "
            . join(', ', @thick_domains) . "\n"
            if @thick_domains;
    } else {
        die "VG_MODE_CONFLICT: isolated Thick VG '$vg' contains thin-pool failure domains: "
            . join(', ', @thin_domains) . "\n"
            if @thin_domains;
    }
    return 1;
}

sub _thick_foreign_intent_admission {
    my ($class, $storeid, $scfg, $requested_anchor, $previous) = @_;
    my $vg = $scfg->{'slt-vgname'};
    my $device = "/dev/mapper/$scfg->{'slt-expected-wwid'}";
    my $intent = $class->_read_vg_intent($scfg, $vg, $device);
    return { action => 'GRANT' } if !defined($intent);

    die "existing VG intent is not an exact foreign Thick transition; mutation refused\n"
        if ($intent->{state} // '') ne 'OPEN'
        || ($intent->{op} // '') !~ /^(?:DM_CUTOVER|DM_PIVOT)$/
        || ($intent->{tx} // '') !~ /^[0-9a-f]{32}$/
        || ($intent->{object} // '') eq '';
    die "the requested Thick volume already owns an unresolved transition; implicit retry refused\n"
        if ($intent->{object} // '') eq $requested_anchor;

    my $cfg = PVE::Storage::config();
    my $ids = $cfg->{ids};
    die "PVE storage configuration inventory is unavailable\n"
        if ref($ids) ne 'HASH';
    my $lvs = $class->_thick_list_volumes_scoped($scfg, $vg, $device);
    my ($foreign_sid, $foreign_scfg, $foreign_state, $foreign_vol);
    for my $sid (sort keys %$ids) {
        my $candidate = $ids->{$sid};
        next if ref($candidate) ne 'HASH'
            || ($candidate->{type} // '') ne 'sharedlvmthin'
            || ($candidate->{'slt-vgname'} // '') ne $vg;
        next if ($candidate->{'slt-expected-vg-uuid'} // '') ne
            ($scfg->{'slt-expected-vg-uuid'} // '');
        for my $name (sort keys %{$lvs->{$vg}}) {
            next if $name ne $intent->{object};
            my $decoded = eval { decode_anchor_tags($lvs->{$vg}->{$name}->{tags} // '') };
            next if $@ || ref($decoded) ne 'HASH'
                || ($decoded->{sid} // '') ne $sid;
            die "foreign Thick intent resolves to more than one storage identity\n"
                if defined($foreign_sid);
            ($foreign_sid, $foreign_scfg, $foreign_state, $foreign_vol) =
                ($sid, $candidate, $decoded, $decoded->{vol});
        }
    }
    die "foreign Thick intent has no exact configured sibling anchor owner\n"
        if !defined($foreign_sid) || !defined($foreign_vol);
    $class->_require_thick_identity_config($foreign_sid, $foreign_scfg);
    die "foreign Thick sibling identity differs from the requesting storage\n"
        if grep {
            lc($foreign_scfg->{$_} // '') ne lc($scfg->{$_} // '')
        } qw(slt-expected-vg-uuid slt-expected-pv-uuid slt-expected-wwid);

    my ($state, undef, $anchor) = $class->_thick_read_anchor(
        $foreign_sid, $foreign_scfg, $foreign_vol, $lvs,
    );
    die "foreign Thick intent anchor identity changed during admission\n"
        if $anchor ne $intent->{object}
        || int($state->{v} // 0) != 5
        || ($state->{tx} // '') ne $intent->{tx};
    my $operation = $state->{op} // '';
    my $expected_intent_op = $operation eq 'SNAPSHOT' ? 'DM_CUTOVER'
        : $operation eq 'ROLLBACK' ? 'DM_PIVOT' : '';
    die "foreign Thick intent operation does not match its signed anchor\n"
        if $expected_intent_op eq '' || $intent->{op} ne $expected_intent_op
        || ($state->{phase} // '') !~ /^(?:PREPARED|SOURCE_READY|COMMITTED|HYDRATING|HYDRATION_COMPLETE|LINEAR_PIVOTED)$/;

    # _thick_resume_transition is read-only here.  It proves the exact
    # generations, metadata LV, source, geometry, tags and transaction rather
    # than treating a syntactically valid VG tag as waitable evidence.
    $class->_thick_resume_transition(
        $foreign_scfg, $foreign_sid, $foreign_vol, $state->{snapshot},
        $operation, $intent, $lvs, 1,
    );

    if (defined($previous) && ($previous->{tx} // '') ne $intent->{tx}) {
        my $old_scfg = $ids->{$previous->{sid}};
        die "foreign Thick blocker was replaced and its prior owner is unavailable\n"
            if ref($old_scfg) ne 'HASH';
        my ($old_state, undef, $old_anchor) = $class->_thick_read_anchor(
            $previous->{sid}, $old_scfg, $previous->{vol}, $lvs,
        );
        die "foreign Thick blocker replacement is not a proven anchor handoff\n"
            if $old_anchor ne $previous->{anchor}
            || ($old_state->{tx} // '') ne $previous->{tx}
            || ($old_state->{phase} // '') !~ /^(?:HYDRATING|HYDRATION_COMPLETE|LINEAR_PIVOTED|MATERIALIZED)$/;
    }

    return {
        action => 'WAIT_EXACT_FOREIGN',
        receipt => {
            tx => $intent->{tx}, op => $intent->{op}, anchor => $anchor,
            sid => $foreign_sid, vol => $foreign_vol,
            phase => $state->{phase}, before => $intent->{before},
        },
    };
}

sub _with_thick_allocation_admission {
    my ($class, $storeid, $scfg, $requested_anchor, $code) = @_;
    die "invalid Thick allocation admission callback\n" if ref($code) ne 'CODE';
    die "invalid requested Thick allocation anchor\n"
        if !defined($requested_anchor)
        || $requested_anchor !~ /^sltg-a-[0-9a-f]{24}$/;

    my $device = "/dev/mapper/$scfg->{'slt-expected-wwid'}";
    my $budget = $scfg->{'slt-mutation-admission-timeout'} // 600;
    die "invalid mutation admission timeout\n"
        if $budget !~ /^\d+$/ || $budget < 10 || $budget > 86400;
    my $deadline = $class->_admission_now() + $budget;
    my ($previous, $round) = (undef, 0);

    while (1) {
        my $remaining = $deadline - $class->_admission_now();
        die "timed out waiting for an exact foreign Thick transition; "
            . "no allocation mutation was issued\n"
            if $remaining <= 0;
        my $decision = $class->_with_vg_lock(
            $storeid, $scfg, $code, $device, int($remaining + 1), undef,
            sub {
                return $class->_thick_foreign_intent_admission(
                    $storeid, $scfg, $requested_anchor, $previous,
                );
            },
        );
        return $decision if ref($decision) ne 'HASH'
            || ($decision->{action} // '') ne 'WAIT_EXACT_FOREIGN';
        my $receipt = $decision->{receipt};
        die "foreign Thick allocation admission returned no exact blocker receipt\n"
            if ref($receipt) ne 'HASH';
        if (!defined($previous)
            || ($previous->{tx} // '') ne ($receipt->{tx} // '')) {
            warn "waiting to allocate behind exact foreign Thick transition "
                . "tx=$receipt->{tx} storage=$receipt->{sid} "
                . "volume=$receipt->{vol} phase=$receipt->{phase}; "
                . "no allocation mutation has been issued\n";
        }
        $previous = { %$receipt };
        $round++;
        my $pause = 50 + ($round * 25);
        $pause = 500 if $pause > 500;
        $pause += int(rand(51));
        $pause = 1000 if $pause > 1000;
        $class->_thick_observation_pause($pause);
    }
}

sub _with_vg_lock {
    my ($class, $storeid, $scfg, $code, $device, $acquire_budget,
        $bridge_admission_bypass, $admission_classifier) = @_;
    my $lockid = $class->_canonical_vg_lock_id($scfg);
    # Fail immediately when quorum is already absent. The same gate is repeated
    # under the lock because quorum may disappear while the caller waits.
    $class->_verify_mutation_quorum($storeid, $scfg);
    my $timeout = $scfg->{'slt-lock-timeout'} // 30;
    $timeout = $acquire_budget if defined($acquire_budget) && $acquire_budget < $timeout;
    return $class->cluster_lock_storage(
        $lockid, $scfg->{shared}, $timeout,
        sub {
            $class->_verify_mutation_quorum($storeid, $scfg);
            $class->_verify_storage_identity($storeid, $scfg, $device);
            $class->_verify_same_vg_alias_configuration($storeid, $scfg);
            $class->_verify_vg_failure_domain_inventory($storeid, $scfg, $device);
            $class->_require_bridge_admission_compatible(
                $scfg, $scfg->{'slt-vgname'}, $device,
            ) if !$bridge_admission_bypass;
            if (defined($admission_classifier)) {
                die "invalid VG admission classifier\n"
                    if ref($admission_classifier) ne 'CODE';
                my $decision = $admission_classifier->();
                die "VG admission classifier returned an invalid decision\n"
                    if ref($decision) ne 'HASH'
                    || ($decision->{action} // '') !~ /^(?:GRANT|WAIT_EXACT_FOREIGN)$/;
                return $decision if $decision->{action} eq 'WAIT_EXACT_FOREIGN';
            }
            $class->_assert_no_active_storage_worker($scfg->{'slt-vgname'});
            return $code->();
        },
    );
}

sub _active_storage_workers {
    my ($class, $vg, $proc_root) = @_;
    die "invalid VG name for active-worker inspection\n"
        if !defined($vg) || $vg !~ /^[A-Za-z0-9+_.-]+$/;
    $proc_root //= '/proc';
    opendir(my $dh, $proc_root)
        or die "cannot enumerate process evidence: $!\n";
    my @workers;
    my $prefix = "/dev/$vg/";
    my $lvm_prefix = "$vg/";
    my %lvm_mutator = map { $_ => 1 } qw(
        lvchange lvconvert lvcreate lvextend lvremove vgchange
    );
    while (defined(my $entry = readdir($dh))) {
        next if $entry !~ /^\d+$/;
        my $path = "$proc_root/$entry/cmdline";
        open(my $fh, '<', $path) or do {
            next if !-e $path;
            closedir($dh);
            die "cannot inspect active process '$entry': $!\n";
        };
        local $/;
        my $raw = <$fh> // '';
        close($fh);
        my @argv = grep { length($_) } split(/\0/, $raw, -1);
        next if !@argv;
        my ($exe) = $argv[0] =~ m{([^/]+)$};
        my $target;
        if (($exe // '') eq 'blkdiscard' && grep { $_ eq '--zeroout' } @argv) {
            ($target) = grep { index($_, $prefix) == 0 } @argv[1 .. $#argv];
        } elsif (($exe // '') eq 'dd') {
            my ($output) = grep { index($_, 'of=') == 0 } @argv[1 .. $#argv];
            $target = substr($output, 3)
                if defined($output) && index(substr($output, 3), $prefix) == 0;
        } elsif ($lvm_mutator{$exe // ''}) {
            # A killed lock-owning parent does not prove that a storage-blocked
            # LVM child disappeared.  Refuse another local mutation while an
            # exact mutating command still names this VG.  Read-only/reporting
            # tools are intentionally excluded, as are prefix collisions such
            # as testvg-old.
            ($target) = grep {
                $_ eq $vg || index($_, $lvm_prefix) == 0
                    || index($_, $prefix) == 0
            } @argv[1 .. $#argv];
        }
        push @workers, { pid => int($entry), command => $exe, target => $target }
            if defined($target);
    }
    closedir($dh);
    @workers = sort { $a->{pid} <=> $b->{pid} } @workers;
    return \@workers;
}

sub _assert_no_active_storage_worker {
    my ($class, $vg) = @_;
    my $workers = $class->_active_storage_workers($vg);
    return 1 if !@$workers;
    my $evidence = join(', ', map {
        "pid=$_->{pid} command=$_->{command} target=$_->{target}"
    } @$workers);
    die "active storage mutation worker still targets VG '$vg' ($evidence); "
        . "refusing a second mutation or recovery worker\n";
}

sub _bridge_admission_state {
    my ($class, $vg, $device) = @_;
    my @command = ('/sbin/vgs', '--readonly');
    push @command, ('--devices', $device) if defined($device);
    push @command, ('--noheadings', '-o', 'vg_tags', $vg);
    my $lines = _command_lines(
        \@command,
        "reading migration-bridge admission state of VG '$vg' failed",
    );
    # PVE::Tools may suppress the whitespace-only record emitted by vgs for a
    # VG with no tags. Zero records therefore means the canonical clean state;
    # multiple records remain ambiguous.
    die "migration-bridge admission state of VG '$vg' is ambiguous\n"
        if @$lines > 1;
    my $tags = $lines->[0] // '';
    $tags =~ s/^\s+|\s+$//g;
    my ($schema, @tx, @nodes);
    for my $tag (split(/,/, $tags)) {
        next if $tag eq '';
        $schema++ if $tag eq 'pve-slt-bridge-v1';
        push @tx, $1 if $tag =~ /^pve-slt-bridge-tx-([0-9a-f]{32})$/;
        push @nodes, $1 if $tag =~ /^pve-slt-bridge-node-([A-Za-z0-9][A-Za-z0-9_.-]*)$/;
        die "malformed migration-bridge admission tag '$tag'\n"
            if $tag =~ /^pve-slt-bridge-/
            && $tag ne 'pve-slt-bridge-v1'
            && $tag !~ /^pve-slt-bridge-(?:tx-[0-9a-f]{32}|node-[A-Za-z0-9][A-Za-z0-9_.-]*)$/;
    }
    die "ambiguous migration-bridge admission state on VG '$vg'\n"
        if ($schema // 0) > 1 || @tx > 1 || @nodes > 1
        || (($schema // 0) || @tx || @nodes) && (($schema // 0) != 1 || @tx != 1 || @nodes != 1);
    return {
        active => ($schema // 0) ? 1 : 0,
        tx => $tx[0],
        node => $nodes[0],
    };
}

sub _bridge_admission {
    my ($class, $scfg, $storeid, $action, $tx, $node) = @_;
    die "invalid migration-bridge admission action\n"
        if !defined($action) || $action !~ /^(?:acquire|release)$/;
    die "invalid migration-bridge transaction ID\n"
        if !defined($tx) || $tx !~ /^([0-9a-f]{32})$/;
    $tx = $1;
    die "invalid migration-bridge node\n"
        if !defined($node) || $node !~ /^([A-Za-z0-9][A-Za-z0-9_.-]*)$/;
    $node = $1;
    my $local = $class->_thin_local_node();
    die "migration-bridge admission node '$node' is not local node '$local'\n"
        if $node ne $local;
    my $vg = $scfg->{'slt-vgname'};
    my $device = defined($scfg->{'slt-expected-wwid'})
        ? "/dev/mapper/$scfg->{'slt-expected-wwid'}" : undef;

    return $class->_with_vg_lock($storeid, $scfg, sub {
        $class->_require_no_vg_intent($scfg, $vg, $device);
        my $before = $class->_bridge_admission_state($vg, $device);
        if ($action eq 'acquire') {
            return 'BRIDGE_ADMISSION_ALREADY_HELD'
                if $before->{active} && $before->{tx} eq $tx && $before->{node} eq $node;
            die "migration-bridge admission is held by node '$before->{node}' transaction '$before->{tx}'\n"
                if $before->{active};
            my @command = ('/sbin/vgchange');
            push @command, ('--devices', $device) if defined($device);
            push @command,
                ('--addtag', 'pve-slt-bridge-v1',
                 '--addtag', "pve-slt-bridge-tx-$tx",
                 '--addtag', "pve-slt-bridge-node-$node", $vg);
            run_command(\@command, errmsg => "acquiring migration-bridge admission failed");
            my $after = $class->_bridge_admission_state($vg, $device);
            die "migration-bridge admission acquire postcondition failed\n"
                if !$after->{active} || $after->{tx} ne $tx || $after->{node} ne $node;
            return 'BRIDGE_ADMISSION_ACQUIRED';
        }

        die "migration-bridge admission release refused: no admission is held\n"
            if !$before->{active};
        die "migration-bridge admission release refused: exact owner mismatch\n"
            if $before->{tx} ne $tx || $before->{node} ne $node;
        my @command = ('/sbin/vgchange');
        push @command, ('--devices', $device) if defined($device);
        push @command,
            ('--deltag', 'pve-slt-bridge-v1',
             '--deltag', "pve-slt-bridge-tx-$tx",
             '--deltag', "pve-slt-bridge-node-$node", $vg);
        run_command(\@command, errmsg => "releasing migration-bridge admission failed");
        my $after = $class->_bridge_admission_state($vg, $device);
        die "migration-bridge admission release postcondition failed\n"
            if $after->{active};
        return 'BRIDGE_ADMISSION_RELEASED';
    }, $device, undef, 1);
}

sub _require_bridge_admission_compatible {
    my ($class, $scfg, $vg, $device) = @_;
    my $state = $class->_bridge_admission_state($vg, $device);
    return 1 if !$state->{active};

    my $tx = $ENV{PVE_SLT_BRIDGE_TX} // '';
    my $node = $ENV{PVE_SLT_BRIDGE_NODE} // '';
    die "VG '$vg' is reserved by migration bridge transaction '$state->{tx}' "
        . "on node '$state->{node}'; unrelated mutation refused\n"
        if $tx !~ /^[0-9a-f]{32}$/
        || $node !~ /^[A-Za-z0-9][A-Za-z0-9_.-]*$/
        || $tx ne $state->{tx} || $node ne $state->{node};
    die "migration-bridge mutation context node '$node' is not local\n"
        if $node ne $class->_thin_local_node();
    return 1;
}

sub _admission_now {
    return Time::HiRes::clock_gettime(Time::HiRes::CLOCK_MONOTONIC());
}

sub _with_mutation_lock {
    my ($class, $storeid, $scfg, $code) = @_;
    my $uuid = $scfg->{'slt-expected-vg-uuid'};

    # Pinned storage aliases that share one VG must serialize on one canonical
    # lock, regardless of whether the selected allocation mode is thin or
    # Thick Generations. Legacy unpinned configurations retain their historic
    # per-storage lock and are not qualified for same-VG mixed-mode use.
    if (defined($uuid) && $uuid ne '') {
        my $device = defined($scfg->{'slt-expected-wwid'})
            ? "/dev/mapper/$scfg->{'slt-expected-wwid'}"
            : undef;
        my $budget = $scfg->{'slt-mutation-admission-timeout'} // 600;
        die "invalid mutation admission timeout\n"
            if $budget !~ /^\d+$/ || $budget < 10 || $budget > 86400;
        my $deadline = $class->_admission_now() + $budget;
        my $delay_ms = 250;
        my $waiting;
        while (1) {
            my $remaining = $deadline - $class->_admission_now();
            die "VG mutation admission timed out; intent preserved; no mutation started\n"
                if $remaining < 1;
            my $busy;
            # No eval/retry around the callback: a mutation or lock failure is
            # propagated exactly once, including ambiguous partial failures.
            my $result = $class->_with_vg_lock($storeid, $scfg, sub {
                die "VG mutation admission timed out; no mutation started\n"
                    if $class->_admission_now() >= $deadline;
                my $intent = $class->_require_no_vg_intent($scfg,
                    $scfg->{'slt-vgname'}, $device, 1);
                if (ref($intent) eq 'HASH') {
                    $busy = $intent;
                    return;
                }
                return $code->();
            }, $device, int($remaining));
            return $result if !$busy;
            warn "waiting for VG '$scfg->{'slt-vgname'}' transition '$busy->{tx}' ($busy->{op}); no mutation started\n"
                if !defined($waiting) || $waiting ne $busy->{tx};
            $waiting = $busy->{tx};
            $remaining = $deadline - $class->_admission_now();
            next if $remaining <= 0;
            # Release canonical lock before yielding, so the transition can
            # finish. Reacquire and revalidate quorum/identity/intent each time.
            my $wait_ms = $delay_ms < $remaining * 1000 ? $delay_ms : $remaining * 1000;
            $class->_outer_lock_yield($wait_ms);
            $delay_ms *= 2 if $delay_ms < 2000;
        }
    }

    $class->_verify_mutation_quorum($storeid, $scfg);
    my $timeout = $scfg->{'slt-lock-timeout'} // 30;
    return $class->cluster_lock_storage(
        $storeid, $scfg->{shared}, $timeout,
        sub {
            $class->_verify_mutation_quorum($storeid, $scfg);
            $class->_verify_storage_identity($storeid, $scfg);
            return $code->();
        },
    );
}

sub _vg_state_digest {
    my ($class, $scfg, $vg, $device) = @_;
    my $command_timeout = $class->_thick_command_deadline($scfg);
    my @command = ('/usr/bin/timeout', '--foreground', '--kill-after=5s',
        "${command_timeout}s", '/sbin/vgs', '--readonly');
    push @command, ('--devices', $device) if defined($device);
    push @command, ('--noheadings', '--units', 'b', '--nosuffix', '--separator', '|',
        '-o', 'vg_uuid,vg_seqno,vg_free_count,vg_extent_size', $vg);
    my $lines = _command_lines(
        \@command,
        "reading before-state of VG '$vg' failed",
    );
    die "VG '$vg' before-state is ambiguous\n" if @$lines != 1;
    my $state = $lines->[0];
    $state =~ s/\s+//g;
    die "VG '$vg' before-state is malformed\n"
        if $state !~ /^([A-Za-z0-9-]+\|\d+\|\d+\|\d+(?:\.\d+)?)$/;
    $state = $1;
    return substr(sha256_hex($state), 0, 32);
}

sub _vg_tags {
    my ($class, $scfg, $vg, $device) = @_;
    my $command_timeout = $class->_thick_command_deadline($scfg);
    my @command = ('/usr/bin/timeout', '--foreground', '--kill-after=5s',
        "${command_timeout}s", '/sbin/vgs', '--readonly');
    push @command, ('--devices', $device) if defined($device);
    push @command, ('--noheadings', '--separator', '|',
        '-o', 'vg_name,vg_tags', $vg);
    my $lines = _command_lines(
        \@command,
        "reading mutation intent of VG '$vg' failed",
    );
    die "VG '$vg' tag state is ambiguous\n" if @$lines != 1;
    my ($observed_vg, $tags) = split(/\|/, $lines->[0], 2);
    for ($observed_vg, $tags) { $_ //= ''; s/^\s+|\s+$//g; }
    die "VG '$vg' tag state belongs to '$observed_vg'\n" if $observed_vg ne $vg;
    return $tags;
}

sub _read_vg_intent {
    my ($class, $scfg, $vg, $device) = @_;
    return decode_vg_intent_tags($class->_vg_tags($scfg, $vg, $device));
}

sub _require_no_vg_intent {
    my ($class, $scfg, $vg, $device, $allow_wait) = @_;
    my $intent = $class->_read_vg_intent($scfg, $vg, $device);
    # Only a successfully decoded transition is waitable. This is NOT proof
    # of liveness: abandoned transitions expire without any recovery mutation.
    return $intent if $allow_wait && $intent
        && ($intent->{state} // '') eq 'OPEN'
        && ($intent->{op} // '') =~ /^(?:DM_CUTOVER|DM_PIVOT)$/;
    die "VG '$vg' has unresolved transaction '$intent->{tx}' ($intent->{op} $intent->{object}); mutation refused\n"
        if $intent;
    return 1;
}

sub _require_exact_vg_intent {
    my ($class, $scfg, $vg, %expected) = @_;
    my $device = delete($expected{_device});
    my $intent = $class->_read_vg_intent($scfg, $vg, $device);
    die "VG '$vg' has no recoverable mutation intent\n" if !$intent;
    die "VG '$vg' mutation intent does not match the requested transaction\n"
        if grep { "$intent->{$_}" ne "$expected{$_}" }
            qw(tx state op object before);
    return 1;
}

sub _set_vg_intent {
    my ($class, $scfg, $vg, %intent) = @_;
    my $device = delete($intent{_device});
    $class->_require_no_vg_intent($scfg, $vg, $device);
    my $observed = $class->_vg_state_digest($scfg, $vg, $device);
    die "VG '$vg' changed before mutation intent could be committed\n"
        if $observed ne $intent{before};
    my $tags = vg_intent_tags(%intent);
    my $command_timeout = $class->_thick_command_deadline($scfg);
    my @command = ('/usr/bin/timeout', '--foreground', '--kill-after=5s',
        "${command_timeout}s", '/sbin/vgchange');
    push @command, ('--devices', $device) if defined($device);
    push @command, map { ('--addtag', $_) } @$tags;
    push @command, $vg;
    my $command_error = '';
    eval { run_command(\@command, errmsg => "setting mutation intent on VG '$vg' failed"); };
    $command_error = $@ if $@;
    my ($after, $post_error);
    eval { $after = $class->_read_vg_intent($scfg, $vg, $device); };
    $post_error = $@ if $@;
    $post_error ||= "VG '$vg' mutation-intent postcondition failed\n"
        if !$post_error && (!$after || grep { "$after->{$_}" ne "$intent{$_}" }
            qw(tx state op object before));
    die($command_error ne ''
        ? "setting mutation intent on VG '$vg' failed; outcome is UNKNOWN because "
            . "the exact persisted postcondition is unproven; no retry attempted: "
            . "$command_error$post_error"
        : $post_error) if $post_error;
    warn "setting mutation intent on VG '$vg' reported an error, but the exact "
        . "persisted postcondition is proven; continuing without retry: $command_error"
        if $command_error ne '';
    return 1;
}

sub _clear_vg_intent {
    my ($class, $scfg, $vg, %expected) = @_;
    my $device = delete($expected{_device});
    my $current = $class->_read_vg_intent($scfg, $vg, $device);
    die "VG '$vg' mutation-intent clear precondition failed\n"
        if !$current || grep { "$current->{$_}" ne "$expected{$_}" }
            qw(tx state op object before);
    my $tags = vg_intent_tags(%$current);
    my $command_timeout = $class->_thick_command_deadline($scfg);
    my @command = ('/usr/bin/timeout', '--foreground', '--kill-after=5s',
        "${command_timeout}s", '/sbin/vgchange');
    push @command, ('--devices', $device) if defined($device);
    push @command, map { ('--deltag', $_) } @$tags;
    push @command, $vg;
    my $command_error = '';
    eval { run_command(\@command, errmsg => "clearing mutation intent on VG '$vg' failed"); };
    $command_error = $@ if $@;
    my ($after, $post_error);
    eval { $after = $class->_read_vg_intent($scfg, $vg, $device); };
    $post_error = $@ if $@;
    $post_error ||= "VG '$vg' mutation-intent clear postcondition failed\n"
        if !$post_error && defined($after);
    die($command_error ne ''
        ? "clearing mutation intent on VG '$vg' failed; outcome is UNKNOWN because "
            . "exact absence is unproven; no retry attempted: $command_error$post_error"
        : $post_error) if $post_error;
    warn "clearing mutation intent on VG '$vg' reported an error, but exact absence "
        . "is proven; continuing without retry: $command_error" if $command_error ne '';
    return 1;
}

sub _thick_require_transition_intent {
    my ($class, $storeid, $scfg, $volname, $intent) = @_;
    my $vg = $scfg->{'slt-vgname'};
    my $device = "/dev/mapper/$scfg->{'slt-expected-wwid'}";
    if (!$intent->{_anchor_scoped}) {
        my %exact = %$intent;
        delete $exact{_anchor_scoped};
        return $class->_require_exact_vg_intent($scfg,
            $vg, %exact, _device => $device,
        );
    }

    my ($state, undef, $anchor) =
        $class->_thick_read_anchor($storeid, $scfg, $volname);
    die "anchor-scoped transition intent does not match persistent state\n"
        if $anchor ne ($intent->{object} // '')
        || ($state->{tx} // '') ne ($intent->{tx} // '')
        || ($state->{op} // '') ne 'SNAPSHOT'
        || ($intent->{op} // '') ne 'DM_CUTOVER'
        || ($state->{phase} // '') !~ /^(?:HYDRATING|HYDRATION_COMPLETE|LINEAR_PIVOTED)$/;
    return 1;
}

sub _thick_scope_transition_intent_to_anchor {
    my ($class, $storeid, $scfg, $volname, $intent) = @_;
    my $vg = $scfg->{'slt-vgname'};
    my $device = "/dev/mapper/$scfg->{'slt-expected-wwid'}";
    $class->_require_exact_vg_intent($scfg, $vg, %$intent, _device => $device);
    my ($state, undef, $anchor) =
        $class->_thick_read_anchor($storeid, $scfg, $volname);
    die "published transition is not safe for anchor-scoped materialization\n"
        if $anchor ne ($intent->{object} // '')
        || ($state->{tx} // '') ne ($intent->{tx} // '')
        || ($state->{op} // '') ne 'SNAPSHOT'
        || ($state->{phase} // '') ne 'HYDRATING';
    $class->_clear_vg_intent($scfg, $vg, %$intent, _device => $device);
    die "VG intent remained after anchor-scoped handoff\n"
        if defined($class->_read_vg_intent($scfg, $vg, $device));
    $intent->{_anchor_scoped} = 1;
    return 1;
}

sub _new_transaction_id {
    open(my $fh, '<', '/proc/sys/kernel/random/uuid')
        or die "cannot obtain kernel transaction UUID: $!\n";
    my $tx = <$fh> // '';
    close($fh);
    $tx =~ s/[^0-9A-Fa-f]//g;
    $tx = lc($tx);
    die "kernel returned an invalid transaction UUID\n" if $tx !~ /^([0-9a-f]{32})$/;
    return $1;
}

sub _thick_capacity_gate {
    my ($class, $storeid, $scfg, $size_kib, $overhead_bytes) = @_;
    die "thick-generations allocation requires slt-vg-reserve-percent or slt-vg-reserve-gib\n"
        if !defined($scfg->{'slt-vg-reserve-percent'})
        && !defined($scfg->{'slt-vg-reserve-gib'});
    my $vg = $scfg->{'slt-vgname'};
    my $device = "/dev/mapper/$scfg->{'slt-expected-wwid'}";
    my ($vg_size, $vg_free, $extent_size) = _allocation_numeric_fields(
        [
            '/usr/bin/timeout', '--foreground', '--kill-after=5s',
            $class->_thick_command_deadline($scfg) . 's',
            '/sbin/vgs', '--readonly', '--devices', $device,
            '--noheadings', '--units', 'b', '--nosuffix',
            '--separator', '|', '-o', 'vg_size,vg_free,vg_extent_size', $vg,
        ],
        "reading thick-generations capacity of VG '$vg' failed", 3,
    );
    my $bytes = int($size_kib) * 1024;
    $overhead_bytes = 8 * 1024 * 1024 if !defined($overhead_bytes);
    my $decision = PVE::SharedLvmThinSafety::evaluate_allocation_reserve(
        vg_size => int($vg_size), vg_free => int($vg_free),
        growth_bytes => $bytes, overhead_bytes => $overhead_bytes,
        extent_bytes => int($extent_size),
        reserve_percent => $scfg->{'slt-vg-reserve-percent'} // 0,
        reserve_gib => $scfg->{'slt-vg-reserve-gib'} // 0,
    );
    die "thick-generations allocation rejected for '$vg': requires "
        . "$decision->{required_physical_bytes} bytes; projected free "
        . "$decision->{free_after_bytes} bytes would cross protected reserve "
        . "$decision->{reserve_bytes} bytes; no LV was created\n"
        if !$decision->{allowed};
    $decision->{extent_bytes} = int($extent_size);
    return $decision;
}

sub _thick_materialization_admission {
    my ($class, $scfg, $lvs) = @_;
    my $vg = $scfg->{'slt-vgname'};
    my $limit = $scfg->{'slt-tg-max-active-materializations'} // 4;
    die "invalid Thick materialization concurrency limit\n"
        if $limit !~ /^\d+$/ || $limit < 1 || $limit > 64;
    die "Thick materialization admission cannot see VG '$vg'\n"
        if ref($lvs) ne 'HASH' || ref($lvs->{$vg}) ne 'HASH';

    my $active = 0;
    for my $anchor (grep { /^sltg-a-/ } keys %{$lvs->{$vg}}) {
        my $state = decode_anchor_tags($lvs->{$vg}->{$anchor}->{tags} // '');
        if (int($state->{v} // 0) == 6) {
            # Dormant, claimed and actively serving Lazy disks have background
            # hydration disabled and therefore consume no materializer slot.
            # Only an explicit materialization executor counts.
            next if $state->{phase} =~ /^(?:LAZY_PREPARED|LAZY_DORMANT|LAZY_CLAIMED|LAZY_ACTIVE)$/;
            $active++
                if $state->{phase} =~ /^(?:MATERIALIZING|HYDRATION_COMPLETE|LINEAR_PIVOTED)$/;
            die "Thick materialization admission found unsupported Lazy phase '$state->{phase}'\n"
                if $state->{phase} !~ /^(?:MATERIALIZING|HYDRATION_COMPLETE|LINEAR_PIVOTED|MATERIALIZED)$/;
            next;
        }
        next if $state->{phase} eq 'MATERIALIZED';
        die "Thick materialization admission found unsupported anchor phase '$state->{phase}'\n"
            if $state->{phase} !~ /^(?:PREPARED|SOURCE_READY|COMMITTED|HYDRATING|HYDRATION_COMPLETE|LINEAR_PIVOTED)$/;
        $active++;
    }
    die "Thick materialization admission refused on VG '$vg': $active active transition(s) "
        . "already meet configured limit $limit; no intent or LV was created\n"
        if $active >= $limit;
    return $active;
}

sub _change_exact_tags {
    my ($class, $scfg, $vg, $lv, $remove, $add, $errmsg, $device) = @_;
    my $command_timeout = $class->_thick_command_deadline($scfg);
    my $read_tags = sub {
        my @command = ('/usr/bin/timeout', '--foreground', '--kill-after=5s',
            "${command_timeout}s", '/sbin/lvs', '--readonly');
        push @command, ('--devices', $device) if defined($device);
        push @command, ('--noheadings', '-o', 'lv_tags', "$vg/$lv");
        my $lines = _command_lines(
            \@command,
            "reading exact tag state of '$vg/$lv' failed",
        );
        die "tag state of '$vg/$lv' is ambiguous\n" if @$lines > 1;
        return [] if !@$lines || $lines->[0] eq '';
        my @tags = split(/,/, $lines->[0]);
        for (@tags) { s/^\s+|\s+$//g; }
        die "tag state of '$vg/$lv' contains an empty or duplicate tag\n"
            if grep { $_ eq '' } @tags
            || do { my %seen; grep { $seen{$_}++ } @tags };
        return \@tags;
    };
    my $same = sub {
        my ($left, $right) = @_;
        return 0 if @$left != @$right;
        my %left = map { $_ => 1 } @$left;
        return !grep { !$left{$_} } @$right;
    };

    my $before = $read_tags->();
    die "tag mutation precondition failed for '$vg/$lv'\n"
        if !$same->($before, $remove);
    my %remove = map { $_ => 1 } @$remove;
    my %add = map { $_ => 1 } @$add;
    my @remove_delta = grep { !$add{$_} } @$remove;
    my @add_delta = grep { !$remove{$_} } @$add;
    my @command = ('/usr/bin/timeout', '--foreground', '--kill-after=5s',
        "${command_timeout}s", '/sbin/lvchange');
    push @command, ('--devices', $device) if defined($device);
    push @command, map { ('--deltag', $_) } @remove_delta;
    push @command, map { ('--addtag', $_) } @add_delta;
    push @command, "$vg/$lv";
    my $command_error = '';
    eval { run_command(\@command, errmsg => $errmsg); };
    $command_error = $@ if $@;
    my ($after, $post_error);
    eval { $after = $read_tags->(); };
    $post_error = $@ if $@;
    if ($post_error || !$same->($after, $add)) {
        my $evidence = $post_error
            || "tag mutation postcondition failed for '$vg/$lv'; state preserved for recovery\n";
        die $command_error ne ''
            ? "$errmsg; tag mutation outcome is UNKNOWN because the exact postcondition "
                . "is unproven; no retry attempted: $command_error$evidence"
            : $evidence;
    }
    warn "$errmsg reported an error, but the exact tag postcondition is proven; "
        . "continuing without retry: $command_error" if $command_error ne '';
    return 1;
}

sub _thick_verify_allocation_state {
    my ($class, $storeid, $scfg, $volname, $tx, $phase, $generation, $device) = @_;
    my $vg = $scfg->{'slt-vgname'};
    my $namespace = $class->_thick_namespace($scfg);
    my $anchor = anchor_name($namespace, $volname);
    my $head = generation_name($namespace, $volname, $generation);
    die "thick-generations allocation verification requires a pinned mapper device\n"
        if !defined($device);
    my $lvs = $class->_thick_list_volumes_scoped($scfg, $vg, $device);
    die "thick-generations allocation state is unavailable\n" if !$lvs->{$vg};
    die "thick-generations allocation object is incomplete\n"
        if !$lvs->{$vg}->{$anchor} || !$lvs->{$vg}->{$head};
    my $state = decode_anchor_tags($lvs->{$vg}->{$anchor}->{tags} // '');
    die "thick-generations allocation anchor mismatch\n"
        if $state->{sid} ne $storeid || $state->{vol} ne $volname
        || $state->{tx} ne $tx || $state->{phase} ne $phase
        || $state->{old} ne $head || $state->{new} ne $head
        || $state->{head} ne $head || $state->{generation} != $generation;
    validate_generation_tags(
        $lvs->{$vg}->{$head}->{tags} // '', sid => $storeid, vol => $volname,
        role => 'head', generation => $generation,
    );
    $class->_thick_verify_autoactivation_disabled($scfg, $vg, $head, $device);
    $class->_thick_verify_autoactivation_disabled($scfg, $vg, $anchor, $device);
    return ($state, $anchor, $head);
}

sub _thick_require_fresh_object_names {
    my ($class, $vg, $objects, $volname, $anchor, $head) = @_;
    die "thick-generations name-collision check requires an exact VG inventory\n"
        if ref($objects) ne 'HASH';

    for my $candidate (
        [$volname, 'PVE volume'],
        [$anchor, 'anchor'],
        [$head, 'generation'],
    ) {
        my ($name, $kind) = @$candidate;
        next if !exists($objects->{$name});
        die "refusing thick-generations allocation: $kind name collision at "
            . "'$vg/$name'; an existing LV is never adopted or overwritten\n";
    }
    return 1;
}

sub _thick_select_fresh_guest_name {
    my ($class, $namespace, $vmid, $requested, $objects) = @_;
    die "thick-generations name selection requires an exact VG inventory\n"
        if ref($objects) ne 'HASH';
    die "thick-generations name selection requires a canonical guest disk name\n"
        if !defined($requested) || $requested !~ /^vm-\Q$vmid\E-disk-\d+$/;

    for my $index (0 .. 9999) {
        my $candidate = "vm-$vmid-disk-$index";
        next if $class->_thick_guest_name_occupied(
            $namespace, $candidate, $objects,
        );
        return $candidate;
    }
    die "no collision-free Thick Generations guest disk name is available for VM $vmid\n";
}

sub _thick_guest_name_occupied {
    my ($class, $namespace, $name, $objects) = @_;
    die "thick-generations name collision check requires an exact VG inventory\n"
        if ref($objects) ne 'HASH';
    my $anchor = anchor_name($namespace, $name);
    my $head = generation_name($namespace, $name, 0);
    my $key = object_key($namespace, $name);
    return 1 if exists($objects->{$name}) || exists($objects->{$anchor})
        || exists($objects->{$head});
    return scalar grep { /^sltg-m-\Q$key\E-/ } keys %$objects;
}

sub _thick_recover_empty_allocation {
    my ($class, $scfg, $storeid, $volname) = @_;
    $class->_require_thick_identity_config($storeid, $scfg);
    my (undef, $name) = $class->parse_volname($volname);
    die "empty-allocation recovery requires the canonical volume name\n"
        if $name ne $volname;

    my $vg = $scfg->{'slt-vgname'};
    my $device = "/dev/mapper/$scfg->{'slt-expected-wwid'}";
    my $namespace = $class->_thick_namespace($scfg);
    my $anchor = anchor_name($namespace, $volname);
    my $key = object_key($namespace, $volname);
    my $mapper = mapper_name($namespace, $volname);

    return $class->_with_vg_lock($storeid, $scfg, sub {
        my $intent = $class->_read_vg_intent($scfg, $vg, $device);
        die "VG '$vg' has no recoverable mutation intent\n" if !$intent;
        die "VG '$vg' intent is not the exact empty ALLOC transaction for '$volname'\n"
            if $intent->{state} ne 'OPEN' || $intent->{op} ne 'ALLOC'
            || $intent->{object} ne $anchor;

        my $lvs = $class->_thick_list_volumes_scoped($scfg, $vg, $device);
        die "empty-allocation recovery inventory is unavailable\n" if !$lvs->{$vg};
        my $objects = $lvs->{$vg};
        my @related = sort grep {
            $_ eq $volname || $_ eq $anchor
            || /^sltg-(?:g|m)-\Q$key\E-/
        } keys %$objects;
        die "empty-allocation recovery refused: transaction-related LV state exists ("
            . join(', ', @related) . ")\n" if @related;
        die "empty-allocation recovery refused: transaction frontend '$mapper' exists\n"
            if _block_device_exists("/dev/mapper/$mapper");

        $class->_clear_vg_intent($scfg, $vg, %$intent, _device => $device);
        return 'EMPTY_ALLOCATION_INTENT_RECOVERED';
    }, $device);
}

sub _thick_pve_reference_files {
    my ($class, $storeid, $volname) = @_;
    my $volid = "$storeid:$volname";
    my %files;
    for my $pattern (
        '/etc/pve/nodes/*/qemu-server/*.conf', '/etc/pve/nodes/*/lxc/*.conf',
        '/etc/pve/qemu-server/*.conf', '/etc/pve/lxc/*.conf',
    ) {
        $files{$_} = 1 for glob($pattern);
    }
    my @references;
    for my $file (@{$class->_unique_reference_files(sort keys %files)}) {
        next if !-f $file;
        open(my $fh, '<', $file) or die "reading PVE reference file '$file' failed: $!\n";
        my $text = do { local $/; <$fh> };
        close($fh) or die "closing PVE reference file '$file' failed: $!\n";
        push @references, $file if ($text // '') =~ /\Q$volid\E(?:,|\s|$)/m;
    }
    return \@references;
}

sub _unique_reference_files {
    my ($class, @files) = @_;
    my %identity;
    my @unique;
    for my $file (@files) {
        next if !-f $file;
        my @stat = stat($file);
        die "cannot identify PVE reference file '$file': $!\n" if !@stat;
        my $key = "$stat[0]:$stat[1]";
        next if $identity{$key}++;
        push @unique, $file;
    }
    return \@unique;
}

sub _thick_recover_partial_allocation {
    my ($class, $scfg, $storeid, $volname) = @_;
    $class->_require_thick_identity_config($storeid, $scfg);
    my (undef, $name) = $class->parse_volname($volname);
    die "partial-allocation recovery requires the canonical volume name\n"
        if $name ne $volname;

    my $vg = $scfg->{'slt-vgname'};
    my $device = "/dev/mapper/$scfg->{'slt-expected-wwid'}";
    my $command_timeout = $class->_thick_command_deadline($scfg);
    my $namespace = $class->_thick_namespace($scfg);
    my $anchor = anchor_name($namespace, $volname);
    my $head = generation_name($namespace, $volname, 0);
    my $key = object_key($namespace, $volname);
    my $metadata = sprintf('sltg-m-%s-%08d', $key, 0);
    my $mapper = mapper_name($namespace, $volname);

    return $class->_with_vg_lock($storeid, $scfg, sub {
        my $intent = $class->_read_vg_intent($scfg, $vg, $device);
        die "VG '$vg' has no recoverable mutation intent\n" if !$intent;
        die "VG '$vg' intent is not the exact partial ALLOC transaction for '$volname'\n"
            if $intent->{state} ne 'OPEN' || $intent->{op} ne 'ALLOC'
            || $intent->{object} ne $anchor;

        my $lvs = $class->_thick_list_volumes_scoped($scfg, $vg, $device);
        die "partial-allocation recovery inventory is unavailable\n" if !$lvs->{$vg};
        my $objects = $lvs->{$vg};
        my @related = sort grep {
            $_ eq $volname || $_ eq $anchor || /^sltg-(?:g|m)-\Q$key\E-/
        } keys %$objects;
        my $anchor_present = exists($objects->{$anchor});
        my $head_present = exists($objects->{$head});
        my $metadata_present = exists($objects->{$metadata});
        my $anchor_state = $anchor_present
            ? decode_anchor_tags($objects->{$anchor}->{tags} // '') : undef;
        my $lazy = $metadata_present || ($anchor_state && int($anchor_state->{v} // 0) == 6);
        my %allowed = map { $_ => 1 } ($anchor, $head, ($lazy ? $metadata : ()));
        die "partial-allocation recovery requires at least one exact signed allocation object\n"
            if !@related || grep { !$allowed{$_} } @related;
        die "partial-allocation recovery refused: transaction frontend '$mapper' exists\n"
            if _block_device_exists("/dev/mapper/$mapper");

        if ($anchor_present) {
            my $state = $anchor_state;
            if ($lazy) {
                die "partial Lazy allocation anchor does not match the exact OPEN ALLOC transaction\n"
                    if int($state->{v} // 0) != 6 || $state->{sid} ne $storeid
                    || $state->{vol} ne $volname || $state->{phase} ne 'LAZY_PREPARED'
                    || $state->{op} ne 'ALLOC' || $state->{tx} ne $intent->{tx}
                    || $state->{snapshot} ne 'none' || $state->{generation} != 0
                    || $state->{head} ne $head || $state->{source} ne $head
                    || $state->{old} ne $head || $state->{new} ne $head
                    || $state->{metadata} ne $metadata || $state->{policy} ne 'lazy-zero'
                    || $state->{publication} != 0 || $state->{owner_node} ne 'none'
                    || $state->{owner_boot} ne 'none' || $state->{owner_epoch} ne 'none';
            } else {
                die "partial-allocation anchor does not match the exact OPEN ALLOC transaction\n"
                    if $state->{sid} ne $storeid || $state->{vol} ne $volname
                    || $state->{phase} ne 'PREPARED' || $state->{op} ne 'ALLOC'
                    || $state->{tx} ne $intent->{tx} || $state->{snapshot} ne 'none'
                    || $state->{generation} != 0 || $state->{head} ne $head
                    || $state->{source} ne $head || $state->{old} ne $head
                    || $state->{new} ne $head;
            }
        }
        if ($lazy) {
            my $data_state = $head_present
                ? decode_lazy_object_tags($objects->{$head}->{tags} // '') : undef;
            my $metadata_state = $metadata_present
                ? decode_lazy_object_tags($objects->{$metadata}->{tags} // '') : undef;
            die "partial Lazy data object does not match the exact OPEN ALLOC transaction\n"
                if $data_state && ($data_state->{sid} ne $storeid
                    || $data_state->{vol} ne $volname || $data_state->{tx} ne $intent->{tx}
                    || $data_state->{kind} ne 'data');
            die "partial Lazy metadata object does not match the exact OPEN ALLOC transaction\n"
                if $metadata_state && ($metadata_state->{sid} ne $storeid
                    || $metadata_state->{vol} ne $volname
                    || $metadata_state->{tx} ne $intent->{tx}
                    || $metadata_state->{kind} ne 'metadata');
            die "partial Lazy allocation object geometry is inconsistent\n"
                if $data_state && $metadata_state
                && $data_state->{region} != $metadata_state->{region};
            if ($anchor_state) {
                die "partial Lazy allocation data identity changed\n"
                    if $head_present && (($objects->{$head}->{lv_uuid} // '') eq ''
                        || $objects->{$head}->{lv_uuid} ne $anchor_state->{data_uuid}
                        || $data_state->{bytes} != $anchor_state->{bytes}
                        || $data_state->{region} != $anchor_state->{region});
                die "partial Lazy allocation metadata identity changed\n"
                    if $metadata_present && (($objects->{$metadata}->{lv_uuid} // '') eq ''
                        || $objects->{$metadata}->{lv_uuid} ne $anchor_state->{metadata_uuid}
                        || $metadata_state->{region} != $anchor_state->{region});
            }
        } else {
            validate_generation_tags(
                $objects->{$head}->{tags} // '', sid => $storeid, vol => $volname,
                role => 'head', generation => 0,
            ) if $head_present;
        }
        my $references = $class->_thick_pve_reference_files($storeid, $volname);
        die "partial-allocation recovery refused: PVE still references '$storeid:$volname' in "
            . join(', ', @$references) . "\n" if @$references;
        my @remove = $lazy ? ($metadata, $head, $anchor) : ($head, $anchor);
        my $kernel_inventory = _dm_kernel_inventory($command_timeout);
        for my $object (grep { exists($objects->{$_}) } @remove) {
            $class->_thick_verify_autoactivation_disabled($scfg, $vg, $object, $device);
            my $object_mapper = _thick_lv_mapper_name($vg, $object);
            if (exists($kernel_inventory->{$object_mapper})) {
                $class->_thick_deactivate_exact_lvs(
                    $scfg, $vg, $device,
                    "deactivating partial-allocation object '$vg/$object' failed",
                    $object,
                );
                $kernel_inventory = _dm_kernel_inventory($command_timeout);
            }
        }
        my $command_error = '';
        eval {
            for my $object (grep { exists($objects->{$_}) } @remove) {
                run_command(
                    ['/usr/bin/timeout', '--foreground', '--kill-after=5s', "${command_timeout}s",
                        '/sbin/lvremove', '--devices', $device, '-f', "$vg/$object"],
                    errmsg => "removing partial thick object '$vg/$object' failed",
                );
            }
        };
        $command_error = $@ if $@;
        my $after = $class->_thick_list_volumes_scoped($scfg, $vg, $device);
        eval { $class->_verify_storage_identity($storeid, $scfg, $device); };
        die "PARTIAL ALLOCATION CLEANUP: storage identity could not be revalidated; "
            . "OPEN ALLOC intent preserved: $@" if $@;
        my $after_objects = $after->{$vg} // {};
        die "PARTIAL ALLOCATION CLEANUP: exact objects remain; OPEN ALLOC intent preserved"
            . ($command_error ? ": $command_error" : "\n")
            if grep { exists($after_objects->{$_}) } @remove;
        die "PARTIAL ALLOCATION CLEANUP: removal reported an error after exact objects disappeared; "
            . "OPEN ALLOC intent preserved and no command was retried: $command_error"
            if $command_error;
        $class->_clear_vg_intent($scfg, $vg, %$intent, _device => $device);
        return 'PARTIAL_ALLOCATION_RECOVERED';
    }, $device);
}

sub _thick_recover_lazy_orphan_allocation {
    my ($class, $scfg, $storeid, $volname) = @_;
    $class->_require_thick_identity_config($storeid, $scfg);
    my (undef, $name) = $class->parse_volname($volname);
    die "Lazy orphan recovery requires the canonical volume name\n"
        if $name ne $volname;

    my $vg = $scfg->{'slt-vgname'};
    my $device = "/dev/mapper/$scfg->{'slt-expected-wwid'}";
    my $command_timeout = $class->_thick_command_deadline($scfg);
    my $namespace = $class->_thick_namespace($scfg);
    my $key = object_key($namespace, $volname);
    my $anchor = anchor_name($namespace, $volname);
    my $head = generation_name($namespace, $volname, 0);
    my $metadata = sprintf('sltg-m-%s-%08d', $key, 0);
    my $mapper = mapper_name($namespace, $volname);

    return $class->_with_vg_lock($storeid, $scfg, sub {
        $class->_require_no_vg_intent($scfg, $vg, $device);
        my $references = $class->_thick_pve_reference_files($storeid, $volname);
        die "Lazy orphan recovery refused: PVE still references '$storeid:$volname' in "
            . join(', ', @$references) . "\n" if @$references;
        die "Lazy orphan recovery refused: frontend '$mapper' exists\n"
            if _block_device_exists("/dev/mapper/$mapper");

        my $lvs = $class->_thick_list_volumes_scoped($scfg, $vg, $device);
        die "Lazy orphan recovery inventory is unavailable\n" if !$lvs->{$vg};
        my $objects = $lvs->{$vg};
        my @related = sort grep {
            $_ eq $volname || $_ eq $anchor || /^sltg-(?:g|m)-\Q$key\E-/
        } keys %$objects;
        my %allowed = map { $_ => 1 } ($anchor, $head, $metadata);
        die "Lazy orphan recovery requires exactly one signed generation-zero triple\n"
            if @related != 3 || scalar(grep { !$allowed{$_} } @related)
            || scalar(grep { !exists($objects->{$_}) } keys %allowed);

        my $state = decode_anchor_tags($objects->{$anchor}->{tags} // '');
        die "Lazy orphan anchor is not an exact closed unreferenced allocation\n"
            if int($state->{v} // 0) != 6 || $state->{sid} ne $storeid
            || $state->{vol} ne $volname || $state->{phase} ne 'LAZY_DORMANT'
            || $state->{op} ne 'ALLOC' || $state->{snapshot} ne 'none'
            || $state->{generation} != 0 || $state->{head} ne $head
            || $state->{source} ne $head || $state->{old} ne $head
            || $state->{new} ne $head || $state->{metadata} ne $metadata
            || $state->{policy} ne 'lazy-zero' || $state->{publication} != 1
            || $state->{owner_node} ne 'none' || $state->{owner_boot} ne 'none'
            || $state->{owner_epoch} ne 'none';
        my $data = decode_lazy_object_tags($objects->{$head}->{tags} // '');
        my $meta = decode_lazy_object_tags($objects->{$metadata}->{tags} // '');
        die "Lazy orphan data identity or geometry changed\n"
            if $data->{sid} ne $storeid || $data->{vol} ne $volname
            || $data->{tx} ne $state->{tx} || $data->{kind} ne 'data'
            || $data->{bytes} != $state->{bytes} || $data->{region} != $state->{region}
            || ($objects->{$head}->{lv_uuid} // '') ne $state->{data_uuid};
        die "Lazy orphan metadata identity or geometry changed\n"
            if $meta->{sid} ne $storeid || $meta->{vol} ne $volname
            || $meta->{tx} ne $state->{tx} || $meta->{kind} ne 'metadata'
            || $meta->{region} != $state->{region}
            || ($objects->{$metadata}->{lv_uuid} // '') ne $state->{metadata_uuid};

        my @remove = ($metadata, $head, $anchor);
        my $kernel_inventory = _dm_kernel_inventory($command_timeout);
        for my $object (@remove) {
            $class->_thick_verify_autoactivation_disabled($scfg, $vg, $object, $device);
            my $object_mapper = _thick_lv_mapper_name($vg, $object);
            if (exists($kernel_inventory->{$object_mapper})) {
                $class->_thick_deactivate_exact_lvs(
                    $scfg, $vg, $device,
                    "deactivating Lazy orphan '$vg/$object' failed", $object,
                );
                $kernel_inventory = _dm_kernel_inventory($command_timeout);
            }
        }
        my $command_error = '';
        eval {
            for my $object (@remove) {
                run_command(
                    ['/usr/bin/timeout', '--foreground', '--kill-after=5s', "${command_timeout}s",
                        '/sbin/lvremove', '--devices', $device, '-f', "$vg/$object"],
                    errmsg => "removing Lazy orphan '$vg/$object' failed",
                );
            }
        };
        $command_error = $@ if $@;
        my $after = $class->_thick_list_volumes_scoped($scfg, $vg, $device);
        eval { $class->_verify_storage_identity($storeid, $scfg, $device); };
        die "LAZY ORPHAN CLEANUP: storage identity could not be revalidated: $@" if $@;
        my $after_objects = $after->{$vg} // {};
        die "LAZY ORPHAN CLEANUP: exact objects remain"
            . ($command_error ? ": $command_error" : "\n")
            if grep { exists($after_objects->{$_}) } @remove;
        die "LAZY ORPHAN CLEANUP: removal reported an error after exact objects disappeared; "
            . "no command was retried: $command_error" if $command_error;
        return 'LAZY_ORPHAN_ALLOCATION_RECOVERED';
    }, $device);
}

sub _thick_recover_orphan_allocation {
    my ($class, $scfg, $storeid, $volname) = @_;
    my ($state) = $class->_thick_read_anchor($storeid, $scfg, $volname);
    return $class->_thick_recover_lazy_orphan_allocation($scfg, $storeid, $volname)
        if int($state->{v} // 0) == 6 && ($state->{policy} // '') eq 'lazy-zero';
    $class->_thick_free_image($storeid, $scfg, $volname, 0, 1);
    return 'ORPHAN_ALLOCATION_RECOVERED';
}

sub _thick_recover_volume_delete {
    my ($class, $scfg, $storeid, $volname) = @_;
    $class->_require_thick_identity_config($storeid, $scfg);
    my (undef, $name) = $class->parse_volname($volname);
    die "volume-delete recovery requires the canonical volume name\n"
        if $name ne $volname;

    my $vg = $scfg->{'slt-vgname'};
    my $device = "/dev/mapper/$scfg->{'slt-expected-wwid'}";
    my $command_timeout = $class->_thick_command_deadline($scfg);
    my $namespace = $class->_thick_namespace($scfg);
    my $anchor = anchor_name($namespace, $volname);
    my $key = object_key($namespace, $volname);
    my $mapper = mapper_name($namespace, $volname);

    return $class->_with_vg_lock($storeid, $scfg, sub {
        my $intent = $class->_read_vg_intent($scfg, $vg, $device);
        die "VG '$vg' has no volume-delete transaction to recover\n" if !$intent;
        die "VG '$vg' intent is not the exact OPEN REMOVE transaction for '$volname'\n"
            if $intent->{state} ne 'OPEN' || $intent->{op} ne 'REMOVE'
            || $intent->{object} ne $anchor;
        my %expected = %$intent;
        $class->_require_exact_vg_intent($scfg, $vg, %expected, _device => $device);

        my $references = $class->_thick_pve_reference_files($storeid, $volname);
        die "volume-delete recovery refused: PVE still references '$storeid:$volname' in "
            . join(', ', @$references) . "\n" if @$references;
        die "volume-delete recovery refused: transaction frontend '$mapper' exists\n"
            if _block_device_exists("/dev/mapper/$mapper");

        my $lvs = $class->_thick_list_volumes_scoped($scfg, $vg, $device);
        my $objects = $lvs->{$vg} // {};
        my @related = sort grep {
            $_ eq $volname || $_ eq $anchor || /^sltg-(?:g|m)-\Q$key\E-/
        } keys %$objects;
        my $anchor_present = exists($objects->{$anchor});
        my $head;
        if ($anchor_present) {
            my $state = decode_anchor_tags($objects->{$anchor}->{tags} // '');
            die "volume-delete recovery anchor is not a canonical materialized allocation\n"
                if $state->{sid} ne $storeid || $state->{vol} ne $volname
                || $state->{phase} ne 'MATERIALIZED' || $state->{op} ne 'ALLOC'
                || $state->{snapshot} ne 'none' || $state->{source} ne $state->{head}
                || $state->{old} ne $state->{head} || $state->{new} ne $state->{head};
            $head = $state->{head};
            die "volume-delete recovery found objects outside the signed HEAD and anchor\n"
                if grep { $_ ne $anchor && $_ ne $head } @related;
            if (exists($objects->{$head})) {
                validate_generation_tags(
                    $objects->{$head}->{tags} // '', sid => $storeid, vol => $volname,
                    role => 'head', generation => $state->{generation},
                );
            }
        } else {
            die "volume-delete recovery found owned-looking objects without the signed anchor\n"
                if @related;
        }

        my @remaining = grep { defined($_) && exists($objects->{$_}) } ($head, $anchor);
        for my $object (@remaining) {
            $class->_thick_verify_autoactivation_disabled($scfg, $vg, $object, $device);
        }
        $class->_thick_deactivate_exact_lvs(
            $scfg, $vg, $device,
            "deactivating remaining volume-delete objects for '$vg/$volname' failed",
            @remaining,
        ) if @remaining;

        my $command_error = '';
        eval {
            run_command(
                ['/usr/bin/timeout', '--foreground', '--kill-after=5s', "${command_timeout}s",
                    '/sbin/lvremove', '--devices', $device, '-f', "$vg/$head"],
                errmsg => "removing remaining thick generation '$vg/$head' failed",
            ) if defined($head) && exists($objects->{$head});
            run_command(
                ['/usr/bin/timeout', '--foreground', '--kill-after=5s', "${command_timeout}s",
                    '/sbin/lvremove', '--devices', $device, '-f', "$vg/$anchor"],
                errmsg => "removing remaining thick generation anchor '$vg/$anchor' failed",
            ) if $anchor_present;
        };
        $command_error = $@ if $@;

        eval { $class->_verify_storage_identity($storeid, $scfg, $device); };
        die "VOLUME DELETE RECOVERY: storage identity could not be revalidated; "
            . "OPEN REMOVE intent preserved: $@" if $@;
        my $after = $class->_thick_list_volumes_scoped($scfg, $vg, $device);
        my $after_objects = $after->{$vg} // {};
        my @after_related = grep {
            $_ eq $volname || $_ eq $anchor || /^sltg-(?:g|m)-\Q$key\E-/
        } keys %$after_objects;
        die "VOLUME DELETE RECOVERY: exact or ambiguous objects remain; "
            . "OPEN REMOVE intent preserved" . ($command_error ? ": $command_error" : "\n")
            if @after_related;
        warn "volume-delete recovery command reported an error, but authoritative inventory "
            . "proves all exact objects absent; clearing the matching intent without retry: "
            . $command_error if $command_error;
        $class->_clear_vg_intent($scfg, $vg, %expected, _device => $device);
        return 'VOLUME_DELETE_RECOVERED';
    }, $device);
}

sub _thick_recover_unpublished_prepare {
    my ($class, $scfg, $storeid, $volname) = @_;
    $class->_require_thick_identity_config($storeid, $scfg);
    my (undef, $name) = $class->parse_volname($volname);
    die "unpublished-prepare recovery requires the canonical volume name\n"
        if $name ne $volname;

    my $vg = $scfg->{'slt-vgname'};
    my $device = "/dev/mapper/$scfg->{'slt-expected-wwid'}";
    my $command_timeout = $class->_thick_command_deadline($scfg);
    my $namespace = $class->_thick_namespace($scfg);
    my $expected_anchor = anchor_name($namespace, $volname);
    my $key = object_key($namespace, $volname);
    my $front = mapper_name($namespace, $volname);

    return $class->_with_vg_lock($storeid, $scfg, sub {
        my $intent = $class->_read_vg_intent($scfg, $vg, $device);
        die "VG '$vg' has no unpublished transition prepare to recover\n" if !$intent;
        die "VG '$vg' intent is not an exact OPEN unpublished transition for '$volname'\n"
            if $intent->{state} ne 'OPEN'
            || ($intent->{op} ne 'DM_CUTOVER' && $intent->{op} ne 'DM_PIVOT')
            || $intent->{object} ne $expected_anchor;
        my %expected_intent = %$intent;
        $class->_require_exact_vg_intent($scfg,
            $vg, %expected_intent, _device => $device,
        );

        my $lvs = $class->_thick_list_volumes_scoped($scfg, $vg, $device);
        die "unpublished-prepare recovery cannot see VG '$vg'\n" if !$lvs->{$vg};
        my ($state, $head_info, $anchor) =
            $class->_thick_anchor($storeid, $scfg, $volname, $lvs);
        die "unpublished-prepare recovery found a non-materialized anchor\n"
            if $state->{phase} ne 'MATERIALIZED';
        die "unpublished-prepare recovery intent was already recorded in the anchor\n"
            if $state->{tx} eq $intent->{tx};
        die "unpublished-prepare recovery anchor identity mismatch\n"
            if $anchor ne $expected_anchor;
        $class->_thick_verify_frontend(
            $scfg, $volname, $state->{head}, int($head_info->{lv_size} / 512),
        );
        die "unpublished-prepare recovery refused: stable frontend is suspended\n"
            if $class->_thick_mapper_is_suspended(
                $front, $class->_thick_command_deadline($scfg),
            );
        my $dm = _dm_kernel_inventory($command_timeout);
        die "unpublished-prepare recovery found transition runtime before PREPARED\n"
            if grep { /^\Q$front\E-src-\d{8}$/ } keys %$dm;

        my $generation = int($state->{generation}) + 1;
        die "unpublished-prepare recovery generation is outside the supported range\n"
            if $generation > 99_999_999;
        my $new = generation_name($namespace, $volname, $generation);
        my $meta = sprintf('sltg-m-%s-%08d', $key, $generation);
        my $objects = $lvs->{$vg};
        for my $object (grep { /^sltg-g-\Q$key\E-\d{8}$/ } keys %$objects) {
            next if $object eq $state->{head} || $object eq $new;
            my $owned = decode_generation_tags($objects->{$object}->{tags} // '');
            die "unpublished-prepare recovery found an ambiguous generation\n"
                if $owned->{sid} ne $storeid || $owned->{vol} ne $volname
                || $owned->{role} ne 'snapshot'
                || generation_name($namespace, $volname, $owned->{generation}) ne $object;
        }
        my @metadata = grep { /^sltg-m-\Q$key\E-/ } keys %$objects;
        die "unpublished-prepare recovery found foreign transition metadata\n"
            if grep { $_ ne $meta } @metadata;
        if (exists($objects->{$new})) {
            validate_generation_tags(
                $objects->{$new}->{tags} // '', sid => $storeid, vol => $volname,
                role => 'head', generation => $generation,
            );
        }
        if (exists($objects->{$meta})) {
            my $owned = decode_transition_tags($objects->{$meta}->{tags} // '');
            die "unpublished-prepare metadata ownership does not match the exact intent\n"
                if $owned->{sid} ne $storeid || $owned->{vol} ne $volname
                || $owned->{tx} ne $intent->{tx} || $owned->{kind} ne 'metadata'
                || int($owned->{generation}) != $generation;
        }

        my @remaining = grep { exists($objects->{$_}) } ($meta, $new);
        for my $object (@remaining) {
            $class->_thick_verify_autoactivation_disabled($scfg, $vg, $object, $device);
        }
        $class->_thick_deactivate_exact_lvs(
            $scfg, $vg, $device,
            "deactivating unpublished transition objects for '$vg/$volname' failed",
            @remaining,
        ) if @remaining;
        my $command_error = '';
        eval {
            for my $object ($meta, $new) {
                next if !exists($objects->{$object});
                run_command(
                    ['/usr/bin/timeout', '--foreground', '--kill-after=5s',
                        "${command_timeout}s", '/sbin/lvremove', '--devices', $device,
                        '-f', "$vg/$object"],
                    errmsg => "removing unpublished transition object '$vg/$object' failed",
                );
            }
        };
        $command_error = $@ if $@;

        $class->_verify_storage_identity($storeid, $scfg, $device);
        my $after = $class->_thick_list_volumes_scoped($scfg, $vg, $device);
        die "unpublished-prepare recovery cannot confirm VG '$vg' after cleanup\n"
            if !$after->{$vg};
        die "unpublished-prepare recovery left exact transition objects; intent preserved"
            . ($command_error ? ": $command_error" : "\n")
            if exists($after->{$vg}->{$new}) || exists($after->{$vg}->{$meta});
        my ($final, $final_head, $final_anchor) =
            $class->_thick_anchor($storeid, $scfg, $volname, $after);
        die "unpublished-prepare recovery changed the authoritative anchor\n"
            if $final_anchor ne $anchor
            || join('|', @{PVE::SharedLvmThinThick::anchor_tags(%$final)})
                ne join('|', @{PVE::SharedLvmThinThick::anchor_tags(%$state)});
        $class->_thick_verify_frontend(
            $scfg, $volname, $final->{head}, int($final_head->{lv_size} / 512),
        );
        warn "unpublished-prepare cleanup command reported an error, but exact postconditions "
            . "prove cleanup complete; clearing the matching intent without retry: "
            . $command_error if $command_error;
        $class->_clear_vg_intent($scfg,
            $vg, %expected_intent, _device => $device,
        );
        return 'UNPUBLISHED_PREPARE_RECOVERED';
    }, $device);
}

sub _zero_new_thick_generation {
    my ($class, $path, $bytes, $description) = @_;
    die "invalid thick-generation zero length\n"
        if !defined($bytes) || $bytes !~ /^\d+$/ || $bytes < 512 || $bytes % 512;
    die "invalid thick-generation block-device path\n"
        if !defined($path)
        || $path !~ m{^/dev/[A-Za-z0-9+_.-]+/[A-Za-z0-9+_.-]+$}
        || $path =~ m{(?:^|/)\.\.?($|/)};

    # BLKZEROOUT is a standard Linux block ioctl. It can be offloaded through
    # DM/SCSI by capable storage and otherwise fails before we use the
    # universally qualified direct-write path. A failed ioctl may have
    # completed a prefix, which is harmless because the fallback rewrites the
    # entire exact range. Never use BLKDISCARD here: a Thick Generation must
    # remain a fully allocated LVM LV with deterministic zero contents.
    my $zeroout_error;
    eval {
        run_command(
            ['/usr/sbin/blkdiscard', '--zeroout', '--offset', '0',
                '--length', "$bytes", $path],
            errmsg => "BLKZEROOUT of $description failed",
        );
    };
    return 'blkzeroout' if !$@;
    $zeroout_error = $@;

    eval {
        run_command(
            ['/usr/bin/dd', 'if=/dev/zero', "of=$path", 'bs=4M',
                "count=$bytes", 'iflag=count_bytes', 'oflag=direct',
                'conv=fsync,nocreat', 'status=none'],
            errmsg => "direct zero initialization of $description failed",
        );
    };
    die "BLKZEROOUT was unavailable for $description ($zeroout_error)"
        . "and the full direct-write fallback failed: $@" if $@;
    return 'direct-write-fallback';
}

sub _thick_alloc_image {
    my ($class, $storeid, $scfg, $vmid, $fmt, $name, $size) = @_;
    die "unsupported format '$fmt'\n" if defined($fmt) && $fmt ne 'raw';
    $class->_require_thick_identity_config($storeid, $scfg);
    $name = $class->find_free_diskname($storeid, $scfg, $vmid) if !$name;
    my $is_guest = $name =~ /^vm-\Q$vmid\E-disk-\d+$/;
    my $is_aux = $name =~ /^vm-\Q$vmid\E-(?:state-[A-Za-z0-9][A-Za-z0-9_.-]*|fleece-\d+|cloudinit)$/;
    die "illegal volume name '$name'\n" if !$is_guest && !$is_aux;
    die "invalid thick-generations allocation size\n"
        if !defined($size) || $size !~ /^\d+$/ || $size < 1;

    my $vg = $scfg->{'slt-vgname'};
    my $device = "/dev/mapper/$scfg->{'slt-expected-wwid'}";
    my $namespace = $class->_thick_namespace($scfg);
    my $generation = 0;
    my $geometry = $class->_thick_new_geometry($scfg, int($size) * 1024);
    my ($anchor, $head);
    my $tx = $class->_new_transaction_id();
    my %intent;

    my $requested_anchor = anchor_name($namespace, $name);
    $class->_with_thick_allocation_admission($storeid, $scfg, $requested_anchor, sub {
        $class->_require_no_vg_intent($scfg, $vg, $device);
        $class->_thick_capacity_gate($storeid, $scfg, $size);
        my $lvs = $class->_thick_list_volumes_scoped($scfg, $vg, $device);
        my $objects = $lvs->{$vg} // {};
        if ($is_guest && $class->_thick_guest_name_occupied(
            $namespace, $name, $objects,
        )) {
            $name = $class->_thick_select_fresh_guest_name(
                $namespace, $vmid, $name, $objects,
            );
        }
        $anchor = anchor_name($namespace, $name);
        $head = generation_name($namespace, $name, $generation);
        $class->_thick_require_fresh_object_names(
            $vg, $objects, $name, $anchor, $head,
        );
        my $before = $class->_vg_state_digest($scfg, $vg, $device);
        %intent = (
            tx => $tx, state => 'OPEN', op => 'ALLOC', object => $anchor,
            before => $before,
        );
        $class->_set_vg_intent($scfg, $vg, %intent, _device => $device);
        my $head_tags = PVE::SharedLvmThinThick::generation_tags(
            sid => $storeid, vol => $name, role => 'head', generation => $generation,
        );
        my $anchor_tags = PVE::SharedLvmThinThick::anchor_tags(
            sid => $storeid, vol => $name, phase => 'PREPARED', tx => $tx,
            op => 'ALLOC', snapshot => 'none', source => $head,
            old => $head, new => $head, head => $head, generation => $generation,
            region => $geometry->{region_sectors},
        );
        my @head_create = (
            '/sbin/lvcreate', '--yes', '--wipesignatures', 'y', '--ignoreactivationskip',
            '--devices', $device, '-L', "${size}K", '-n', $head,
            '--setactivationskip', 'y', '--setautoactivation', 'n',
        );
        push @head_create, map { ('--addtag', $_) } @$head_tags;
        push @head_create, $vg;
        $class->_thick_create_lv_exact(
            $scfg, $device, \@head_create,
            "creating thick generation '$vg/$head' failed",
            sub {
                return $class->_thick_verify_created_lv_exact(
                    $scfg, $vg, $head, $device, int($size) * 1024,
                    sub { validate_generation_tags(
                        $_[0], sid => $storeid, vol => $name,
                        role => 'head', generation => $generation,
                    ) },
                );
            },
        );
        my @anchor_create = (
            '/sbin/lvcreate', '--yes', '--wipesignatures', 'y', '--ignoreactivationskip',
            '--devices', $device, '-L', '8M', '-n', $anchor,
            '--setactivationskip', 'y', '--setautoactivation', 'n',
        );
        push @anchor_create, map { ('--addtag', $_) } @$anchor_tags;
        push @anchor_create, $vg;
        $class->_thick_create_lv_exact(
            $scfg, $device, \@anchor_create,
            "creating thick generation anchor '$vg/$anchor' failed",
            sub {
                return $class->_thick_verify_created_lv_exact(
                    $scfg, $vg, $anchor, $device, 8 * 1024 * 1024,
                    sub {
                        my $state = decode_anchor_tags($_[0]);
                        die "created Thick allocation anchor identity mismatch\n"
                            if $state->{sid} ne $storeid || $state->{vol} ne $name
                            || $state->{phase} ne 'PREPARED' || $state->{tx} ne $tx
                            || $state->{op} ne 'ALLOC' || $state->{snapshot} ne 'none'
                            || $state->{source} ne $head || $state->{old} ne $head
                            || $state->{new} ne $head || $state->{head} ne $head
                            || int($state->{generation}) != $generation
                            || int($state->{region}) != $geometry->{region_sectors};
                        return 1;
                    },
                );
            },
        );
        $class->_thick_verify_allocation_state(
            $storeid, $scfg, $name, $tx, 'PREPARED', $generation, $device,
        );
        return;
    });

    eval {
        $class->_thick_activate_exact_lvs(
            $scfg, $vg, $device,
            "activating new thick generation '$vg/$head' for zeroing failed",
            $head,
        );
        my $zero_bytes = int($size) * 1024;
        $class->_zero_new_thick_generation(
            "/dev/$vg/$head", $zero_bytes, "new thick generation '$vg/$head'",
        );
        run_command(
            ['/sbin/blockdev', '--flushbufs', "/dev/$vg/$head"],
            errmsg => "flushing new thick generation '$vg/$head' failed",
        );
        $class->_thick_deactivate_exact_lvs(
            $scfg, $vg, $device,
            "deactivating zeroed thick generation '$vg/$head' failed",
            $head,
        );
    };
    if (my $error = $@) {
        die _partial_allocation_error(
            $storeid, $vmid, $name, $anchor, $error,
            "PREPARED thick generation '$vg/$head' preserved with OPEN VG intent",
        );
    }

    $class->_with_vg_lock($storeid, $scfg, sub {
        $class->_require_exact_vg_intent($scfg, $vg, %intent, _device => $device);
        $class->_thick_verify_allocation_state(
            $storeid, $scfg, $name, $tx, 'PREPARED', $generation, $device,
        );
        my $old_tags = PVE::SharedLvmThinThick::anchor_tags(
            sid => $storeid, vol => $name, phase => 'PREPARED', tx => $tx,
            op => 'ALLOC', snapshot => 'none', source => $head,
            old => $head, new => $head, head => $head, generation => $generation,
            region => $geometry->{region_sectors},
        );
        my $new_tags = PVE::SharedLvmThinThick::anchor_tags(
            sid => $storeid, vol => $name, phase => 'MATERIALIZED', tx => $tx,
            op => 'ALLOC', snapshot => 'none', source => $head,
            old => $head, new => $head, head => $head, generation => $generation,
            region => $geometry->{region_sectors},
        );
        $class->_change_exact_tags($scfg, $vg, $anchor, $old_tags, $new_tags,
            "committing materialized thick generation '$vg/$head' failed", $device);
        $class->_thick_verify_allocation_state(
            $storeid, $scfg, $name, $tx, 'MATERIALIZED', $generation, $device,
        );
        $class->_clear_vg_intent($scfg, $vg, %intent, _device => $device);
        return;
    }, $device);
    return $name;
}

sub _lazy_alloc_image {
    my ($class, $storeid, $scfg, $vmid, $fmt, $name, $size) = @_;
    die "unsupported format '$fmt'\n" if defined($fmt) && $fmt ne 'raw';
    $class->_require_thick_identity_config($storeid, $scfg);
    $name = $class->find_free_diskname($storeid, $scfg, $vmid) if !$name;
    # PVE creates an EFI vars disk and activates it before the VM config can
    # reference the new volume.  An unreferenced Lazy disk may be activated
    # only by the one-shot Storage Move capability, which EFI creation does
    # not and must not receive.  EFI is tiny, so route only the exact upstream
    # OVMF creation callsite through the already-qualified Eager lifecycle.
    # Do not infer EFI from size or the generated vm-*-disk-* name.
    if ($class->_lazy_efi_allocation_requires_eager()) {
        return $class->_thick_alloc_image(
            $storeid, $scfg, $vmid, $fmt, $name, $size,
        );
    }
    # Lazy materialization is meaningful only for persistent guest disks.
    # PVE auxiliary RAW objects (RAM/vmstate, fleecing and cloud-init) remain
    # fully allocated but use the already-qualified Eager initialization path.
    # This preserves one Thick on-disk format while allowing RAM snapshots on
    # a Lazy-default storage without exposing a partially materialized vmstate.
    if ($name =~ /^vm-\Q$vmid\E-(?:state-[A-Za-z0-9][A-Za-z0-9_.-]*|fleece-\d+|cloudinit)$/) {
        return $class->_thick_alloc_image(
            $storeid, $scfg, $vmid, $fmt, $name, $size,
        );
    }
    die "Lazy Thick supports canonical guest disks only\n"
        if $name !~ /^vm-\Q$vmid\E-disk-\d+$/;
    my $move_context = $class->_lazy_prepare_move_allocation($storeid, $scfg, $vmid);
    die "invalid Lazy Thick allocation size\n"
        if !defined($size) || $size !~ /^\d+$/ || $size < 1;

    my $bytes = int($size) * 1024;
    die "Lazy Thick allocation size must be sector aligned\n" if $bytes % 512;
    my $vg = $scfg->{'slt-vgname'};
    my $device = "/dev/mapper/$scfg->{'slt-expected-wwid'}";
    my $namespace = $class->_thick_namespace($scfg);
    my $geometry;
    my $allocated_bytes;
    my $tx = $class->_new_transaction_id();
    my ($anchor, $data, $metadata, %intent);

    $class->_with_vg_lock($storeid, $scfg, sub {
        $class->_require_no_vg_intent($scfg, $vg, $device);
        # Learn the exact VG extent before signing any Lazy object. LVM rounds
        # small auxiliary-style guest disks (notably EFI disks) to a complete
        # extent; signing the unrounded PVE request makes our own backing LV
        # fail identity validation after creation. This first admission is
        # read-only and preliminary; the second one below includes the exact
        # rounded data and metadata/anchor consumption before any lvcreate.
        my $capacity = $class->_thick_capacity_gate(
            $storeid, $scfg, $size, 0,
        );
        my $extent_bytes = $capacity->{extent_bytes};
        die "Lazy Thick allocation cannot determine the VG extent size\n"
            if !defined($extent_bytes) || $extent_bytes !~ /^\d+$/ || $extent_bytes < 512;
        $allocated_bytes = int(($bytes + $extent_bytes - 1) / $extent_bytes) * $extent_bytes;
        $geometry = $class->_thick_new_geometry($scfg, $allocated_bytes);
        $class->_thick_capacity_gate(
            $storeid, $scfg, int(($allocated_bytes + 1023) / 1024),
            $geometry->{metadata_bytes} + 8 * 1024 * 1024,
        );
        my $lvs = $class->_thick_list_volumes_scoped($scfg, $vg, $device);
        my $objects = $lvs->{$vg} // {};
        if ($class->_thick_guest_name_occupied(
            $namespace, $name, $objects,
        )) {
            $name = $class->_thick_select_fresh_guest_name(
                $namespace, $vmid, $name, $objects,
            );
        }
        $anchor = anchor_name($namespace, $name);
        $data = generation_name($namespace, $name, 0);
        $metadata = sprintf('sltg-m-%s-%08d', object_key($namespace, $name), 0);
        $class->_thick_require_fresh_object_names(
            $vg, $objects, $name, $anchor, $data,
        );
        die "refusing Lazy Thick allocation: metadata name collision at '$vg/$metadata'\n"
            if exists($objects->{$metadata});

        %intent = (
            tx => $tx, state => 'OPEN', op => 'ALLOC', object => $anchor,
            before => $class->_vg_state_digest($scfg, $vg, $device),
        );
        $class->_set_vg_intent($scfg, $vg, %intent, _device => $device);

        my $data_tags = lazy_object_tags(
            sid => $storeid, vol => $name, tx => $tx, kind => 'data',
            bytes => $allocated_bytes, region => $geometry->{region_sectors},
        );
        my @data_create = (
            '/sbin/lvcreate', '--yes', '--wipesignatures', 'n',
            '--ignoreactivationskip', '--devices', $device,
            '-L', int($allocated_bytes / 1024) . 'K', '-n', $data,
            '--setactivationskip', 'y', '--setautoactivation', 'n',
        );
        push @data_create, map { ('--addtag', $_) } @$data_tags;
        push @data_create, $vg;
        $class->_thick_create_lv_exact(
            $scfg, $device, \@data_create,
            "creating Lazy Thick data LV '$vg/$data' failed",
            sub { $class->_thick_verify_created_lv_exact(
                $scfg, $vg, $data, $device, $allocated_bytes,
                sub { validate_lazy_object_tags(
                    $_[0], sid => $storeid, vol => $name, tx => $tx,
                    kind => 'data', bytes => $allocated_bytes,
                    region => $geometry->{region_sectors},
                ) },
            ) },
        );

        my $metadata_tags = lazy_object_tags(
            sid => $storeid, vol => $name, tx => $tx, kind => 'metadata',
            bytes => $geometry->{metadata_bytes},
            region => $geometry->{region_sectors},
        );
        my @metadata_create = (
            '/sbin/lvcreate', '--yes', '--wipesignatures', 'y',
            '--ignoreactivationskip', '--devices', $device,
            '-L', $geometry->{metadata_bytes} . 'B', '-n', $metadata,
            '--setactivationskip', 'y', '--setautoactivation', 'n',
        );
        push @metadata_create, map { ('--addtag', $_) } @$metadata_tags;
        push @metadata_create, $vg;
        $class->_thick_create_lv_exact(
            $scfg, $device, \@metadata_create,
            "creating Lazy Thick metadata LV '$vg/$metadata' failed",
            sub { $class->_thick_verify_created_lv_exact(
                $scfg, $vg, $metadata, $device, $geometry->{metadata_bytes},
                sub { validate_lazy_object_tags(
                    $_[0], sid => $storeid, vol => $name, tx => $tx,
                    kind => 'metadata', bytes => $geometry->{metadata_bytes},
                    region => $geometry->{region_sectors},
                ) },
            ) },
        );

        $lvs = $class->_thick_list_volumes_scoped($scfg, $vg, $device);
        my $data_info = $lvs->{$vg}->{$data};
        my $metadata_info = $lvs->{$vg}->{$metadata};
        die "Lazy Thick allocation cannot prove exact backing LV UUIDs\n"
            if !$data_info || !$metadata_info
            || ($data_info->{lv_uuid} // '') eq ''
            || ($metadata_info->{lv_uuid} // '') eq '';
        my $anchor_tags = PVE::SharedLvmThinThick::anchor_tags(
            v => 6, sid => $storeid, vol => $name,
            phase => 'LAZY_PREPARED', tx => $tx, op => 'ALLOC',
            snapshot => 'none', source => $data, old => $data,
            new => $data, head => $data, generation => 0,
            region => $geometry->{region_sectors}, policy => 'lazy-zero',
            bytes => $allocated_bytes, metadata => $metadata,
            data_uuid => $data_info->{lv_uuid},
            metadata_uuid => $metadata_info->{lv_uuid}, zero_source => 'dm-zero',
            publication => 0, owner_node => 'none', owner_boot => 'none',
            owner_epoch => 'none',
        );
        my @anchor_create = (
            '/sbin/lvcreate', '--yes', '--wipesignatures', 'y',
            '--ignoreactivationskip', '--devices', $device,
            '-L', '8M', '-n', $anchor,
            '--setactivationskip', 'y', '--setautoactivation', 'n',
        );
        push @anchor_create, map { ('--addtag', $_) } @$anchor_tags;
        push @anchor_create, $vg;
        $class->_thick_create_lv_exact(
            $scfg, $device, \@anchor_create,
            "creating Lazy Thick anchor '$vg/$anchor' failed",
            sub { $class->_thick_verify_created_lv_exact(
                $scfg, $vg, $anchor, $device, 8 * 1024 * 1024,
                sub {
                    my $state = decode_anchor_tags($_[0]);
                    die "created Lazy Thick anchor identity mismatch\n"
                        if $state->{v} != 6 || $state->{sid} ne $storeid
                        || $state->{vol} ne $name || $state->{tx} ne $tx
                        || $state->{phase} ne 'LAZY_PREPARED'
                        || $state->{data_uuid} ne $data_info->{lv_uuid}
                        || $state->{metadata_uuid} ne $metadata_info->{lv_uuid};
                    return 1;
                },
            ) },
        );
        $class->_thick_read_anchor($storeid, $scfg, $name);
        return;
    }, $device);

    eval {
        $class->_thick_activate_exact_lvs(
            $scfg, $vg, $device,
            "activating Lazy Thick metadata '$vg/$metadata' for initialization failed",
            $metadata,
        );
        $class->_zero_new_thick_generation(
            "/dev/$vg/$metadata", $geometry->{metadata_bytes},
            "Lazy Thick metadata '$vg/$metadata'",
        );
        run_command(['/sbin/blockdev', '--flushbufs', "/dev/$vg/$metadata"],
            errmsg => "flushing Lazy Thick metadata '$vg/$metadata' failed");
        $class->_thick_deactivate_exact_lvs(
            $scfg, $vg, $device,
            "deactivating initialized Lazy Thick metadata '$vg/$metadata' failed",
            $metadata,
        );
    };
    if (my $error = $@) {
        die _partial_allocation_error(
            $storeid, $vmid, $name, $anchor, $error,
            "LAZY_PREPARED data and metadata preserved with OPEN VG intent",
        );
    }

    $class->_with_vg_lock($storeid, $scfg, sub {
        $class->_require_exact_vg_intent($scfg, $vg, %intent, _device => $device);
        my ($state) = $class->_thick_read_anchor($storeid, $scfg, $name);
        die "Lazy Thick allocation publication state changed unexpectedly\n"
            if $state->{phase} ne 'LAZY_PREPARED' || $state->{tx} ne $tx;
        $class->_thick_transition_anchor(
            $scfg, $vg, $anchor, $state,
            phase => 'LAZY_DORMANT', publication => 1, _device => $device,
        );
        my ($published) = $class->_thick_read_anchor($storeid, $scfg, $name);
        die "Lazy Thick allocation publication postcondition failed\n"
            if $published->{phase} ne 'LAZY_DORMANT'
            || int($published->{publication}) != 1;
        $class->_clear_vg_intent($scfg, $vg, %intent, _device => $device);
        $class->_lazy_record_move_allocation($storeid, $name, $move_context, $published);
        return;
    }, $device);
    return $name;
}

sub _verify_storage_identity {
    my ($class, $storeid, $scfg, $device) = @_;

    my $expected_vg = $scfg->{'slt-expected-vg-uuid'};
    my $expected_pv = $scfg->{'slt-expected-pv-uuid'};
    my $expected_wwid = $scfg->{'slt-expected-wwid'};

    return 1 if !defined($expected_vg)
        && !defined($expected_pv)
        && !defined($expected_wwid);

    my $vg = $scfg->{'slt-vgname'};
    my @vg_command = ('/sbin/vgs', '--readonly');
    push @vg_command, ('--devices', $device) if defined($device);
    push @vg_command, ('--noheadings', '-o', 'vg_uuid', $vg);
    my $vg_lines = _command_lines(
        \@vg_command,
        "reading identity of VG '$vg' failed",
    );

    die "storage '$storeid' identity is ambiguous: expected exactly one VG '$vg'\n"
        if @$vg_lines != 1;

    die "storage '$storeid' VG UUID mismatch: expected '$expected_vg', found '$vg_lines->[0]'\n"
        if defined($expected_vg) && lc($vg_lines->[0]) ne lc($expected_vg);

    my @pv_command = ('/sbin/pvs', '--readonly');
    push @pv_command, ('--devices', $device) if defined($device);
    push @pv_command, ('--noheadings', '--separator', '|', '-o', 'pv_uuid,pv_name',
        '--select', "vg_name=$vg");
    my $pv_lines = _command_lines(
        \@pv_command,
        "reading backing PV identity of VG '$vg' failed",
    );

    die "storage '$storeid' identity is ambiguous: expected exactly one backing PV for '$vg'\n"
        if @$pv_lines != 1;

    my ($pv_uuid, $pv_name) = split(/\|/, $pv_lines->[0], 2);
    for ($pv_uuid, $pv_name) {
        $_ //= '';
        s/^\s+|\s+$//g;
    }

    die "storage '$storeid' PV UUID mismatch: expected '$expected_pv', found '$pv_uuid'\n"
        if defined($expected_pv) && lc($pv_uuid) ne lc($expected_pv);

    if (defined($expected_wwid)) {
        my ($actual_wwid) = $pv_name =~ m{^/dev/mapper/([0-9A-Fa-f]+)$};
        die "storage '$storeid' cannot verify WWID: PV '$pv_name' is not a stable /dev/mapper/<WWID> path\n"
            if !defined($actual_wwid);
        die "storage '$storeid' WWID mismatch: expected '$expected_wwid', found '$actual_wwid'\n"
            if lc($actual_wwid) ne lc($expected_wwid);
    }

    return 1;
}

sub _verify_owned_volume {
    my ($class, $storeid, $scfg, $volname, %options) = @_;
    my $vg = $scfg->{'slt-vgname'};
    my (undef, undef, $vmid) = $class->parse_volname($volname);
    my $pool = "sltp-$vmid";
    my $expected_tag = "pve-slt-sid-$storeid";
    my $lvs = PVE::Storage::LVMPlugin::lvm_list_volumes($vg);
    my $vg_lvs = $lvs->{$vg} // {};
    my $pool_info = $vg_lvs->{$pool};
    my $volume_info = $vg_lvs->{$volname};

    die "refusing to mutate '$vg/$volname': ownership pool '$pool' is missing\n" if !$pool_info;
    die "refusing to mutate '$vg/$volname': '$pool' is not a thin pool\n"
        if !defined($pool_info->{lv_type}) || $pool_info->{lv_type} ne 't';
    $class->_verify_pool_health($vg, $pool, %options);
    my $tags = $pool_info->{tags} // '';
    die "refusing to mutate '$vg/$volname': pool ownership is not positively proven for storage '$storeid'\n"
        if $tags !~ /(?:^|,)\Q$expected_tag\E(?:,|$)/;
    die "refusing to mutate '$vg/$volname': volume is missing\n" if !$volume_info;
    die "refusing to mutate '$vg/$volname': volume is not in expected pool '$pool'\n"
        if !defined($volume_info->{pool_lv}) || $volume_info->{pool_lv} ne $pool;
    return 1;
}

sub _verify_pool_health {
    my ($class, $vg, $pool, %options) = @_;
    my $lines = _command_lines(
        [
            '/sbin/lvs', '--noheadings', '--separator', '|',
            '-o', 'lv_attr,lv_health_status,lv_check_needed,data_percent,zero', "$vg/$pool",
        ],
        "reading thin-pool health of '$vg/$pool' failed",
    );
    die "CRITICAL: thin-pool health of '$vg/$pool' is ambiguous; mutations disabled; manual recovery required\n"
        if @$lines != 1;
    my ($attr, $health, $check_needed, $data_percent, $zero) =
        split(/\|/, $lines->[0], 5);
    for ($attr, $health, $check_needed, $data_percent, $zero) {
        $_ //= '';
        s/^\s+|\s+$//g;
    }
    my $decision = PVE::SharedLvmThinSafety::evaluate_pool_health(
        lv_attr => $attr,
        health_status => $health,
        check_needed => $check_needed,
    );
    die "CRITICAL: '$vg/$pool': $decision->{reason}; mutations disabled; data preserved; manual recovery required\n"
        if $decision->{blocks_operation};
    die "CRITICAL: '$vg/$pool' Data% is malformed; mutations disabled until capacity can be proven\n"
        if $data_percent ne '' && $data_percent !~ /^\d+(?:\.\d+)?$/;
    die "CRITICAL: '$vg/$pool' Data% is $data_percent; mutations disabled below the 5% free-space safety boundary\n"
        if !$options{allow_capacity_teardown}
        && $data_percent ne '' && $data_percent >= 95;
    # LVM has emitted more than one documented/legacy positive spelling for
    # this report field.  Modern lvmthin(7) documents "zero" or "1", while
    # older qualified hosts return "y".  Require the independent `z` flag in
    # the eighth lv_attr position as well; a missing or contradictory signal
    # remains UNKNOWN and therefore fails closed.
    my $zero_field_enabled = $zero eq 'zero' || $zero eq '1' || $zero eq 'y';
    my $zero_attr_enabled = length($attr) == 10
        && substr($attr, 0, 1) eq 't'
        && substr($attr, 7, 1) eq 'z';
    die "CRITICAL: '$vg/$pool' does not prove enabled thin-block zeroing (zero='$zero', lv_attr='$attr'); "
        . "activation and mutations disabled to prevent cross-volume data disclosure\n"
        if !$zero_field_enabled || !$zero_attr_enabled;
    return 1;
}

sub _thin_local_node {
    require PVE::INotify;
    my $node = PVE::INotify::nodename();
    die "cannot determine local PVE node for shared thin-pool ownership\n"
        if !defined($node) || $node !~ /^([A-Za-z0-9][A-Za-z0-9_.-]*)$/;
    return $1;
}

use constant THIN_OWNER_SCHEMA_TAG => 'pve-slt-owner-v1';

sub _thin_owner_state_from_tags {
    my ($tags) = @_;
    $tags //= '';
    my (@owners, @epochs);
    my $schema = 0;
    for my $tag (split(/,/, $tags)) {
        next if $tag eq '';
        if ($tag eq THIN_OWNER_SCHEMA_TAG) {
            $schema++;
            next;
        }
        if ($tag =~ /^pve-slt-owner-node-([A-Za-z0-9][A-Za-z0-9_.-]*)$/) {
            push @owners, $1;
            next;
        }
        if ($tag =~ /^pve-slt-owner-epoch-([0-9a-f]{32})$/) {
            push @epochs, $1;
            next;
        }
        die "malformed shared thin-pool owner tag '$tag'\n"
            if $tag =~ /^pve-slt-owner-/;
    }
    die "ambiguous shared thin-pool ownership: duplicate schema or owner tags found\n"
        if $schema > 1 || @owners > 1 || @epochs > 1;
    die "ambiguous shared thin-pool ownership: owner node and epoch must exist together\n"
        if @owners != @epochs;
    return {
        schema => $schema ? 1 : 0,
        owner => $owners[0],
        epoch => $epochs[0],
    };
}

sub _thin_owner_from_tags {
    return _thin_owner_state_from_tags($_[0])->{owner};
}

sub _thin_pool_tags {
    my ($class, $vg, $pool, $device) = @_;
    my @command = ('/sbin/lvs', '--readonly');
    push @command, ('--devices', $device) if defined($device);
    push @command, ('--noheadings', '--separator', '|', '-o', 'lv_name,lv_tags',
        "$vg/$pool");
    my $lines = _command_lines(
        \@command,
        "reading shared thin-pool ownership of '$vg/$pool' failed",
    );
    die "shared thin-pool ownership of '$vg/$pool' is ambiguous\n"
        if @$lines != 1;
    my ($name, $tags) = split(/\|/, $lines->[0], 2);
    for ($name, $tags) {
        $_ //= '';
        s/^\s+|\s+$//g;
    }
    die "shared thin-pool ownership query returned unexpected object '$name'\n"
        if $name ne $pool;
    return $tags;
}

sub _thin_pool_dm_identity {
    my ($class, $vg, $pool, $device) = @_;
    my @command = ('/sbin/lvs', '--readonly');
    push @command, ('--devices', $device) if defined($device);
    push @command, ('--noheadings', '--separator', '|', '-o',
        'vg_uuid,lv_uuid', "$vg/$pool");
    my $lines = _command_lines(
        \@command,
        "reading thin-pool DM identity of '$vg/$pool' failed",
    );
    die "thin-pool DM identity of '$vg/$pool' is ambiguous\n"
        if @$lines != 1;
    my ($vg_uuid, $lv_uuid) = split(/\|/, $lines->[0], 2);
    for ($vg_uuid, $lv_uuid) {
        $_ //= '';
        s/^\s+|\s+$//g;
        die "thin-pool DM identity contains an invalid UUID\n"
            if !/^[A-Za-z0-9-]+$/;
        s/-//g;
    }
    # Restore the canonical LVM representation for the guardian inventory.
    my (undef, $raw_lv_uuid) = split(/\|/, $lines->[0], 2);
    $raw_lv_uuid //= '';
    $raw_lv_uuid =~ s/^\s+|\s+$//g;
    my $vg_dm = $vg;
    my $pool_dm = $pool;
    $vg_dm =~ s/-/--/g;
    $pool_dm =~ s/-/--/g;
    return ("$vg_dm-$pool_dm-tpool", "LVM-$vg_uuid$lv_uuid-tpool", $raw_lv_uuid);
}

sub _thin_configured_peer_nodes {
    my ($class, $scfg, $fenced_owner) = @_;
    # The restricted Thick-only profile neither loads nor ships peer-audit
    # logic. Resolve it only after a Thin LeaseGuard path is actually entered.
    require PVE::SharedLvmThinPeerAudit;
    PVE::Cluster::cfs_update();
    my $members = PVE::Cluster::get_members();
    my $nodes = $scfg->{nodes};
    if (defined($fenced_owner)) {
        die "fenced Thin owner node is malformed\n"
            if $fenced_owner !~ /^[A-Za-z0-9][A-Za-z0-9_.-]*$/;
        if (ref($nodes) eq 'HASH') {
            $nodes = {%$nodes};
            delete $nodes->{$fenced_owner};
        } elsif (defined($nodes) && !ref($nodes)) {
            $nodes = join(',', grep { $_ ne $fenced_owner } split(/,/, $nodes));
        } elsif (!defined($nodes)) {
            $nodes = {map { $_ => ($_ ne $fenced_owner ? 1 : 0) } keys %$members};
        }
    }
    return PVE::SharedLvmThinPeerAudit::select_peer_nodes(
        members => $members,
        local_node => $class->_thin_local_node(),
        nodes => $nodes,
    );
}

sub _thin_remote_mapper_audit_locked {
    my ($class, $scfg, $vg, $pool, $device, $fenced_owner) = @_;
    my $mode = $scfg->{'slt-thin-leaseguard'} // 'disabled';
    die "invalid slt-thin-leaseguard mode '$mode'\n"
        if $mode ne 'disabled' && $mode ne 'remote-audit' && $mode ne 'runtime-guard';
    return 1 if $mode eq 'disabled';
    # Require at the operation boundary as well as in peer selection.  This
    # keeps an overridden/test peer selector from accidentally bypassing the
    # lazy dependency proof before evidence evaluation.
    require PVE::SharedLvmThinPeerAudit;

    my ($connect_timeout, $probe_timeout) =
        $class->_thin_peer_probe_timing($scfg);

    my ($mapper, $expected_uuid) =
        $class->_thin_pool_dm_identity($vg, $pool, $device);
    my $peers = $class->_thin_configured_peer_nodes($scfg, $fenced_owner);
    my @evidence;
    for my $peer (@$peers) {
        my $ssh = PVE::SSHInfo::ssh_info_to_command({
            name => $peer->{node}, ip => $peer->{ip},
        }, '-o', "ConnectTimeout=$connect_timeout", '-o', 'BatchMode=yes',
            '-o', 'NumberOfPasswordPrompts=0');
        push @$ssh, '--',
            '/usr/libexec/pve-sharedlvmthin/sharedlvmthin-remote-thin-evidence',
            $mapper, $expected_uuid;
        my (@stdout, @stderr);
        eval {
            run_command(
                $ssh,
                timeout => $probe_timeout,
                outfunc => sub { push @stdout, $_[0] },
                errfunc => sub { push @stderr, $_[0] },
            );
        };
        die "PVE-native LeaseGuard cannot prove mapper absence on '$peer->{node}': $@\n"
            if $@;
        die "PVE-native LeaseGuard received ambiguous evidence from '$peer->{node}'\n"
            if @stdout != 1
            || $stdout[0] !~ /^BASTRIX_REMOTE_THIN_V1\|(ABSENT|PRESENT)\|\Q$mapper\E\|\Q$expected_uuid\E$/;
        push @evidence, { node => $peer->{node}, line => $stdout[0] };
    }
    my $decision = PVE::SharedLvmThinPeerAudit::evaluate_peer_mapper_evidence(
        mapper => $mapper,
        mapper_uuid => $expected_uuid,
        evidence => \@evidence,
    );
    die "UNSAFE shared LVM-thin activation refused: $decision->{reason}\n" if !$decision->{safe};
    return 1;
}

sub _thin_claim_pool_owner_locked {
    my ($class, $vg, $pool, $device, $scfg) = @_;
    my $node = $class->_thin_local_node();
    my $state = _thin_owner_state_from_tags(
        $class->_thin_pool_tags($vg, $pool, $device),
    );
    die "UNSAFE shared LVM-thin activation refused: pool '$vg/$pool' predates the exclusive-owner schema; stop/deactivate it on every node and run the explicit thin-adopt-owner-model procedure\n"
        if !$state->{schema};
    my $owner = $state->{owner};
    my $fenced_owner;
    if (defined($owner) && $owner ne $node
        && ($scfg->{'slt-thin-ha-takeover'} // 'disabled') eq 'pve-ha') {
        die "automatic PVE HA Thin takeover requires remote-audit or runtime-guard\n"
            if ($scfg->{'slt-thin-leaseguard'} // 'disabled') eq 'disabled';
        $class->_thin_pve_ha_takeover_evidence($pool, $owner, $node);
        $fenced_owner = $owner;

        # Preserve the durable recovery context until every remaining peer has
        # positively proved absence of the exact mapper.  Clearing the old
        # owner first makes a failed audit non-retryable: the next invocation
        # can no longer identify the one node which PVE has fenced.  Therefore
        # the audit is a precondition of the tag transition, not a
        # postcondition of it.
        $class->_thin_remote_mapper_audit_locked(
            $scfg, $vg, $pool, $device, $fenced_owner);

        my @release = ('/sbin/lvchange');
        push @release, ('--devices', $device) if defined($device);
        push @release, ('--deltag', "pve-slt-owner-node-$owner",
            '--deltag', "pve-slt-owner-epoch-$state->{epoch}", "$vg/$pool");
        run_command(\@release,
            errmsg => "clearing PVE HA fenced Thin owner '$owner' failed");
        $state = _thin_owner_state_from_tags(
            $class->_thin_pool_tags($vg, $pool, $device));
        die "PVE HA fenced Thin owner clear postcondition failed for '$vg/$pool'\n"
            if !$state->{schema} || defined($state->{owner}) || defined($state->{epoch});
        $owner = undef;
    }
    die "UNSAFE shared LVM-thin activation refused: pool '$vg/$pool' is owned by node '$owner', not '$node'; concurrent dm-thin activation can corrupt metadata; direct in-place Thin live migration is unsupported (use the Materialized Migration Bridge for online VM migration)\n"
        if defined($owner) && $owner ne $node;
    if (defined($owner)) {
        # A durable local owner is necessary but not sufficient evidence for
        # reactivation.  A previous partial handoff, manual lvchange, stale
        # dmeventd instance, or older plugin may have left the exact pool
        # mapper loaded in another kernel without changing the owner tags.
        # Re-audit every configured peer before every guarded activation;
        # never treat an existing local epoch as a shortcut around the
        # single-kernel invariant.
        $class->_thin_remote_mapper_audit_locked(
            $scfg, $vg, $pool, $device, undef,
        ) if defined($scfg);
        return $state->{epoch};
    }

    # An unowned pool must also be absent from this kernel.  Generic LVM
    # autoactivation or a leaked dmeventd mapping can otherwise load the same
    # thin metadata before the durable owner is claimed.  Never bless that
    # mapper retroactively: require explicit offline hardening/recovery.
    my $runtime = $class->_thin_pool_runtime_state($vg, $pool, $device);
    die "UNSAFE shared LVM-thin activation refused: unowned pool '$vg/$pool' already has local runtime mappings; concurrent or automatic metadata activation is possible; run the explicit ALL-NODES-INACTIVE hardening procedure\n"
        if $runtime->{pool_active} || $runtime->{pool_mapper_active}
        || @{$runtime->{active_children}};

    # A fenced-owner takeover was audited before its durable owner tag was
    # removed.  Ordinary unowned activation still requires the full peer set.
    $class->_thin_remote_mapper_audit_locked(
        $scfg, $vg, $pool, $device, undef)
        if defined($scfg) && !defined($fenced_owner);

    my $epoch = _new_transaction_id();
    my @claim = ('/sbin/lvchange');
    push @claim, ('--devices', $device) if defined($device);
    push @claim, ('--addtag', "pve-slt-owner-node-$node",
        '--addtag', "pve-slt-owner-epoch-$epoch", "$vg/$pool");
    run_command(
        \@claim,
        errmsg => "claiming exclusive shared thin-pool ownership of '$vg/$pool' failed",
    );
    my $after = _thin_owner_state_from_tags(
        $class->_thin_pool_tags($vg, $pool, $device),
    );
    die "shared thin-pool ownership claim postcondition failed for '$vg/$pool'\n"
        if !$after->{schema} || !defined($after->{owner})
        || $after->{owner} ne $node || $after->{epoch} ne $epoch;
    return $epoch;
}

sub _thin_pve_ha_takeover_evidence {
    my ($class, $pool, $owner, $local) = @_;
    die "PVE HA Thin takeover received an invalid pool name\n"
        if $pool !~ /^sltp-([1-9][0-9]*)$/;
    my $sid = "vm:$1";

    require PVE::HA::Config;
    my $resources = PVE::HA::Config::read_resources_config();
    my $resource = $resources->{ids}->{$sid};
    die "PVE HA Thin takeover refused: '$sid' is not HA managed\n"
        if ref($resource) ne 'HASH';
    my $requested = $resource->{state} // 'started';
    die "PVE HA Thin takeover refused: '$sid' requested state is '$requested'\n"
        if $requested ne 'started' && $requested ne 'enabled';

    my $manager = PVE::HA::Config::read_manager_status();
    die "PVE HA Thin takeover refused: manager status is unavailable\n"
        if ref($manager) ne 'HASH';
    my $timestamp = $manager->{timestamp};
    die "PVE HA Thin takeover refused: manager status is stale or from the future\n"
        if !defined($timestamp) || $timestamp !~ /^\d+$/
        || time() - $timestamp > 30 || $timestamp - time() > 5;
    my $service = $manager->{service_status}->{$sid};
    my $service_node = ref($service) eq 'HASH' ? ($service->{node} // '') : '';
    my $service_state = ref($service) eq 'HASH' ? ($service->{state} // '') : '';
    die "PVE HA Thin takeover refused: manager did not assign '$sid' to '$local' for start (node='$service_node', state='$service_state')\n"
        if $service_node ne $local || $service_state !~ /^(?:started|starting|migrate)$/;
    die "PVE HA Thin takeover refused: target node '$local' is not online\n"
        if ($manager->{node_status}->{$local} // '') ne 'online';
    die "PVE HA Thin takeover refused: former owner '$owner' is not in the fenced/offline HA state\n"
        if ($manager->{node_status}->{$owner} // '') !~ /^(?:unknown|fence)$/;
    return 1;
}

sub _thin_runtime_guard_request {
    my ($class, $scfg, $op, %request) = @_;
    return 1 if ($scfg->{'slt-thin-leaseguard'} // 'disabled') ne 'runtime-guard';
    # Thick-only never loads or ships the ThinGuard client.  Keep this require
    # at the first runtime-guard use so the shared plugin core remains loadable
    # without any Thin runtime component in the restricted package profile.
    require PVE::SharedLvmThinGuardClient;
    # PREPARE repeats the exact peer proof inside the watchdog guardian.  Its
    # client wait must not be shorter than the guardian's bounded inventory
    # process or a healthy loaded proof would become transport ambiguity.
    my $request_timeout = $op eq 'PREPARE' ? 1310 : 35;
    my $client = PVE::SharedLvmThinGuardClient->new(
        allow_real_socket => 1, request_timeout => $request_timeout,
    );
    my $action = $op eq 'PREPARE' ? 'ACK_PREPARED'
        : $op eq 'RELEASE' ? ['CLEAN_DISARM', 'REFRESH_WATCHDOG']
        : die "invalid ThinGuard operation\n";
    return $client->request({
        version => 1,
        op => $op,
        request_id => _new_transaction_id(),
        %request,
    }, $action);
}

sub _thin_activation_commit_barrier_locked {
    my ($class, $scfg, $vg, $pool, $device, $epoch) = @_;
    die "Thin activation commit barrier received a malformed owner epoch\n"
        if !defined($epoch) || $epoch !~ /^[a-f0-9]{32}$/;

    $class->_thin_remote_mapper_audit_locked(
        $scfg, $vg, $pool, $device, undef,
    );
    my $commit_owner = _thin_owner_state_from_tags(
        $class->_thin_pool_tags($vg, $pool, $device),
    );
    my $local_node = $class->_thin_local_node();
    die "UNSAFE shared LVM-thin activation refused: owner epoch changed before local activation of '$vg/$pool'\n"
        if !$commit_owner->{schema}
        || !defined($commit_owner->{owner})
        || $commit_owner->{owner} ne $local_node
        || !defined($commit_owner->{epoch})
        || $commit_owner->{epoch} ne $epoch;
    return 1;
}

sub _thin_release_pool_owner_locked {
    my ($class, $vg, $pool, $device, $foreign_cleanup_ok) = @_;
    my $node = $class->_thin_local_node();
    my $state = _thin_owner_state_from_tags(
        $class->_thin_pool_tags($vg, $pool, $device),
    );
    # Idempotent teardown of an already-inactive legacy pool is safe and is
    # needed by PVE's failed-start cleanup path.  This never authorizes a new
    # activation: the claim path still requires the schema positively.
    return 1 if !$state->{schema}
        && !defined($state->{owner}) && !defined($state->{epoch});
    die "refusing to release shared thin-pool ownership of '$vg/$pool': owner schema is absent\n"
        if !$state->{schema};
    my $owner = $state->{owner};
    return 1 if $foreign_cleanup_ok && defined($owner) && $owner ne $node;
    die "refusing to release shared thin-pool ownership of '$vg/$pool': owner is '$owner', local node is '$node'\n"
        if defined($owner) && $owner ne $node;
    return 1 if !defined($owner);

    my @release = ('/sbin/lvchange');
    push @release, ('--devices', $device) if defined($device);
    push @release, ('--deltag', "pve-slt-owner-node-$node",
        '--deltag', "pve-slt-owner-epoch-$state->{epoch}", "$vg/$pool");
    run_command(
        \@release,
        errmsg => "releasing exclusive shared thin-pool ownership of '$vg/$pool' failed",
    );
    my $after = _thin_owner_state_from_tags(
        $class->_thin_pool_tags($vg, $pool, $device),
    );
    die "shared thin-pool ownership release postcondition failed for '$vg/$pool'\n"
        if !$after->{schema} || defined($after->{owner}) || defined($after->{epoch});
    return 1;
}

sub _forced_single_node_quorum_from_evidence {
    my ($configured_nodes, $online_nodes, $expected_votes, $qdevice_configured) = @_;
    die "cluster vote evidence is incomplete; shared mutation refused\n"
        if !defined($configured_nodes) || !defined($online_nodes)
        || !defined($expected_votes) || !defined($qdevice_configured);
    die "cluster vote evidence is invalid; shared mutation refused\n"
        if $configured_nodes !~ /^\d+$/ || $online_nodes !~ /^\d+$/
        || $expected_votes !~ /^\d+$/ || $qdevice_configured !~ /^(?:0|1)$/
        || $configured_nodes < 1 || $online_nodes < 1
        || $online_nodes > $configured_nodes || $expected_votes < 1;
    return $configured_nodes > 1
        && $online_nodes == 1
        && $expected_votes == 1
        && !$qdevice_configured;
}

sub _verify_no_forced_single_node_quorum {
    return 1 if !-e '/etc/pve/corosync.conf';

    open(my $fh, '<', '/etc/pve/corosync.conf')
        or die "cluster configuration is unreadable; shared mutation refused\n";
    local $/;
    my $config = <$fh> // '';
    close($fh);

    my $configured_nodes = () = $config =~ /^\s*node\s*\{/mg;
    return 1 if $configured_nodes < 2;
    my $qdevice_configured = $config =~ /^\s*quorum\s*\{.*?^\s*device\s*\{/ms ? 1 : 0;

    my $lines = _command_lines(
        ['/usr/bin/pvecm', 'status'],
        'reading cluster vote state failed',
    );
    my $status = join("\n", @$lines);
    my ($online_nodes) = $status =~ /^Nodes:\s+(\d+)/m;
    my ($expected_votes) = $status =~ /^Expected votes:\s+(\d+)/m;

    die "cluster vote status is incomplete; shared mutation refused\n"
        if !defined($online_nodes) || !defined($expected_votes);

    die "CRITICAL: forced single-node quorum (expected_votes=1) detected in a multi-node cluster; SharedLvmThin mutations refused\n"
        if _forced_single_node_quorum_from_evidence(
            $configured_nodes, $online_nodes, $expected_votes, $qdevice_configured,
        );
    return 1;
}

sub _verify_mutation_quorum {
    my ($class, $storeid, $scfg) = @_;
    my ($runtime_api, $runtime_error);
    {
        local $@;
        $runtime_api = eval {
            $class->_runtime_storage_api();
        };
        $runtime_error = $@;
    }
    die "unable to determine running PVE Storage API; mutation refused\n"
        if !defined($runtime_api) || $runtime_error;
    die "running PVE Storage API $runtime_api is outside the tested SharedLvmThin range "
        . MIN_TESTED_PVE_STORAGE_API . '..' . MAX_TESTED_PVE_STORAGE_API
        . "; mutation refused\n"
        if $runtime_api < MIN_TESTED_PVE_STORAGE_API
        || $runtime_api > MAX_TESTED_PVE_STORAGE_API;
    return 1 if !$scfg->{shared};
    PVE::Cluster::check_cfs_quorum();
    _verify_no_forced_single_node_quorum();
    return 1;
}

sub _partial_allocation_error {
    my ($storeid, $vmid, $name, $pool, $reason, $object_state) = @_;
    chomp($reason //= 'unknown allocation failure');
    return join(
        "\n",
        'PARTIAL ALLOCATION',
        "Storage: $storeid",
        "VMID: $vmid",
        "Object: $name",
        "Pool: $pool",
        "Preserved state: $object_state",
        'Automatic cleanup was intentionally NOT performed.',
        "Reason: $reason",
        'Existing objects were preserved to protect data.',
        'Required action: verify VG UUID, PV UUID, WWID, ownership tags, PVE references, quorum, and the storage lock before any cleanup.',
        '',
    );
}

sub _verify_resize_postcondition {
    my ($class, $scfg, $volname, $expected_size) = @_;
    my $vg = $scfg->{'slt-vgname'};
    my $lvs = PVE::Storage::LVMPlugin::lvm_list_volumes($vg);
    die "resize postcondition unavailable: VG '$vg' is not visible\n" if !$lvs->{$vg};
    my $info = $lvs->{$vg}->{$volname};
    die "resize postcondition failed: '$vg/$volname' is missing\n" if !$info;
    die "resize postcondition failed: '$vg/$volname' size is unknown\n"
        if !defined($info->{lv_size});
    die "resize postcondition failed: '$vg/$volname' is smaller than requested\n"
        if $info->{lv_size} < $expected_size;
    return 1;
}

sub _disable_and_verify_autoactivation {
    my ($class, $vg, $lv, $device) = @_;

    my @command = ('/sbin/lvchange');
    push @command, ('--devices', $device) if defined($device);
    push @command, ('--setautoactivation', 'n', "$vg/$lv");
    run_command(
        \@command,
        errmsg => "disabling autoactivation for '$vg/$lv' failed",
    );

    return $class->_verify_autoactivation_disabled($vg, $lv, $device);
}

sub _verify_autoactivation_disabled {
    my ($class, $vg, $lv, $device) = @_;
    my @command = ('/sbin/lvs', '--readonly');
    push @command, ('--devices', $device) if defined($device);
    push @command, ('--binary', '--noheadings', '-o', 'lv_autoactivation', "$vg/$lv");
    my $lines = _command_lines(
        \@command,
        "reading autoactivation state of '$vg/$lv' failed",
    );
    die "autoactivation postcondition failed: state of '$vg/$lv' is ambiguous\n"
        if @$lines != 1;
    my $state = lc($lines->[0] // '');
    $state =~ s/^\s+|\s+$//g;
    die "autoactivation postcondition failed: '$vg/$lv' has lv_autoactivation='$state', expected '0'\n"
        if $state ne '0';
    return 1;
}

sub _thick_verify_autoactivation_disabled {
    my ($class, $scfg, $vg, $lv, $device) = @_;
    die "Thick autoactivation verification requires an exact mapper device\n"
        if !defined($device) || $device !~ m{^/dev/mapper/[0-9A-Fa-f]+$};
    my $command_timeout = $class->_thick_command_deadline($scfg);
    my $lines = _command_lines(
        ['/usr/bin/timeout', '--foreground', '--kill-after=5s', "${command_timeout}s",
            '/sbin/lvs', '--readonly', '--devices', $device,
            '--binary', '--noheadings', '-o', 'lv_autoactivation', "$vg/$lv"],
        "reading Thick autoactivation state of '$vg/$lv' failed",
    );
    die "Thick autoactivation postcondition failed: state of '$vg/$lv' is ambiguous\n"
        if @$lines != 1;
    my $state = lc($lines->[0] // '');
    $state =~ s/^\s+|\s+$//g;
    die "Thick autoactivation postcondition failed: '$vg/$lv' has "
        . "lv_autoactivation='$state', expected '0'\n"
        if $state ne '0';
    return 1;
}

sub _thick_disable_and_verify_autoactivation {
    my ($class, $scfg, $vg, $lv, $device) = @_;
    die "Thick autoactivation disable requires an exact mapper device\n"
        if !defined($device) || $device !~ m{^/dev/mapper/[0-9A-Fa-f]+$};
    my $command_timeout = $class->_thick_command_deadline($scfg);
    my $errmsg = "disabling Thick autoactivation for '$vg/$lv' failed";
    my $command_error = '';
    eval {
        run_command(
            ['/usr/bin/timeout', '--foreground', '--kill-after=5s', "${command_timeout}s",
                '/sbin/lvchange', '--devices', $device,
                '--setautoactivation', 'n', "$vg/$lv"],
            errmsg => $errmsg,
        );
    };
    $command_error = $@ if $@;

    my $post_error = '';
    eval { $class->_thick_verify_autoactivation_disabled($scfg, $vg, $lv, $device); };
    $post_error = $@ if $@;
    if ($post_error ne '') {
        die $command_error ne ''
            ? "$errmsg; outcome is UNKNOWN because the exact postcondition is unproven; "
                . "no retry attempted: $command_error$post_error"
            : $post_error;
    }
    warn "$errmsg reported an error, but the exact disabled postcondition is proven; "
        . "continuing without retry: $command_error" if $command_error ne '';
    return 1;
}

sub _verify_snapshot_postcondition {
    my ($class, $scfg, $volname, $snapvol, $must_exist) = @_;
    my $vg = $scfg->{'slt-vgname'};
    my (undef, undef, $vmid) = $class->parse_volname($volname);
    my $pool = "sltp-$vmid";
    my $lvs = PVE::Storage::LVMPlugin::lvm_list_volumes($vg);
    die "snapshot postcondition unavailable: VG '$vg' is not visible\n" if !$lvs->{$vg};
    my $info = $lvs->{$vg}->{$snapvol};
    if ($must_exist) {
        die "snapshot postcondition failed: '$vg/$snapvol' is missing\n" if !$info;
        die "snapshot postcondition failed: '$vg/$snapvol' is not in pool '$pool'\n"
            if !defined($info->{pool_lv}) || $info->{pool_lv} ne $pool;
        my $lines = _command_lines(
            ['/sbin/lvs', '--noheadings', '-o', 'lv_attr', "$vg/$snapvol"],
            "reading snapshot flags of '$vg/$snapvol' failed",
        );
        die "snapshot postcondition failed: flags of '$vg/$snapvol' are ambiguous\n"
            if @$lines != 1;
        my $attr = $lines->[0] // '';
        $attr =~ s/^\s+|\s+$//g;
        die "snapshot postcondition failed: '$vg/$snapvol' must be a read-only thin LV with activation-skip (found '$attr')\n"
            if length($attr) < 10
            || substr($attr, 0, 1) ne 'V'
            || substr($attr, 1, 1) ne 'r'
            || substr($attr, 6, 1) ne 't'
            || substr($attr, 9, 1) ne 'k';
    } else {
        die "snapshot delete postcondition failed: '$vg/$snapvol' still exists\n" if $info;
    }
    return 1;
}

sub parse_volname {
    my ($class, $volname) = @_;

    PVE::Storage::Plugin::parse_lvm_name($volname);

    if ($volname =~ m/^((vm|base)-(\d+)-\S+)$/) {
        return ('images', $1, $3, undef, undef, $2 eq 'base', 'raw');
    }

    die "unable to parse shared LVM thin volume name '$volname'\n";
}

sub filesystem_path {
    my ($class, $scfg, $volname, $snapname) = @_;
    return $class->_thick_filesystem_path($scfg, $volname, $snapname)
        if $class->_is_thick_mode($scfg);

    my ($vtype, $name, $vmid) = $class->parse_volname($volname);

    my $vg = $scfg->{'slt-vgname'};

    my $lv =
        defined($snapname)
        ? "snap_${name}_${snapname}"
        : $name;

    my $path = "/dev/$vg/$lv";

    return wantarray ? ($path, $vmid, $vtype) : $path;
}

sub volume_size_info {
    my ($class, $scfg, $storeid, $volname, $timeout) = @_;

    # Every guest volume exposed by this plugin is a raw block device.  Do not
    # use the generic file_size_info() implementation here: qemu-img can omit
    # its format result while QEMU holds an active raw device, which makes the
    # PVE content endpoint return "no format" and forces backup products to
    # retry/fall back to a full inventory scan.  blockdev is read-only and
    # reports the kernel-visible size without opening the device for writing.
    my ($format) = ($class->parse_volname($volname))[6];
    die "unsupported volume format '$format' for '$storeid:$volname'\n"
        if $format ne 'raw';

    # QemuServer activates a named source snapshot before a full clone, but
    # its volume_size_info API does not carry the snapshot name.  A Thick
    # snapshot activation intentionally publishes only the immutable private
    # generation, not the mutable stable HEAD frontend.  Requiring that
    # frontend here therefore makes a supported named-snapshot clone fail on
    # an otherwise healthy, inactive volume.  Read the authenticated
    # anchor->HEAD inventory instead.  Snapshot copy admission below proves
    # that the selected snapshot has this exact geometry before PVE can reach
    # this size call; unequal geometry fails closed before target allocation.
    if ($class->_is_thick_mode($scfg)) {
        my ($state, $head) = $class->_thick_read_anchor(
            $storeid, $scfg, $volname,
        );
        if (($state->{phase} // '') ne 'MATERIALIZED') {
            my $guidance = ($state->{phase} // '') =~ /^(?:LAZY_ACTIVE|LAZY_DORMANT)$/
                ? "; materialize it first with 'sharedlvmthin thick-lazy-materialize "
                    . "$storeid $volname' and retry only after the command and "
                    . "recovery-check both pass"
                : "; do not retry while the transition result is unresolved";
            die "thick-generations size is unavailable while '$storeid:$volname' "
                . "is in phase '$state->{phase}'$guidance\n";
        }
        my $size = int($head->{lv_size} // 0);
        die "thick-generations HEAD size is missing for '$storeid:$volname'\n"
            if $size <= 0;
        return wantarray ? ($size, 'raw', 0, undef) : $size;
    }

    my $path = $class->filesystem_path($scfg, $volname);
    my $size;
    my %options = (
        errmsg => "can't get size of '$path'",
        outfunc => sub {
            my $line = shift;
            die "ambiguous block-device size for '$path'\n" if defined($size);
            die "invalid block-device size for '$path'\n"
                if !defined($line) || $line !~ /^\s*([0-9]+)\s*$/;
            $size = int($1);
        },
    );
    $options{timeout} = $timeout if defined($timeout);
    run_command(['/usr/sbin/blockdev', '--getsize64', $path], %options);
    die "missing block-device size for '$path'\n" if !defined($size) || $size <= 0;

    return wantarray ? ($size, 'raw', 0, undef) : $size;
}

sub activate_storage {
    my ($class, $storeid, $scfg, $cache) = @_;
    # This hook is an identity/inventory inspection only.  It must remain
    # usable while mutation admission is closed so PVE can report storage
    # health during package/runtime qualification.  Every actual volume or VG
    # effect is guarded at its own entry point.  A source-contract regression
    # test rejects effectful commands added to this call graph.
    $class->_assert_loaded_runtime_identity('storage inspection');
    $class->_require_thick_identity_config($storeid, $scfg)
        if $class->_is_thick_mode($scfg);

    my $vg = $scfg->{'slt-vgname'};
    my $inventory = $class->_scoped_vg_status($scfg);
    die "shared LVM VG '$vg' not found\n"
        if $inventory->{state} eq 'ABSENT';

    $class->_verify_storage_identity($storeid, $scfg);
    $class->_verify_same_vg_alias_configuration($storeid, $scfg);
    my $device = defined($scfg->{'slt-expected-wwid'})
        ? "/dev/mapper/$scfg->{'slt-expected-wwid'}" : undef;
    $class->_verify_vg_failure_domain_inventory($storeid, $scfg, $device);

    return 1;
}

sub deactivate_storage {
    return 1;
}

sub status {
    my ($class, $storeid, $scfg, $cache) = @_;
    $class->_allocation_mode($scfg);

    my $info = $class->_scoped_vg_status($scfg);
    return if $info->{state} eq 'ABSENT';
    my $total = $info->{size};
    my $free  = $info->{free};

    return if !defined($total) || !defined($free);

    my $used = $total - $free;

    return ($total, $free, $used, 1);
}

sub _scoped_vg_status {
    my ($class, $scfg) = @_;
    my $vg = $scfg->{'slt-vgname'};
    die "scoped VG status requires a valid VG name\n"
        if !defined($vg) || $vg !~ /^[A-Za-z0-9][A-Za-z0-9+_.-]*$/;

    my $deadline = $class->_thick_command_deadline($scfg);
    my @command = (
        '/usr/bin/timeout', '--foreground', '--kill-after=5s', "${deadline}s",
        '/sbin/vgs', '--readonly', '--reportformat', 'json', '--units', 'b',
        '--nosuffix', '--select', "vg_name=$vg",
        '-o', 'vg_name,vg_uuid,vg_size,vg_free',
    );
    if (defined($scfg->{'slt-expected-wwid'})) {
        my $wwid = $scfg->{'slt-expected-wwid'};
        die "scoped VG status requires a valid expected WWID\n"
            if $wwid !~ /^[0-9A-Fa-f]+$/;
        push @command, '--devices', "/dev/mapper/$wwid";
    }
    my @json;
    run_command(
        \@command,
        outfunc => sub { push @json, $_[0]; },
        errmsg => "reading scoped status of VG '$vg' failed",
    );
    my $report = eval { decode_json(join("\n", @json)) };
    die "scoped status of VG '$vg' is malformed: $@\n"
        if $@ || ref($report) ne 'HASH'
        || ref($report->{report}) ne 'ARRAY' || @{$report->{report}} != 1
        || ref($report->{report}->[0]->{vg}) ne 'ARRAY';
    my $rows = $report->{report}->[0]->{vg};
    return { state => 'ABSENT' } if !@$rows;
    die "scoped status of VG '$vg' is ambiguous\n" if @$rows != 1;
    my $row = $rows->[0];
    die "scoped status of VG '$vg' contains an invalid row\n"
        if ref($row) ne 'HASH' || ($row->{vg_name} // '') ne $vg
        || ($row->{vg_uuid} // '') eq '';
    if (defined($scfg->{'slt-expected-vg-uuid'})) {
        die "scoped status of VG '$vg' UUID mismatch\n"
            if $row->{vg_uuid} ne $scfg->{'slt-expected-vg-uuid'};
    }
    my @values;
    for my $field (qw(vg_size vg_free)) {
        my $value = $row->{$field} // '';
        die "scoped status of VG '$vg' contains invalid $field\n"
            if $value !~ /^(\d+)(?:\.0+)?$/;
        my $integer = $1;
        $integer =~ s/^0+(?=\d)//;
        die "scoped status of VG '$vg' contains overflowing $field\n"
            if length($integer) > 19
            || (length($integer) == 19 && $integer gt '9223372036854775807');
        push @values, int($integer);
    }
    my ($size, $free) = @values;
    die "scoped status of VG '$vg' contains impossible capacity\n"
        if $size <= 0 || $free < 0 || $free > $size;
    return {
        state => 'FOUND',
        size => $size,
        free => $free,
        uuid => $row->{vg_uuid},
    };
}

sub list_images {
    my ($class, $storeid, $scfg, $vmid, $vollist, $cache) = @_;
    return $class->_thick_list_images($storeid, $scfg, $vmid, $vollist, $cache)
        if $class->_is_thick_mode($scfg);

    my $vg = $scfg->{'slt-vgname'};

    my $lvs = PVE::Storage::LVMPlugin::lvm_list_volumes($vg);
    my $res = [];

    return $res if !$lvs->{$vg};

    foreach my $name (sort keys %{$lvs->{$vg}}) {
        next if $name !~ /^vm-(\d+)-disk-\d+$/;

        my $owner = $1;
        next if defined($vmid) && $owner != $vmid;

        my $info = $lvs->{$vg}->{$name};

        # Only expose thin guest LVs living in our per-VM pool.
        my $expected_pool = "sltp-$owner";
        next if !defined($info->{pool_lv});
        next if $info->{pool_lv} ne $expected_pool;

        my $pool_info = $lvs->{$vg}->{$expected_pool};
        next if !$pool_info;
        next if !defined($pool_info->{lv_type}) || $pool_info->{lv_type} ne 't';

        # Tagged pools belonging to another storage must never be exposed.
        # Untagged pools remain visible for RC3 legacy compatibility, but
        # are never silently adopted or automatically removed.
        my $expected_tag = "pve-slt-sid-$storeid";
        my $tags = $pool_info->{tags} // '';
        next if $tags =~ /(?:^|,)pve-slt-sid-[A-Za-z0-9_-]+(?:,|$)/
            && $tags !~ /(?:^|,)\Q$expected_tag\E(?:,|$)/;

        my $volid = "$storeid:$name";

        if ($vollist) {
            my $found = grep { $_ eq $volid } @$vollist;
            next if !$found;
        }

        push @$res, {
            volid  => $volid,
            format => 'raw',
            size   => $info->{lv_size},
            vmid   => $owner,
            ctime  => $info->{ctime},
        };
    }

    return $res;
}

#
# Per-VM thin-pool allocation.
#
sub _allocation_numeric_fields {
    my ($command, $errmsg, $expected, $allow_empty_last) = @_;
    my $lines = _command_lines($command, $errmsg);
    die "$errmsg: expected exactly one result\n" if @$lines != 1;
    my @fields = split(/\|/, $lines->[0], -1);
    die "$errmsg: expected $expected fields\n" if @fields != $expected;
    for my $index (0 .. $#fields) {
        local $_ = $fields[$index];
        s/^\s+|\s+$//g;
        if ($allow_empty_last && $index == $#fields && $_ eq '') {
            $fields[$index] = undef;
            next;
        }
        die "$errmsg: non-numeric result\n" if !/^\d+(?:\.\d+)?$/;
        $fields[$index] = $_;
    }
    return @fields;
}

sub _allocation_metadata_overhead_bytes {
    my ($target_bytes) = @_;
    my $gib = 1024 * 1024 * 1024;
    my $overhead = int(($target_bytes + 99) / 100); # conservative 1%
    $overhead = $gib if $overhead < $gib;
    $overhead = 32 * $gib if $overhead > 32 * $gib;
    return $overhead;
}

sub _thin_import_state_from_tags {
    my ($tags) = @_;
    my ($schema, @tx, @bytes);
    for my $tag (split(/,/, $tags // '')) {
        $schema++ if $tag eq 'pve-slt-import-v1';
        push @tx, $1 if $tag =~ /^pve-slt-import-tx-([0-9a-f]{32})$/;
        push @bytes, $1 if $tag =~ /^pve-slt-import-bytes-([1-9][0-9]*)$/;
        die "malformed Thin import preparation tag '$tag'\n"
            if $tag =~ /^pve-slt-import-/
            && $tag ne 'pve-slt-import-v1'
            && $tag !~ /^pve-slt-import-(?:tx-[0-9a-f]{32}|bytes-[1-9][0-9]*)$/;
    }
    die "ambiguous Thin import preparation tags\n"
        if ($schema // 0) > 1 || @tx > 1 || @bytes > 1
        || ((($schema // 0) || @tx || @bytes)
            && (($schema // 0) != 1 || @tx != 1 || @bytes != 1));
    return {
        active => ($schema // 0) ? 1 : 0,
        tx => $tx[0],
        bytes => defined($bytes[0]) ? int($bytes[0]) : undef,
    };
}

sub _thin_prepare_import_pool {
    my ($class, $scfg, $storeid, $vmid, $required_bytes, $tx) = @_;
    die "Thin import preparation is unavailable for Thick Generations storage\n"
        if $class->_allocation_mode($scfg) ne 'thin';
    die "invalid Thin import VMID\n"
        if !defined($vmid) || $vmid !~ /^([1-9][0-9]{2,8})$/;
    $vmid = int($1);
    die "invalid Thin import byte requirement\n"
        if !defined($required_bytes) || $required_bytes !~ /^([1-9][0-9]*)$/;
    $required_bytes = int($1);
    die "invalid Thin import transaction\n"
        if !defined($tx) || $tx !~ /^([0-9a-f]{32})$/;
    $tx = $1;

    return $class->_with_mutation_lock($storeid, $scfg, sub {
        $class->_verify_mutation_quorum($storeid, $scfg);
        $class->_verify_storage_identity($storeid, $scfg);
        my $vg = $scfg->{'slt-vgname'};
        my $pool = "sltp-$vmid";
        my $device = defined($scfg->{'slt-expected-wwid'})
            ? "/dev/mapper/$scfg->{'slt-expected-wwid'}" : undef;
        my $lvs = PVE::Storage::LVMPlugin::lvm_list_volumes($vg);
        die "Thin import preparation refused: '$vg/$pool' already exists\n"
            if $lvs->{$vg} && $lvs->{$vg}->{$pool};
        die "Thin import preparation refused: VMID $vmid already has Thin objects\n"
            if $lvs->{$vg} && grep { /^vm-\Q$vmid\E-(?:disk|state|fleece)-/ }
                keys %{$lvs->{$vg}};

        my %full = (%$scfg,
            'slt-initial-pool-mode' => 'full',
            # Import admission is based on the exact aggregate virtual size;
            # do not inherit a legacy 16-GiB bootstrap floor for a smaller VM.
            'slt-initial-pool-size' => 1,
        );
        my $gib = 1024 * 1024 * 1024;
        my $burst_bytes = ($scfg->{'slt-burst-headroom-gib'} // 1) * $gib;
        my $admitted_bytes = $required_bytes + $burst_bytes;
        my $capacity_safe = PVE::SharedLvmThinSafety::minimum_pool_bytes_for_used(
            used_bytes => $required_bytes,
        );
        $admitted_bytes = $capacity_safe if $capacity_safe > $admitted_bytes;
        die "Thin import admitted byte count overflow\n"
            if $admitted_bytes <= $required_bytes;
        my $size_kib = int(($admitted_bytes + 1023) / 1024);
        my $plan = $class->_allocation_headroom_plan(
            $storeid, \%full, $pool, $size_kib, 0);
        my $pool_size_kib = int(($plan->{target_bytes} + 1023) / 1024);
        my $created = 0;
        eval {
            my @create = ('/sbin/lvcreate');
            push @create, ('--devices', $device) if defined($device);
            push @create, ('--yes', '--wipesignatures', 'y', '-L',
                "${pool_size_kib}K", '-n', $pool, $vg);
            run_command(\@create,
                errmsg => "creating prepared Thin import pool '$vg/$pool' failed");
            $created = 1;
            my @convert = ('/sbin/lvconvert');
            push @convert, ('--devices', $device) if defined($device);
            push @convert, ('-y', '--type', 'thin-pool', "$vg/$pool");
            run_command(\@convert,
                errmsg => "converting prepared Thin import pool '$vg/$pool' failed");
            my @tag = ('/sbin/lvchange');
            push @tag, ('--devices', $device) if defined($device);
            push @tag, ('--addtag', "pve-slt-sid-$storeid",
                '--addtag', THIN_OWNER_SCHEMA_TAG,
                '--addtag', 'pve-slt-import-v1',
                '--addtag', "pve-slt-import-tx-$tx",
                '--addtag', "pve-slt-import-bytes-$required_bytes", "$vg/$pool");
            run_command(\@tag,
                errmsg => "tagging prepared Thin import pool '$vg/$pool' failed");
            $class->_disable_and_verify_autoactivation($vg, $pool);
            $class->_verify_allocation_reserve_postcondition($storeid, \%full, $pool);
            my $state = $class->_thin_pool_runtime_state($vg, $pool, $device);
            if ($state->{pool_active} || $state->{pool_mapper_active}
                || @{$state->{active_children}}) {
                my @unmonitor = ('/sbin/lvchange');
                push @unmonitor, ('--devices', $device) if defined($device);
                push @unmonitor, ('--monitor', 'n', "$vg/$pool");
                run_command(\@unmonitor,
                    errmsg => "unmonitoring prepared Thin import pool '$vg/$pool' failed");
                my @off = ('/sbin/lvchange');
                push @off, ('--devices', $device) if defined($device);
                push @off, ('-an', "$vg/$pool");
                run_command(\@off,
                    errmsg => "deactivating prepared Thin import pool '$vg/$pool' failed");
                $state = $class->_thin_pool_runtime_state($vg, $pool, $device);
            }
            die "prepared Thin import pool '$vg/$pool' remains active\n"
                if $state->{pool_active} || $state->{pool_mapper_active}
                || @{$state->{active_children}};
        };
        my $error = $@;
        die _partial_allocation_error(
            $storeid, $vmid, "prepared-import-$tx", $pool, $error,
            $created ? "prepared pool '$vg/$pool' may exist" : 'no created pool was confirmed')
            if $error;
        return 'THIN_IMPORT_POOL_PREPARED';
    });
}

sub _allocation_headroom_plan {
    my ($class, $storeid, $scfg, $pool, $size_kib, $pool_exists) = @_;
    my $mode = $scfg->{'slt-initial-pool-mode'} // 'fixed';
    my $fixed = $scfg->{'slt-initial-pool-size'} // 16;
    my $percent = $scfg->{'slt-initial-pool-percent'} // 50;
    my $maximum = $scfg->{'slt-initial-pool-max'};
    my $headroom = $scfg->{'slt-burst-headroom-gib'} // 64;

    # Exact backwards compatibility: fixed mode retains the RC4/early-RC5
    # allocation behavior and does not introduce a new reserve dependency.
    if ($mode eq 'fixed') {
        return PVE::SharedLvmThinSafety::evaluate_allocation_target(
            mode => $mode,
            fixed_gib => $fixed,
            requested_kib => $size_kib,
            used_bytes => 0,
            current_pool_bytes => $pool_exists ? $fixed * 1024 * 1024 * 1024 : 0,
        );
    }

    die "allocation headroom policy requires slt-vg-reserve-percent or slt-vg-reserve-gib\n"
        if !defined($scfg->{'slt-vg-reserve-percent'})
        && !defined($scfg->{'slt-vg-reserve-gib'});

    my $vg = $scfg->{'slt-vgname'};
    my ($vg_size, $vg_free, $extent_size) = _allocation_numeric_fields(
        [
            '/sbin/vgs', '--readonly', '--noheadings', '--units', 'b', '--nosuffix',
            '--separator', '|', '-o', 'vg_size,vg_free,vg_extent_size', $vg,
        ],
        "reading allocation capacity of VG '$vg' failed", 3,
    );

    my ($current_pool, $used, $usage_known) = (0, 0, 1);
    if ($pool_exists) {
        my ($pool_size, $data_percent) = _allocation_numeric_fields(
            [
                '/sbin/lvs', '--readonly', '--noheadings', '--units', 'b', '--nosuffix',
                '--separator', '|', '-o', 'lv_size,data_percent', "$vg/$pool",
            ],
            "reading allocation state of '$vg/$pool' failed", 2, 1,
        );
        $current_pool = int($pool_size);
        if (!defined($data_percent)) {
            # `lvs --readonly` intentionally avoids consulting live
            # device-mapper state.  For an exactly identified active hidden
            # -tpool it can therefore omit Data% even though the pool is
            # owned and in use.  This used to disable pre-growth during a
            # native PVE import and allowed a fast copy to outrun dmeventd.
            # Reuse the same exact runtime topology proof as the monitor,
            # then perform a normal read-only query (no metadata mutation)
            # scoped to the pinned device.  Never activate an inactive pool
            # merely to obtain an estimate.
            my $device = defined($scfg->{'slt-expected-wwid'})
                ? "/dev/mapper/$scfg->{'slt-expected-wwid'}"
                : undef;
            my $runtime = $class->_thin_pool_runtime_state(
                $vg, $pool, $device,
            );
            if ($runtime->{pool_mapper_active}) {
                my @live = (
                    '/sbin/lvs', '--noheadings', '--units', 'b', '--nosuffix',
                    '--separator', '|', '-o', 'lv_size,data_percent',
                );
                push @live, ('--devices', $device) if defined($device);
                push @live, "$vg/$pool";
                ($pool_size, $data_percent) = _allocation_numeric_fields(
                    \@live,
                    "reading active allocation state of '$vg/$pool' failed", 2, 1,
                );
                $current_pool = int($pool_size);
            }
        }
        if (defined($data_percent)) {
            $used = int(($current_pool * $data_percent + 99) / 100);
        } else {
            # Inactive thin pools can legitimately omit Data%.  Never activate
            # shared storage merely to improve an admission estimate.  An
            # unknown value must not be converted into used=current-size:
            # doing so adds headroom again after every cancelled allocation.
            # The policy engine keeps an existing pool unchanged, while full
            # admission fails closed because its guarantee cannot be proven.
            $usage_known = 0;
            warn "SharedLvmThin: Data% unavailable for inactive '$vg/$pool'; "
                . "automatic pre-growth disabled until usage is known\n";
        }
    }

    my %target_args = (
        mode => $mode,
        fixed_gib => $fixed,
        requested_kib => $size_kib,
        used_bytes => $used,
        current_pool_bytes => $current_pool,
        usage_known => $usage_known,
        # Existing active pools may receive an immediate full-speed native
        # import after allocation.  Keep the admitted write below the hard
        # data ceiling instead of relying on asynchronous autogrow.
        guard_projected_write => ($pool_exists && $usage_known) ? 1 : 0,
    );
    $target_args{percent} = $percent if $mode eq 'proportional';
    $target_args{headroom_gib} = $headroom if $mode eq 'elastic';
    $target_args{max_gib} = $maximum if defined($maximum);
    my $plan = PVE::SharedLvmThinSafety::evaluate_allocation_target(%target_args);

    my $overhead = $pool_exists ? 0
        : _allocation_metadata_overhead_bytes($plan->{target_bytes});
    my $reserve = PVE::SharedLvmThinSafety::evaluate_allocation_reserve(
        vg_size => int($vg_size),
        vg_free => int($vg_free),
        growth_bytes => $plan->{growth_bytes},
        overhead_bytes => $overhead,
        extent_bytes => int($extent_size),
        reserve_percent => $scfg->{'slt-vg-reserve-percent'} // 0,
        reserve_gib => $scfg->{'slt-vg-reserve-gib'} // 0,
    );
    die "allocation headroom rejected for '$vg/$pool': requires "
        . "$reserve->{required_physical_bytes} physical bytes including extent/metadata allowance; "
        . "projected VG free $reserve->{free_after_bytes} bytes would cross protected reserve "
        . "$reserve->{reserve_bytes} bytes; no lvcreate/lvextend was run\n"
        if !$reserve->{allowed};

    $plan->{reserve} = $reserve;
    return $plan;
}

sub _grow_pool_for_allocation {
    my ($class, $vg, $pool, $plan) = @_;
    return if !$plan->{growth_bytes};

    my $target_kib = int(($plan->{target_bytes} + 1023) / 1024);
    my $grow_error;
    eval {
        run_command(
            ['/sbin/lvextend', '-L', "${target_kib}K", "$vg/$pool"],
            errmsg => "allocation pre-grow of '$vg/$pool' failed",
        );
    };
    $grow_error = $@;

    my $actual;
    my $post_error;
    eval {
        ($actual) = _allocation_numeric_fields(
            [
                '/sbin/lvs', '--readonly', '--noheadings', '--units', 'b', '--nosuffix',
                '-o', 'lv_size', "$vg/$pool",
            ],
            "reading allocation pre-grow postcondition of '$vg/$pool' failed", 1,
        );
    };
    $post_error = $@;

    die "$grow_error"
        . "UNKNOWN: allocation pre-grow outcome cannot be verified; no retry or shrink will be attempted\n"
        if $post_error || !defined($actual);
    die "$grow_error"
        . "PARTIAL: pool remains at $actual bytes below required target $plan->{target_bytes}; "
        . "no retry or shrink will be attempted\n"
        if $actual < $plan->{target_bytes};
    warn "SharedLvmThin PARTIAL/COMPLETED: lvextend reported an error but allocation target was reached; no retry\n"
        if $grow_error;
    return;
}

sub _verify_allocation_reserve_postcondition {
    my ($class, $storeid, $scfg, $pool) = @_;
    return if ($scfg->{'slt-initial-pool-mode'} // 'fixed') eq 'fixed';

    my $vg = $scfg->{'slt-vgname'};
    my ($vg_size, $vg_free) = _allocation_numeric_fields(
        [
            '/sbin/vgs', '--readonly', '--noheadings', '--units', 'b', '--nosuffix',
            '--separator', '|', '-o', 'vg_size,vg_free', $vg,
        ],
        "reading allocation reserve postcondition of VG '$vg' failed", 2,
    );
    my $decision = PVE::SharedLvmThinSafety::evaluate_allocation_reserve(
        vg_size => int($vg_size),
        vg_free => int($vg_free),
        growth_bytes => 0,
        overhead_bytes => 0,
        extent_bytes => 1,
        reserve_percent => $scfg->{'slt-vg-reserve-percent'} // 0,
        reserve_gib => $scfg->{'slt-vg-reserve-gib'} // 0,
    );
    die "PARTIAL ALLOCATION: '$vg/$pool' reached its allocation target, but actual VG free "
        . "$vg_free bytes is below protected reserve $decision->{reserve_bytes} bytes; "
        . "pool preserved; guest LV creation refused; no shrink, cleanup, or retry attempted\n"
        if !$decision->{allowed};
    return 1;
}

sub _deactivate_new_thin_pool_after_allocation {
    my ($class, $vg, $pool, $lv, $device) = @_;

    # lvconvert/lvcreate intentionally activate a newly created thin pool in
    # the allocating kernel.  At this point PVE has not attached the target to
    # QEMU and no durable runtime owner has been claimed.  Publish an entirely
    # inactive object so the normal activate_volume hook can claim an epoch
    # and load a fresh metadata view.  This is essential for online Storage
    # Move into Thin and harmless for ordinary stopped-VM allocation.
    my @unmonitor = ('/sbin/lvchange');
    push @unmonitor, ('--devices', $device) if defined($device);
    push @unmonitor, ('--monitor', 'n', "$vg/$pool");
    run_command(
        \@unmonitor,
        errmsg => "unregistering newly allocated thin pool '$vg/$pool' failed",
    );

    my @child_off = ('/sbin/lvchange');
    push @child_off, ('--devices', $device) if defined($device);
    push @child_off, ('-an', "$vg/$lv");
    run_command(
        \@child_off,
        errmsg => "deactivating newly allocated thin LV '$vg/$lv' failed",
    );

    my $vg_dm = $vg;
    my $pool_dm = $pool;
    my $lv_dm = $lv;
    $vg_dm =~ s/-/--/g;
    $pool_dm =~ s/-/--/g;
    $lv_dm =~ s/-/--/g;
    my $pool_path = "/dev/mapper/$vg_dm-$pool_dm-tpool";
    my $child_path = "/dev/mapper/$vg_dm-$lv_dm";
    if (_block_device_exists($pool_path)) {
        my @pool_off = ('/sbin/lvchange');
        push @pool_off, ('--devices', $device) if defined($device);
        push @pool_off, ('-an', "$vg/$pool");
        run_command(
            \@pool_off,
            errmsg => "deactivating newly allocated thin pool '$vg/$pool' failed",
        );
    }

    die "new thin allocation publication failed: '$vg/$pool' remains active\n"
        if _block_device_exists($pool_path) || _block_device_exists($child_path);
    return 1;
}

sub alloc_image {
    my ($class, $storeid, $scfg, $vmid, $fmt, $name, $size) = @_;
    $class->_assert_package_operations_released('volume allocation');
    return $class->_lazy_alloc_image($storeid, $scfg, $vmid, $fmt, $name, $size)
        if $class->_is_lazy_mode($scfg);
    return $class->_thick_alloc_image($storeid, $scfg, $vmid, $fmt, $name, $size)
        if $class->_allocation_mode($scfg) eq 'thick-generations';

    return $class->_with_mutation_lock($storeid, $scfg, sub {
        return $class->_alloc_image_locked(
            $storeid, $scfg, $vmid, $fmt, $name, $size,
        );
    });
}

sub _alloc_image_locked {
    my ($class, $storeid, $scfg, $vmid, $fmt, $name, $size) = @_;

    die "unsupported format '$fmt'\n"
        if defined($fmt) && $fmt ne 'raw';

    $class->_verify_mutation_quorum($storeid, $scfg);
    $class->_verify_storage_identity($storeid, $scfg);

    my $vg = $scfg->{'slt-vgname'};
    my $pool = "sltp-$vmid";

    $name = $class->find_free_diskname($storeid, $scfg, $vmid)
        if !$name;

    my $is_guest_disk = $name =~ /^vm-\Q$vmid\E-disk-\d+$/;
    my $is_vmstate = $name =~ /^vm-\Q$vmid\E-state-[A-Za-z0-9][A-Za-z0-9_.-]*$/;
    my $is_fleecing = $name =~ /^vm-\Q$vmid\E-fleece-\d+$/;
    my $is_cloudinit = $name eq "vm-$vmid-cloudinit";
    my $is_auxiliary = $is_vmstate || $is_fleecing || $is_cloudinit;

    die "illegal volume name '$name'\n"
        if !$is_guest_disk && !$is_auxiliary;

    my $lvs = PVE::Storage::LVMPlugin::lvm_list_volumes($vg);

    my $pool_exists =
        $lvs->{$vg}
        && $lvs->{$vg}->{$pool};

    die "refusing VMID reuse for $vmid: object '$vg/$name' already exists; naming is not ownership proof\n"
        if $lvs->{$vg} && $lvs->{$vg}->{$name};

    my $created_pool = 0;
    my $publish_inactive = !$pool_exists;
    my $sid_tag = "pve-slt-sid-$storeid";
    my $import_state = { active => 0 };

    die "refusing auxiliary allocation '$vg/$name': owned VM pool '$vg/$pool' does not exist\n"
        if !$pool_exists && $is_auxiliary;

    my $headroom_plan = $class->_allocation_headroom_plan(
        $storeid, $scfg, $pool, $size, $pool_exists,
    );

    if (!$pool_exists) {
        my $pool_size_k = int(($headroom_plan->{target_bytes} + 1023) / 1024);
        my $pool_create_error;

        eval {
            run_command(
                [
                    '/sbin/lvcreate',
                    '--yes', '--wipesignatures', 'y',
                    '-L', "${pool_size_k}K",
                    '-n', $pool,
                    $vg,
                ],
                errmsg => "creating backing LV '$vg/$pool' failed",
            );

            #
            # From this point on this invocation owns the newly-created
            # backing LV and must clean it up if a later step fails.
            #
            $created_pool = 1;

            run_command(
                [
                    '/sbin/lvconvert',
                    '-y',
                    '--type', 'thin-pool',
                    "$vg/$pool",
                ],
                errmsg => "converting '$vg/$pool' to thin pool failed",
            );

            run_command(
                ['/sbin/lvchange', '--addtag', $sid_tag,
                    '--addtag', THIN_OWNER_SCHEMA_TAG, "$vg/$pool"],
                errmsg => "tagging thin pool '$vg/$pool' failed",
            );

            $class->_disable_and_verify_autoactivation($vg, $pool);
            $class->_verify_allocation_reserve_postcondition(
                $storeid, $scfg, $pool,
            );
        };

        $pool_create_error = $@;

        if ($pool_create_error) {
            die _partial_allocation_error(
                $storeid, $vmid, $name, $pool, $pool_create_error,
                $created_pool ? "backing object '$vg/$pool' may exist" : 'no created object was confirmed',
            );
        }
    } else {
        #
        # Existing pools must already belong to this storage.
        # Never silently adopt a foreign per-VM pool.
        #
        my $pool_info = $lvs->{$vg}->{$pool};

        die "existing pool '$vg/$pool' is not a thin pool\n"
            if !defined($pool_info->{lv_type}) || $pool_info->{lv_type} ne 't';

        die "existing pool '$vg/$pool' does not belong to storage '$storeid'\n"
            if !defined($pool_info->{tags})
            || $pool_info->{tags} !~ /(?:^|,)\Q$sid_tag\E(?:,|$)/;

        my $owner_state = _thin_owner_state_from_tags($pool_info->{tags});
        die "existing pool '$vg/$pool' predates the exclusive-owner schema; allocation is blocked until explicit offline adoption\n"
            if !$owner_state->{schema};
        my $local_node = $class->_thin_local_node();
        die "refusing allocation in '$vg/$pool': persistent owner is '$owner_state->{owner}', local node is '$local_node'\n"
            if defined($owner_state->{owner}) && $owner_state->{owner} ne $local_node;
        # lvcreate may transiently activate an otherwise inactive existing
        # thin pool.  Leaving that mapper behind while the durable owner is
        # empty makes the next activate_volume correctly fail closed.  Only
        # a pool already owned by this node may remain active for hotplug;
        # every unowned/offline allocation must be published fully inactive.
        $publish_inactive = !defined($owner_state->{owner});
        $import_state = _thin_import_state_from_tags($pool_info->{tags});

        my @owned_disks = grep {
            /^vm-\Q$vmid\E-disk-\d+$/
            && defined($lvs->{$vg}->{$_}->{pool_lv})
            && $lvs->{$vg}->{$_}->{pool_lv} eq $pool
        } keys %{$lvs->{$vg}};
        my @stale_snapshots = grep {
            /^snap_vm-\Q$vmid\E-disk-\d+_/
        } keys %{$lvs->{$vg}};

        die "VMID reuse requires recovery review: owned pool '$vg/$pool' exists without an active owned disk"
            . (@stale_snapshots ? " and contains stale snapshots" : '')
            . "; automatic adoption is disabled\n"
            if !@owned_disks && !$import_state->{active};
        die "prepared Thin import pool '$vg/$pool' may allocate only a guest disk\n"
            if $import_state->{active} && !$is_guest_disk;
    }

    $class->_verify_pool_health($vg, $pool);

    if ($pool_exists && ($scfg->{'slt-initial-pool-mode'} // 'fixed') ne 'fixed') {
        $class->_grow_pool_for_allocation($vg, $pool, $headroom_plan);
        $class->_verify_pool_health($vg, $pool);
        $class->_verify_allocation_reserve_postcondition(
            $storeid, $scfg, $pool,
        );
    }

    $lvs = PVE::Storage::LVMPlugin::lvm_list_volumes($vg);

    die "volume '$vg/$name' appeared during allocation; preserving all objects\n"
        if $lvs->{$vg} && $lvs->{$vg}->{$name};

    my $alloc_error;

    eval {
        run_command(
            [
                '/sbin/lvcreate',
                '--yes', '--wipesignatures', 'y',
                '-V', "${size}K",
                '-n', $name,
                '--thinpool', "$vg/$pool",
            ],
            errmsg => "creating thin LV '$vg/$name' failed",
        );
    };

    $alloc_error = $@;

    if ($alloc_error) {
        die _partial_allocation_error(
            $storeid, $vmid, $name, $pool, $alloc_error,
            $created_pool ? "owned pool '$vg/$pool' exists; guest LV completion is unknown" : "pre-existing owned pool '$vg/$pool' preserved",
        );
    }

    my $autoactivation_error;
    eval { $class->_disable_and_verify_autoactivation($vg, $name); };
    $autoactivation_error = $@;
    if ($autoactivation_error) {
        die _partial_allocation_error(
            $storeid, $vmid, $name, $pool, $autoactivation_error,
            "guest LV '$vg/$name' exists but safe shared-storage autoactivation state is unconfirmed",
        );
    }

    if ($created_pool || $import_state->{active} || $publish_inactive) {
        my $device = defined($scfg->{'slt-expected-wwid'})
            ? "/dev/mapper/$scfg->{'slt-expected-wwid'}"
            : undef;
        my $publication_error;
        eval {
            $class->_deactivate_new_thin_pool_after_allocation(
                $vg, $pool, $name, $device,
            );
        };
        $publication_error = $@;
        if ($publication_error) {
            die _partial_allocation_error(
                $storeid, $vmid, $name, $pool, $publication_error,
                "new thin LV '$vg/$name' exists, but inactive publication is unproven",
            );
        }
    }

    # The prepared-import evidence is removed only after the first guest LV is
    # durably present and the complete pool/child mapping has been published
    # inactive. A crash before this point therefore remains classifiable.
    if ($import_state->{active}) {
        my $device = defined($scfg->{'slt-expected-wwid'})
            ? "/dev/mapper/$scfg->{'slt-expected-wwid'}" : undef;
        my @clear = ('/sbin/lvchange');
        push @clear, ('--devices', $device) if defined($device);
        push @clear, ('--deltag', 'pve-slt-import-v1',
            '--deltag', "pve-slt-import-tx-$import_state->{tx}",
            '--deltag', "pve-slt-import-bytes-$import_state->{bytes}", "$vg/$pool");
        run_command(\@clear,
            errmsg => "clearing prepared Thin import state on '$vg/$pool' failed");
        my $after = $class->_thin_pool_tags($vg, $pool, $device);
        die "prepared Thin import state clear postcondition failed for '$vg/$pool'\n"
            if _thin_import_state_from_tags($after)->{active};
    }

    return $name;
}

sub activate_volume {
    my ($class, $storeid, $scfg, $volname, $snapname, $cache) = @_;
    $class->_assert_package_operations_released('volume activation');
    # Lazy is a provisioning default for canonical guest disks, not a second
    # representation for PVE's short-lived auxiliary RAW objects.  vmstate,
    # fleecing and cloud-init volumes are allocated fully materialized and must
    # use the matching Thick activation lifecycle.  Sending one through the
    # guest-disk-only Lazy executor would reject its identity during snapshot
    # cleanup and could mask the original QMP result.
    return $class->_thick_activate_volume($storeid, $scfg, $volname, $snapname, $cache)
        if $class->_is_lazy_mode($scfg)
        && $volname =~ /^vm-\d+-(?:state-[A-Za-z0-9][A-Za-z0-9_.-]*|fleece-\d+|cloudinit)$/;
    return $class->_lazy_activate_volume($storeid, $scfg, $volname, $snapname, $cache)
        if $class->_is_lazy_mode($scfg);
    return $class->_thick_activate_volume($storeid, $scfg, $volname, $snapname, $cache)
        if $class->_allocation_mode($scfg) eq 'thick-generations';

    return $class->_with_mutation_lock($storeid, $scfg, sub {
        return $class->_activate_thin_volume_locked(
            $storeid, $scfg, $volname, $snapname, $cache);
    });
}

# Reusable only with the canonical mutation lock already held. Rollback must
# use the same owner/peer/ThinGuard admission before LVM can activate a pool.
sub _activate_thin_volume_locked {
    my ($class, $storeid, $scfg, $volname, $snapname, $cache) = @_;

    my $vg = $scfg->{'slt-vgname'};
    my $lv = $snapname ? "snap_${volname}_${snapname}" : $volname;
    my (undef, undef, $vmid) = $class->parse_volname($volname);
    my $pool = "sltp-$vmid";
    my $device = defined($scfg->{'slt-expected-wwid'})
        ? "/dev/mapper/$scfg->{'slt-expected-wwid'}"
        : undef;

    # A dm-thin pool is a single-kernel metadata domain.  PVE shared-storage
    # live migration explicitly activates the target before the source closes,
    # so a normal cluster operation lock is not sufficient: ownership must
    # persist for the complete lifetime of the active pool.  Claim the pool
    # under the canonical VG lock before local activation.  A different
    # owner's tag is never stolen or inferred stale here.
        $class->_verify_owned_volume($storeid, $scfg, $volname);
        $class->_verify_autoactivation_disabled($vg, $pool, $device);
        $class->_verify_autoactivation_disabled($vg, $lv, $device);
        my $runtime_before = {pool_mapper_active => 0};
        $runtime_before = $class->_thin_pool_runtime_state($vg, $pool, $device)
            if ($scfg->{'slt-thin-leaseguard'} // 'disabled') eq 'runtime-guard';
        my $epoch = $class->_thin_claim_pool_owner_locked($vg, $pool, $device, $scfg);
        if (($scfg->{'slt-thin-leaseguard'} // 'disabled') eq 'runtime-guard'
            && !$runtime_before->{pool_mapper_active}) {
            my (undef, $mapper_uuid, $pool_uuid) =
                $class->_thin_pool_dm_identity($vg, $pool, $device);
            eval {
                $class->_thin_runtime_guard_request($scfg, 'PREPARE',
                    storage_id => $storeid,
                    pool_uuid => $pool_uuid,
                    owner_epoch => $epoch,
                    mapper_uuid => $mapper_uuid,
                );
            };
            if (my $guard_error = $@) {
                # No local mapper exists yet. Releasing the just-created owner
                # epoch is therefore a bounded rollback, not storage repair.
                $class->_thin_release_pool_owner_locked($vg, $pool, $device, 0);
                die "ThinGuard refused activation before lvchange: $guard_error";
            }
        }

        # Activation commit barrier.  Peer absence proved while claiming the
        # owner must not be treated as indefinitely fresh: SSH and guardian
        # preparation can take long enough for stale/manual state to become
        # visible.  Audit once more immediately before the mutating lvchange,
        # then prove that the exact local owner epoch we admitted still exists.
        $class->_thin_activation_commit_barrier_locked(
            $scfg, $vg, $pool, $device, $epoch,
        );

        my @activate = ('/sbin/lvchange');
        push @activate, ('--devices', $device) if defined($device);
        push @activate, ('-ay', '-K', "$vg/$lv");
        run_command(
            \@activate,
            errmsg => "activating exclusively-owned shared thin LV '$vg/$lv' failed; owner preserved for explicit recovery",
        );
        return 1;
}

sub _thin_pool_runtime_state {
    my ($class, $vg, $pool, $device) = @_;
    my @command = ('/sbin/lvs', '--readonly');
    push @command, ('--devices', $device) if defined($device);
    push @command, ('--noheadings', '--separator', '|',
        '-o', 'lv_name,lv_attr,pool_lv', $vg);
    my $lines = _command_lines(
        \@command,
        "reading runtime state of thin pool '$vg/$pool' failed",
    );

    my $dm_inventory = _dm_kernel_inventory();
    my $vg_dm = $vg;
    $vg_dm =~ s/-/--/g;
    my ($pool_found, @active_children);
    for my $line (@$lines) {
        my ($name, $attr, $pool_lv) = split(/\|/, $line, -1);
        for ($name, $attr, $pool_lv) {
            $_ //= '';
            s/^\s+|\s+$//g;
        }
        die "runtime thin-pool inventory of '$vg/$pool' is malformed\n"
            if $name eq '' || length($attr) < 5;
        if ($name eq $pool) {
            die "runtime thin-pool inventory contains duplicate pool '$vg/$pool'\n"
                if $pool_found;
            $pool_found = 1;
        }
        if ($pool_lv eq $pool) {
            my $name_dm = $name;
            $name_dm =~ s/-/--/g;
            push @active_children, $name
                if exists($dm_inventory->{"$vg_dm-$name_dm"});
        }
    }
    die "runtime thin-pool inventory is missing '$vg/$pool'\n"
        if !$pool_found;

    my $pool_dm = $pool;
    $pool_dm =~ s/-/--/g;
    my $public_pool_mapper = "$vg_dm-$pool_dm";
    my $hidden_pool_mapper = "$vg_dm-$pool_dm-tpool";
    my $public_active = exists($dm_inventory->{$public_pool_mapper}) ? 1 : 0;
    my $hidden_active = exists($dm_inventory->{$hidden_pool_mapper}) ? 1 : 0;
    my $pool_mapper_active = $public_active || $hidden_active ? 1 : 0;
    my $pool_mapper = $hidden_active ? $hidden_pool_mapper : $public_pool_mapper;

    return {
        pool_active => $pool_mapper_active,
        pool_mapper => $pool_mapper,
        pool_mapper_active => $pool_mapper_active,
        public_pool_mapper_active => $public_active,
        hidden_pool_mapper_active => $hidden_active,
        active_children => \@active_children,
    };
}

sub _thin_pool_members {
    my ($class, $vg, $pool, $device) = @_;
    my @command = ('/sbin/lvs', '--readonly');
    push @command, ('--devices', $device) if defined($device);
    push @command, ('--noheadings', '--separator', '|', '-o', 'lv_name,pool_lv', $vg);
    my $lines = _command_lines(
        \@command,
        "reading exact thin-pool membership of '$vg/$pool' failed",
    );
    my %members;
    for my $line (@$lines) {
        my ($name, $pool_lv) = split(/\|/, $line, -1);
        for ($name, $pool_lv) {
            $_ //= '';
            s/^\s+|\s+$//g;
        }
        die "thin-pool membership inventory contains an invalid LV name\n"
            if $name !~ /^[A-Za-z0-9_.+-]+$/;
        next if $pool_lv ne $pool;
        die "thin-pool membership inventory contains duplicate '$vg/$name'\n"
            if $members{$name}++;
    }
    return [sort keys %members];
}

sub deactivate_volume {
    my ($class, $storeid, $scfg, $volname, $snapname, $cache) = @_;
    return $class->_thick_deactivate_volume($storeid, $scfg, $volname, $snapname, $cache)
        if $class->_is_lazy_mode($scfg)
        && $volname =~ /^vm-\d+-(?:state-[A-Za-z0-9][A-Za-z0-9_.-]*|fleece-\d+|cloudinit)$/;
    return $class->_lazy_deactivate_volume($storeid, $scfg, $volname, $snapname, $cache)
        if $class->_is_lazy_mode($scfg);
    return $class->_thick_deactivate_volume($storeid, $scfg, $volname, $snapname, $cache)
        if $class->_allocation_mode($scfg) eq 'thick-generations';

    return $class->_with_mutation_lock($storeid, $scfg, sub {
        return $class->_deactivate_thin_volume_locked(
            $storeid, $scfg, $volname, $snapname, $cache,
        );
    });
}

sub _deactivate_thin_volume_locked {
    my ($class, $storeid, $scfg, $volname, $snapname, $cache) = @_;
    my $vg = $scfg->{'slt-vgname'};
    my $lv = $snapname ? "snap_${volname}_${snapname}" : $volname;
    my (undef, undef, $vmid) = $class->parse_volname($volname);
    my $pool = "sltp-$vmid";
    my $device = defined($scfg->{'slt-expected-wwid'})
        ? "/dev/mapper/$scfg->{'slt-expected-wwid'}"
        : undef;

    # Activation, dmeventd registration and DM teardown are node-local runtime
    # state.  They do not change shared VG metadata and must not serialize all
    # VM migrations behind the canonical VG mutation lock.  PVE already holds
    # the VM operation lock; exact mapper names plus the pinned device and
    # ownership checks scope this cleanup to one VM pool on this node.
    $class->_verify_storage_identity($storeid, $scfg, $device);
    # Capacity pressure must never prevent node-local mapper teardown after a
    # guest has stopped.  Only the Data% admission rule is relaxed here;
    # metadata health, exact ownership and storage identity remain mandatory.
    $class->_verify_owned_volume(
        $storeid, $scfg, $volname,
        allow_capacity_teardown => 1,
    );

    my $guard_owner;
    my $guard_pool_uuid;
    if (($scfg->{'slt-thin-leaseguard'} // 'disabled') eq 'runtime-guard') {
        $guard_owner = _thin_owner_state_from_tags(
            $class->_thin_pool_tags($vg, $pool, $device));
        (undef, undef, $guard_pool_uuid) =
            $class->_thin_pool_dm_identity($vg, $pool, $device);
    }

    my $state = $class->_thin_pool_runtime_state($vg, $pool, $device);
    my @other_children = grep { $_ ne $lv } @{$state->{active_children}};

        # dmeventd must release the hidden -tpool mapping while the public
        # pool is still active.  Once the final guest LV is deactivated LVM
        # can report the public pool inactive even though dmeventd remains
        # the sole opener of the hidden mapper, at which point --monitor n
        # no longer unregisters it.
    if (!@other_children && $state->{pool_active}) {
        my @unmonitor = ('/sbin/lvchange');
        push @unmonitor, ('--devices', $device) if defined($device);
        push @unmonitor, ('--monitor', 'n', "$vg/$pool");
        run_command(
            \@unmonitor,
            errmsg => "unregistering final shared thin pool '$vg/$pool' from dmeventd failed",
        );
    }

    my @deactivate = ('/sbin/lvchange');
    push @deactivate, ('--devices', $device) if defined($device);
    push @deactivate, ('-an', "$vg/$lv");
    run_command(
        \@deactivate,
        errmsg => "deactivating shared thin LV '$vg/$lv' failed",
    );

    $state = $class->_thin_pool_runtime_state($vg, $pool, $device);
        # Under concurrent live migration QEMU teardown can make lvchange -an
        # complete while the exact thin mapping is still being removed by DM.
        # Do not mistake that bounded transition for an independently active
        # sibling, otherwise dmeventd remains the sole opener of the pool on
        # the evacuated node.  Wait only for the exact requested LV and never
        # for an unrelated child.
    for (1 .. 20) {
        last if !grep { $_ eq $lv } @{$state->{active_children}};
        select(undef, undef, undef, 0.25);
        $state = $class->_thin_pool_runtime_state($vg, $pool, $device);
    }
    die "thin LV deactivation postcondition failed: '$vg/$lv' remains active\n"
        if grep { $_ eq $lv } @{$state->{active_children}};
    return 1 if @{$state->{active_children}};

    if ($state->{pool_active} || $state->{pool_mapper_active}) {
        my @pool_deactivate = ('/sbin/lvchange');
        push @pool_deactivate, ('--devices', $device) if defined($device);
        push @pool_deactivate, ('-an', "$vg/$pool");
        run_command(
            \@pool_deactivate,
            errmsg => "deactivating idle shared thin pool '$vg/$pool' failed",
        );
    }

    my $after = $class->_thin_pool_runtime_state($vg, $pool, $device);
    die "thin-pool deactivation postcondition failed: '$vg/$pool' remains active\n"
        if $after->{pool_active};
    die "thin-pool deactivation postcondition failed: hidden mapper '$after->{pool_mapper}' remains active\n"
        if $after->{pool_mapper_active};
    die "thin-pool deactivation postcondition failed: active children remain in '$vg/$pool'\n"
        if @{$after->{active_children}};

    # PVE invokes target-side deactivate cleanup after a failed start/live
    # migration.  If the exact local runtime is already absent and the
    # persistent owner belongs to another node, cleanup is complete locally.
    # Never turn that cleanup callback into an attempt to release or rewrite
    # the remote owner's evidence.
    $class->_thin_release_pool_owner_locked($vg, $pool, $device, 1);
    if (defined($guard_owner) && defined($guard_owner->{owner})
        && $guard_owner->{owner} eq $class->_thin_local_node()) {
        $class->_thin_runtime_guard_request($scfg, 'RELEASE',
            pool_uuid => $guard_pool_uuid,
            owner_epoch => $guard_owner->{epoch},
        );
    }
    return 1;
}

sub _thick_resume_transition {
    my ($class, $scfg, $storeid, $volname, $snap, $operation, $intent, $lvs,
        $persistent_only) = @_;
    my $vg = $scfg->{'slt-vgname'};
    my $namespace = $class->_thick_namespace($scfg);
    my $device = "/dev/mapper/$scfg->{'slt-expected-wwid'}";
    $lvs //= $class->_thick_list_volumes_scoped($scfg, $vg, $device);
    my ($state, $head_info, $anchor) =
        $class->_thick_read_anchor($storeid, $scfg, $volname, $lvs);
    my $expected_intent_op = $operation eq 'ROLLBACK' ? 'DM_PIVOT' : 'DM_CUTOVER';

    die "existing VG intent is not the exact resumable transition\n"
        if !defined($intent) || $intent->{state} ne 'OPEN'
        || $intent->{op} ne $expected_intent_op || $intent->{object} ne $anchor
        || $intent->{tx} ne $state->{tx};
    die "recoverable transition request identity mismatch\n"
        if $state->{phase} !~ /^(?:PREPARED|SOURCE_READY|COMMITTED|HYDRATING|HYDRATION_COMPLETE|LINEAR_PIVOTED)$/
        || $state->{op} ne $operation || $state->{snapshot} ne $snap
        || ($state->{phase} =~ /^(?:PREPARED|SOURCE_READY)$/ && $state->{head} ne $state->{old})
        || ($state->{phase} =~ /^(?:COMMITTED|HYDRATING|HYDRATION_COMPLETE|LINEAR_PIVOTED)$/
            && $state->{head} ne $state->{new});

    my ($old, $new, $source) = @{$state}{qw(old new source)};
    my ($old_gen) = $old =~ /-(\d{8})$/;
    my ($new_gen) = $new =~ /-(\d{8})$/;
    die "recoverable transition generation names are malformed\n"
        if !defined($old_gen) || !defined($new_gen);
    ($old_gen, $new_gen) = (int($old_gen), int($new_gen));
    die "recoverable transition generations are not consecutive\n"
        if $new_gen != $old_gen + 1;
    my $expected_anchor_gen = $state->{phase} =~ /^(?:PREPARED|SOURCE_READY)$/
        ? $old_gen : $new_gen;
    die "recoverable transition anchor generation mismatch\n"
        if int($state->{generation}) != $expected_anchor_gen;
    my $key = object_key($namespace, $volname);
    my $expected_new = generation_name($namespace, $volname, $new_gen);
    my $meta = sprintf('sltg-m-%s-%08d', $key, $new_gen);
    die "prepared transition destination name mismatch\n" if $new ne $expected_new;
    for my $name ($source, $new) {
        die "prepared transition object '$vg/$name' is missing\n"
            if !$lvs->{$vg} || !$lvs->{$vg}->{$name};
    }
    die "prepared transition object '$vg/$old' is missing\n"
        if (!$lvs->{$vg} || !$lvs->{$vg}->{$old})
        && !($state->{phase} eq 'LINEAR_PIVOTED' && $operation eq 'ROLLBACK');
    die "prepared transition object '$vg/$meta' is missing\n"
        if (!$lvs->{$vg} || !$lvs->{$vg}->{$meta})
        && $state->{phase} ne 'LINEAR_PIVOTED';

    my ($source_gen, $source_info);
    if ($operation eq 'ROLLBACK') {
        my $found;
        ($found, $source_gen, $source_info) = $class->_thick_find_snapshot(
            $storeid, $scfg, $volname, $snap, $lvs,
        );
        die "prepared rollback source identity mismatch\n" if $found ne $source;
    } else {
        $source_gen = $old_gen;
        $source_info = $lvs->{$vg}->{$source};
        die "prepared snapshot source identity mismatch\n" if $source ne $old;
    }
    my $size = $source_info->{lv_size};
    my $old_size = $lvs->{$vg}->{$old}
        ? $lvs->{$vg}->{$old}->{lv_size}
        : $size;
    die "prepared transition size is invalid\n"
        if !defined($size) || $size !~ /^\d+$/ || !$size || $size % 512
        || !defined($old_size) || $old_size !~ /^\d+$/ || !$old_size || $old_size % 512;
    my $geometry = clone_geometry(int($size), int($state->{region}));
    validate_generation_tags(
        $lvs->{$vg}->{$new}->{tags} // '', sid => $storeid,
        vol => $volname, role => 'head', generation => $new_gen,
    );
    if ($lvs->{$vg}->{$meta}) {
        $class->_thick_verify_transition_metadata(
            $storeid, $scfg, $volname, $lvs->{$vg}->{$meta},
            tx => $intent->{tx}, generation => $new_gen,
            region => $geometry->{region_sectors},
            metadata_bytes => $geometry->{metadata_bytes}, name => $meta, device => $device,
        );
    }
    $class->_thick_verify_autoactivation_disabled($scfg, $vg, $new, $device);
    $class->_thick_verify_autoactivation_disabled($scfg, $vg, $meta, $device)
        if $lvs->{$vg}->{$meta};
    my $source_map = $class->_thick_source_mapper_name($scfg, $volname, $source_gen);
    my $persistent = {
        state => $state, anchor => $anchor, old => $old, new => $new,
        source => $source, source_gen => $source_gen,
        old_gen => $old_gen, new_gen => $new_gen, meta => $meta,
        source_map => $source_map, size => int($size), old_size => int($old_size),
        geometry => $geometry, operation => $operation, snapshot => $snap,
    };
    # Admission may inspect a foreign transaction while its owning worker is
    # changing node-local DM runtime.  Persistent-only proof deliberately
    # stops before mapper inspection and, crucially, before the resume path is
    # allowed to advance PREPARED to SOURCE_READY.  It proves the signed
    # anchor/LV/UUID/tag/geometry graph and nothing more.
    return $persistent if $persistent_only;
    if ($class->_thick_managed_mapper_present(
        $scfg, $source_map, "SLT-TG3-SOURCE-$intent->{tx}",
        "thick-generations source mapper '$source_map'",
    )) {
        $class->_thick_verify_source_mapper(
            $scfg, $source_map, $source, int($size / 512), $intent->{tx},
        );
        if ($state->{phase} eq 'PREPARED') {
            $state = $class->_thick_transition_anchor($scfg,
                $vg, $anchor, $state, phase => 'SOURCE_READY', _device => $device,
            );
        }
    } elsif ($state->{phase} ne 'PREPARED' && $state->{phase} ne 'LINEAR_PIVOTED') {
        my $front = mapper_name($namespace, $volname);
        die "$state->{phase} transition runtime is partial; source mapper '$source_map' is missing\n"
            if $class->_thick_frontend_present($scfg, $volname);
    }
    if ($state->{phase} =~ /^(?:PREPARED|SOURCE_READY)$/) {
        my $runtime = 'active';
        if ($state->{phase} eq 'SOURCE_READY'
            && $class->_thick_mapper_is_suspended(
                mapper_name($class->_thick_namespace($scfg), $volname),
                $class->_thick_command_deadline($scfg),
            )) {
            $runtime = 'suspended';
        }
        $class->_thick_verify_frontend(
            $scfg, $volname, $old, int($old_size / 512), $runtime,
        );
    }

    $persistent->{state} = $state;
    return $persistent;
}

sub _thick_reconstruct_missing_transition_runtime {
    my ($class, $scfg, $volname, $tr, $intent) = @_;
    my $phase = $tr->{state}->{phase} // '';
    return 1 if $phase eq 'PREPARED';

    my $vg = $scfg->{'slt-vgname'};
    my $namespace = $class->_thick_namespace($scfg);
    my $device = "/dev/mapper/$scfg->{'slt-expected-wwid'}";
    my $front = mapper_name($namespace, $volname);
    my $source_map = $tr->{source_map};
    my $front_exists = $class->_thick_frontend_present($scfg, $volname);
    my $source_exists = $class->_thick_managed_mapper_present(
        $scfg, $source_map, "SLT-TG3-SOURCE-$intent->{tx}",
        "thick-generations source mapper '$source_map'",
    );

    # Once LINEAR_PIVOTED is durably recorded, the new signed HEAD is the sole
    # data authority. A reboot may remove the transient stable frontend after
    # the pivot while cleanup objects are present, partially removed, or fully
    # absent. Reconstruct only the canonical linear frontend; never recreate a
    # clone table or already-cleaned transition artifact in this phase.
    if ($phase eq 'LINEAR_PIVOTED') {
        my $sectors = int($tr->{size} / 512);
        if (!$front_exists) {
            $class->_thick_activate_exact_lvs(
                $scfg, $vg, $device,
                "activating linear-pivoted Thick Generations HEAD failed",
                $tr->{new},
            );
            my $uuid = 'SLT-TG2-' . object_key($namespace, $volname);
            $class->_thick_create_mapper_exact(
                $scfg,
                ['/sbin/dmsetup', '--verifyudev', 'create', $front, '--uuid', $uuid,
                    '--table', "0 $sectors linear /dev/$vg/$tr->{new} 0"],
                "reconstructing linear-pivoted Thick Generations frontend failed",
                sub { $class->_thick_verify_frontend(
                    $scfg, $volname, $tr->{new}, $sectors,
                ) },
            );
        } else {
            $class->_thick_verify_frontend($scfg, $volname, $tr->{new}, $sectors);
        }
        return 1;
    }

    return 1 if $front_exists && $source_exists;
    die "$phase transition runtime is partial; refusing reconstruction\n"
        if $front_exists || $source_exists;

    # A host reboot removes all transient device-mapper tables while the LVM
    # anchor, immutable generations, clone metadata, and OPEN VG intent remain
    # persistent. Reconstruct only the exact dependency graph described by
    # those already-verified objects. No global scan or cleanup is performed.
    $class->_thick_activate_exact_lvs(
        $scfg, $vg, $device,
        "activating exact persisted transition objects failed",
        $tr->{source}, $tr->{new}, $tr->{meta},
    );
    $class->_thick_create_mapper_exact(
        $scfg,
        ['/sbin/dmsetup', '--verifyudev', 'create', $source_map,
            '--readonly', '--uuid', "SLT-TG3-SOURCE-$intent->{tx}", '--table',
            "0 " . int($tr->{size} / 512) . " linear /dev/$vg/$tr->{source} 0"],
        "reconstructing immutable transition source failed",
        sub { $class->_thick_verify_source_mapper(
            $scfg, $source_map, $tr->{source}, int($tr->{size} / 512), $intent->{tx},
        ) },
    );

    my $uuid = 'SLT-TG2-' . object_key($namespace, $volname);
    if ($phase eq 'SOURCE_READY') {
        $class->_thick_activate_exact_lvs(
            $scfg, $vg, $device,
            "activating exact pre-cutover state failed",
            $tr->{old}, $tr->{anchor},
        );
        $class->_thick_create_mapper_exact(
            $scfg,
            ['/sbin/dmsetup', '--verifyudev', 'create', $front, '--uuid', $uuid,
                '--table', "0 " . int($tr->{old_size} / 512)
                    . " linear /dev/$vg/$tr->{old} 0"],
            "reconstructing pre-cutover frontend failed",
            sub { $class->_thick_verify_frontend(
                $scfg, $volname, $tr->{old}, int($tr->{old_size} / 512),
            ) },
        );
        return 1;
    }

    die "$phase transition is not safe for runtime reconstruction\n"
        if $phase ne 'COMMITTED'
        && $phase ne 'HYDRATING'
        && $phase ne 'HYDRATION_COMPLETE';
    my ($threshold, $batch) = $class->_thick_hydration_tuning(
        $scfg, $tr->{geometry}->{region_sectors},
    );
    my $sectors = int($tr->{size} / 512);
    my $command_timeout = $class->_thick_command_deadline($scfg);
    my $meta_devno = $class->_thick_verify_active_lv_identity(
        $scfg, $vg, $tr->{meta}, $device, $command_timeout,
    );
    my $new_devno = $class->_thick_verify_active_lv_identity(
        $scfg, $vg, $tr->{new}, $device, $command_timeout,
    );
    my $source_devno = $class->_thick_verify_source_mapper(
        $scfg, $source_map, $tr->{source}, $sectors, $intent->{tx},
    );
    $class->_thick_create_mapper_exact(
        $scfg,
        ['/sbin/dmsetup', '--verifyudev', 'create', $front, '--uuid', $uuid,
            '--table', "0 $sectors clone $meta_devno $new_devno "
                . "$source_devno " . $tr->{geometry}->{region_sectors}
                . " 2 no_hydration no_discard_passdown "
                . "4 hydration_threshold $threshold hydration_batch_size $batch"],
        "reconstructing persisted dm-clone frontend failed",
        sub { $class->_thick_verify_clone_frontend(
            $scfg, $volname, sectors => $sectors,
            region => $tr->{geometry}->{region_sectors}, meta => $tr->{meta},
            new => $tr->{new}, source_map => $source_map,
            source => $tr->{source}, tx => $intent->{tx},
        ) },
    );
    $class->_thick_verify_clone_status(
        $front, $phase eq 'HYDRATION_COMPLETE' ? 1 : 0,
        $sectors, $tr->{geometry}->{region_sectors},
        $class->_thick_command_deadline($scfg),
    );
    return 1;
}

sub _thick_verify_published_transition_frontend {
    my ($class, $storeid, $scfg, $volname, $state) = @_;
    my $phase = $state->{phase} // '';
    if ($phase eq 'LINEAR_PIVOTED') {
        return $class->_thick_verify_frontend($scfg, $volname, $state->{head});
    }
    die "thick-generations frontend is not in a published transition state; recovery required\n"
        if $phase ne 'HYDRATING' && $phase ne 'HYDRATION_COMPLETE';

    my $vg = $scfg->{'slt-vgname'};
    my $device = "/dev/mapper/$scfg->{'slt-expected-wwid'}";
    my $intent = $class->_read_vg_intent($scfg, $vg, $device);
    my $lvs = $class->_thick_list_volumes_scoped($scfg, $vg, $device);
    my (undef, undef, $anchor) =
        $class->_thick_read_anchor($storeid, $scfg, $volname, $lvs);

    # An asynchronous snapshot deliberately hands the published transition
    # from the VG-wide intent to its signed anchor before the PVE callback
    # returns.  From that point the anchor is the persistent transaction
    # authority and another volume may legitimately own a short-lived VG
    # intent.  Lifecycle verification is read-only, so derive only the exact
    # identity already signed by this anchor; _thick_resume_transition below
    # still verifies every generation, tag, size, metadata LV and mapper
    # dependency before the existing runtime is accepted.
    if (!defined($intent) || ($intent->{object} // '') ne $anchor) {
        die "published thick-generations transition has no exact anchor-scoped handoff\n"
            if ($state->{op} // '') ne 'SNAPSHOT'
            || ($state->{tx} // '') !~ /^[0-9a-f]{32}$/;
        $intent = {
            tx => $state->{tx}, state => 'OPEN', op => 'DM_CUTOVER',
            object => $anchor, before => ('0' x 32), _anchor_scoped => 1,
        };
    }
    my $tr = $class->_thick_resume_transition(
        $scfg, $storeid, $volname, $state->{snapshot}, $state->{op}, $intent, $lvs,
    );
    $class->_thick_verify_clone_frontend(
        $scfg, $volname, sectors => int($tr->{size} / 512),
        region => $tr->{geometry}->{region_sectors}, meta => $tr->{meta},
        new => $tr->{new}, source_map => $tr->{source_map},
        source => $tr->{source}, tx => $intent->{tx},
    );
    $class->_thick_verify_clone_status(
        mapper_name($class->_thick_namespace($scfg), $volname),
        $phase eq 'HYDRATION_COMPLETE' ? 1 : 0,
        int($tr->{size} / 512), $tr->{geometry}->{region_sectors},
        $class->_thick_command_deadline($scfg),
    );
    return 1;
}

sub _thick_volume_snapshot {
    my ($class, $scfg, $storeid, $volname, $snap, $operation, $materialize_now,
        $expected_tx) = @_;
    $operation //= 'SNAPSHOT';
    die "invalid thick-generations materialization operation\n"
        if $operation ne 'SNAPSHOT' && $operation ne 'ROLLBACK';
    my $rollback = $operation eq 'ROLLBACK';
    $snap = _thick_snapshot_name($snap);
    die "invalid expected thick-generations transaction UUID\n"
        if defined($expected_tx) && $expected_tx !~ /^[0-9a-f]{32}$/;
    die "materialization worker requires an expected transaction UUID\n"
        if $materialize_now && !defined($expected_tx);
    die "expected transaction is valid only for an existing materialization worker\n"
        if defined($expected_tx) && !$materialize_now;
    $class->_require_thick_identity_config($storeid, $scfg);
    my $vg = $scfg->{'slt-vgname'};
    my $namespace = $class->_thick_namespace($scfg);
    my $front = mapper_name($namespace, $volname);
    my $device = "/dev/mapper/$scfg->{'slt-expected-wwid'}";
    my ($tr, %intent);
    my $async_snapshot = 0;

    if (!$rollback && !$materialize_now
        && $class->_thick_online_materialization_mode($scfg) eq 'asynchronous'
        && $class->_thick_frontend_present($scfg, $volname)) {
        # PVE opens and pauses a running QEMU disk before invoking a storage
        # snapshot callback.  Waiting for full hydration in that callback
        # would therefore turn background materialization into VM downtime.
        # A zero-open frontend denotes an offline snapshot and remains
        # synchronous so no idle transition is left behind unnecessarily.
        $async_snapshot = $class->_thick_frontend_open_count(
            $front, $class->_thick_command_deadline($scfg),
        ) > 0 ? 1 : 0;
    }

    if ($rollback && $class->_thick_frontend_present($scfg, $volname)) {
        my $open = $class->_thick_frontend_open_count(
            $front, $class->_thick_command_deadline($scfg),
        );
        die "refusing thick-generations rollback while the frontend is open\n"
            if $open != 0;
    }

    my $admission_deadline = $class->_thick_progress_clock()
        + ($scfg->{'slt-mutation-admission-timeout'} // 600);
    my ($previous_blocker, $wait_round) = (undef, 0);
    while (1) {
    my $remaining = $admission_deadline - $class->_thick_progress_clock();
    die "timed out waiting for an exact foreign Thick transition; no storage mutation was issued\n"
        if !$materialize_now && $remaining <= 0;
    my $decision = $class->_with_vg_lock($storeid, $scfg, sub {
        my $existing_intent = $class->_read_vg_intent($scfg, $vg, $device);
        my $lvs = $class->_thick_list_volumes_scoped($scfg, $vg, $device);
        my $this_anchor = anchor_name($namespace, $volname);
        if (defined($expected_tx)) {
            my ($expected_state, undef, $expected_anchor) =
                $class->_thick_read_anchor($storeid, $scfg, $volname, $lvs);
            die "materialization worker transaction changed before VG-locked dispatch\n"
                if $expected_anchor ne $this_anchor
                || ($expected_state->{tx} // '') ne $expected_tx
                || ($expected_state->{op} // '') ne $operation
                || ($expected_state->{snapshot} // '') ne $snap
                || ($expected_state->{phase} // '')
                    !~ /^(?:PREPARED|SOURCE_READY|COMMITTED|HYDRATING|HYDRATION_COMPLETE|LINEAR_PIVOTED)$/;
            if (defined($existing_intent)
                && ($existing_intent->{object} // '') eq $this_anchor) {
                my $expected_intent_op = $operation eq 'ROLLBACK'
                    ? 'DM_PIVOT' : 'DM_CUTOVER';
                die "materialization worker VG intent changed before dispatch\n"
                    if ($existing_intent->{tx} // '') ne $expected_tx
                    || ($existing_intent->{state} // '') ne 'OPEN'
                    || ($existing_intent->{op} // '') ne $expected_intent_op;
            }
        }
        if (defined($existing_intent)
            && (!$materialize_now || ($existing_intent->{object} // '') eq $this_anchor)) {
            $tr = $class->_thick_resume_transition(
                $scfg, $storeid, $volname, $snap, $operation, $existing_intent, $lvs,
            );
            %intent = %$existing_intent;
            return;
        }
        if ($materialize_now) {
            my ($persisted, undef, $persisted_anchor) =
                $class->_thick_read_anchor($storeid, $scfg, $volname, $lvs);
            if (($persisted->{phase} // '') =~ /^(?:HYDRATING|HYDRATION_COMPLETE|LINEAR_PIVOTED)$/) {
                %intent = (
                    tx => $persisted->{tx}, state => 'OPEN', op => 'DM_CUTOVER',
                    object => $persisted_anchor, before => ('0' x 32),
                    _anchor_scoped => 1,
                );
                $tr = $class->_thick_resume_transition(
                    $scfg, $storeid, $volname, $snap, $operation, \%intent, $lvs,
                );
                return;
            }
            die "materialization worker found no exact resumable transition\n";
        }
        my ($state, $head_info, $anchor) =
            $class->_thick_anchor($storeid, $scfg, $volname, $lvs);
        my ($old, $old_gen) = ($state->{head}, int($state->{generation}));
        my ($source, $source_gen, $source_info) = ($old, $old_gen, $head_info);
        if ($rollback) {
            ($source, $source_gen, $source_info) = $class->_thick_find_snapshot(
                $storeid, $scfg, $volname, $snap, $lvs,
            );
            $class->_thick_verify_snapshot_readonly($scfg, $vg, $source, $device);
            $class->_thick_verify_autoactivation_disabled($scfg, $vg, $source, $device);
        }
        my $new_gen = $old_gen + 1;
        die "thick-generations generation limit reached\n" if $new_gen > 99_999_999;
        my $key = object_key($namespace, $volname);
        my $new = generation_name($namespace, $volname, $new_gen);
        my $meta = sprintf('sltg-m-%s-%08d', $key, $new_gen);
        my $source_map = $class->_thick_source_mapper_name($scfg, $volname, $source_gen);
        my $size = $source_info->{lv_size};
        my $old_size = $head_info->{lv_size};
        die "thick-generations source size is unknown or not sector aligned\n"
            if !defined($size) || $size !~ /^\d+$/ || !$size || $size % 512;
        die "thick-generations previous HEAD size is unknown or not sector aligned\n"
            if !defined($old_size) || $old_size !~ /^\d+$/ || !$old_size || $old_size % 512;
        my $geometry = $class->_thick_new_geometry($scfg, int($size));

        for my $name (grep { /^sltg-g-\Q$key\E-\d{8}$/ } keys %{$lvs->{$vg}}) {
            next if $rollback;
            my ($generation) = $name =~ /-(\d{8})$/;
            my $exists = eval {
                validate_generation_tags(
                    $lvs->{$vg}->{$name}->{tags} // '', sid => $storeid,
                    vol => $volname, role => 'snapshot',
                    generation => int($generation), snapshot => $snap,
                );
                1;
            };
            die "snapshot '$snap' already exists for '$volname'\n" if $exists;
        }
        die "thick-generations transition object already exists\n"
            if $lvs->{$vg}->{$new} || $lvs->{$vg}->{$meta}
            || $class->_thick_mapper_name_present($scfg, $source_map);
        $class->_thick_materialization_admission($scfg, $lvs);
        $class->_thick_capacity_gate(
            $storeid, $scfg, int(($size + 1023) / 1024),
            $geometry->{metadata_bytes},
        );
        $class->_thick_fault_point('C0', $operation, $storeid, $volname);
        %intent = (
            tx => $class->_new_transaction_id(), state => 'OPEN',
            op => ($rollback ? 'DM_PIVOT' : 'DM_CUTOVER'), object => $anchor,
            before => $class->_vg_state_digest($scfg, $vg, $device),
        );
        $class->_set_vg_intent($scfg, $vg, %intent, _device => $device);
        $class->_thick_fault_point('C1', $operation, $storeid, $volname);
        my $new_tags = PVE::SharedLvmThinThick::generation_tags(
            sid => $storeid, vol => $volname, role => 'head',
            generation => $new_gen,
        );
        my @new_create = (
            '/sbin/lvcreate', '--yes', '--wipesignatures', 'y', '--ignoreactivationskip',
            '--devices', $device, '-L', "${size}B", '-n', $new,
            '--setactivationskip', 'y', '--setautoactivation', 'n',
        );
        push @new_create, map { ('--addtag', $_) } @$new_tags;
        push @new_create, $vg;
        $class->_thick_create_lv_exact(
            $scfg, $device, \@new_create,
            "creating thick snapshot destination '$vg/$new' failed",
            sub {
                return $class->_thick_verify_created_lv_exact(
                    $scfg, $vg, $new, $device, int($size),
                    sub { validate_generation_tags(
                        $_[0], sid => $storeid, vol => $volname,
                        role => 'head', generation => $new_gen,
                    ) },
                );
            },
        );
        my $meta_tags = transition_tags(
            sid => $storeid, vol => $volname, tx => $intent{tx},
            kind => 'metadata', generation => $new_gen,
            region => $geometry->{region_sectors},
        );
        my @meta_create = (
            '/sbin/lvcreate', '--yes', '--wipesignatures', 'y', '--ignoreactivationskip',
            '--devices', $device,
            '-L', $geometry->{metadata_bytes} . 'B', '-n', $meta,
            '--setactivationskip', 'y', '--setautoactivation', 'n',
        );
        push @meta_create, map { ('--addtag', $_) } @$meta_tags;
        push @meta_create, $vg;
        $class->_thick_create_lv_exact(
            $scfg, $device, \@meta_create,
            "creating dm-clone metadata '$vg/$meta' failed",
            sub {
                return $class->_thick_verify_created_lv_exact(
                    $scfg, $vg, $meta, $device, $geometry->{metadata_bytes},
                    sub { validate_transition_tags(
                        $_[0], sid => $storeid, vol => $volname,
                        tx => $intent{tx}, kind => 'metadata',
                        generation => $new_gen,
                        region => $geometry->{region_sectors},
                    ) },
                );
            },
        );
        $class->_thick_disable_and_verify_autoactivation($scfg, $vg, $new, $device);
        $class->_thick_disable_and_verify_autoactivation($scfg, $vg, $meta, $device);
        $class->_thick_fault_point('C2', $operation, $storeid, $volname);
        my $prepared = $class->_thick_transition_anchor($scfg,
            $vg, $anchor, $state,
            phase => 'PREPARED', tx => $intent{tx}, old => $old, new => $new,
            op => $operation, snapshot => $snap, source => $source,
            head => $old, generation => $old_gen,
            region => $geometry->{region_sectors},
            _device => $device,
        );
        $tr = {
            state => $prepared, anchor => $anchor, old => $old, new => $new,
            source => $source, source_gen => $source_gen,
            old_gen => $old_gen, new_gen => $new_gen, meta => $meta,
            source_map => $source_map, size => int($size), old_size => int($old_size),
            geometry => $geometry, operation => $operation, snapshot => $snap,
        };
        $class->_thick_fault_point('C3', $operation, $storeid, $volname);
        return;
    }, $device, ($materialize_now ? undef : int($remaining + 1)), undef,
        ($materialize_now ? undef : sub {
            return $class->_thick_foreign_intent_admission(
                $storeid, $scfg, anchor_name($namespace, $volname),
                $previous_blocker,
            );
        }));
    last if ref($decision) ne 'HASH'
        || ($decision->{action} // '') ne 'WAIT_EXACT_FOREIGN';
    my $receipt = $decision->{receipt};
    die "foreign Thick admission returned no exact blocker receipt\n"
        if ref($receipt) ne 'HASH';
    if (!defined($previous_blocker)
        || ($previous_blocker->{tx} // '') ne ($receipt->{tx} // '')) {
        warn "waiting for exact foreign Thick transition tx=$receipt->{tx} "
            . "storage=$receipt->{sid} volume=$receipt->{vol} phase=$receipt->{phase}; "
            . "no storage mutation has been issued by this request\n";
    }
    $previous_blocker = { %$receipt };
    $wait_round++;
    my $pause = 50 + ($wait_round * 25);
    $pause = 500 if $pause > 500;
    $pause += int(rand(51));
    $pause = 1000 if $pause > 1000;
    $class->_thick_observation_pause($pause);
    }

    $class->_thick_reconstruct_missing_transition_runtime(
        $scfg, $volname, $tr, \%intent,
    );

    if ($tr->{state}->{phase} eq 'PREPARED') {
        eval {
            # dm-clone treats a DISCARD covering an unhydrated region as a
            # request to mark that region hydrated without copying the source.
            # We deliberately disable discard passdown so a guest cannot make
            # storage-specific discard semantics part of correctness.  The
            # destination must therefore contain deterministic zeroes before
            # the clone frontend can ever be published.  PREPARED is an
            # idempotent boundary: after interruption the whole exact range is
            # zeroed again, never assumed complete from a partial attempt.
            $class->_thick_activate_exact_lvs(
                $scfg, $vg, $device,
                "activating thick snapshot destination for zeroing failed",
                $tr->{new},
            );
            $class->_zero_new_thick_generation(
                "/dev/$vg/$tr->{new}", int($tr->{size}),
                "thick snapshot destination '$vg/$tr->{new}'",
            );
            run_command(
                ['/sbin/blockdev', '--flushbufs', "/dev/$vg/$tr->{new}"],
                errmsg => "flushing zeroed thick snapshot destination failed",
            );
            $class->_thick_deactivate_exact_lvs(
                $scfg, $vg, $device,
                "deactivating zeroed thick snapshot destination failed",
                $tr->{new},
            );
            $class->_thick_activate_exact_lvs(
                $scfg, $vg, $device,
                "activating dm-clone metadata failed",
                $tr->{meta},
            );
            run_command(
                ['/usr/bin/dd', 'if=/dev/zero', "of=/dev/$vg/$tr->{meta}",
                    'bs=4096', 'count=1', 'conv=fsync,nocreat', 'status=none'],
                errmsg => "initialising dm-clone metadata failed",
            );
            run_command(
                ['/sbin/blockdev', '--flushbufs', "/dev/$vg/$tr->{meta}"],
                errmsg => "flushing dm-clone metadata failed",
            );
        };
        die "PARTIAL SNAPSHOT for '$storeid:$volname': metadata initialisation is uncertain; "
            . "OPEN intent and all objects preserved; no retry or cleanup: $@" if $@;

        $class->_with_vg_lock($storeid, $scfg, sub {
            $class->_thick_require_transition_intent(
                $storeid, $scfg, $volname, \%intent,
            );
            my $lvs = $class->_thick_list_volumes_scoped($scfg, $vg, $device);
            my $info = $lvs->{$vg} && $lvs->{$vg}->{$tr->{anchor}};
            die "snapshot transition anchor disappeared\n" if !$info;
            my $state = decode_anchor_tags($info->{tags} // '');
            die "snapshot transition PREPARED state mismatch\n"
                if $state->{phase} ne 'PREPARED' || $state->{tx} ne $intent{tx}
                || $state->{head} ne $tr->{old} || $state->{old} ne $tr->{old}
                || $state->{new} ne $tr->{new}
                || $state->{region} != $tr->{geometry}->{region_sectors};
            for my $lv ($tr->{old}, $tr->{source}, $tr->{new}, $tr->{meta}) {
                die "snapshot transition object '$vg/$lv' is missing\n"
                    if !$lvs->{$vg}->{$lv};
            }
            validate_generation_tags(
                $lvs->{$vg}->{$tr->{new}}->{tags} // '', sid => $storeid,
                vol => $volname, role => 'head', generation => $tr->{new_gen},
            );
            $class->_thick_verify_transition_metadata(
                $storeid, $scfg, $volname, $lvs->{$vg}->{$tr->{meta}},
                tx => $intent{tx}, generation => $tr->{new_gen},
                region => $tr->{geometry}->{region_sectors},
                metadata_bytes => $tr->{geometry}->{metadata_bytes}, name => $tr->{meta},
                device => $device,
            );
            if (!$class->_thick_frontend_present($scfg, $volname)) {
                $class->_thick_activate_exact_lvs(
                    $scfg, $vg, $device,
                    "activating prepared thick-generations source failed",
                    $tr->{old}, $tr->{anchor},
                );
                my $uuid = 'SLT-TG2-' . object_key($namespace, $volname);
                my $sectors = int($tr->{old_size} / 512);
                $class->_thick_create_mapper_exact(
                    $scfg,
                    ['/sbin/dmsetup', '--verifyudev', 'create', $front, '--uuid', $uuid,
                        '--table', "0 $sectors linear /dev/$vg/$tr->{old} 0"],
                    "creating prepared thick-generations frontend failed",
                    sub { $class->_thick_verify_frontend(
                        $scfg, $volname, $tr->{old}, int($tr->{old_size} / 512),
                    ) },
                );
            } else {
                $class->_thick_verify_frontend(
                    $scfg, $volname, $tr->{old}, int($tr->{old_size} / 512),
                );
            }
            $class->_thick_activate_exact_lvs(
                $scfg, $vg, $device,
                "activating snapshot transition LVs failed",
                $tr->{source}, $tr->{new}, $tr->{meta},
            );
        # Construct the read-only source view while the old linear frontend is
        # still active.  No LVM command may run while that frontend is
        # suspended: udev may inspect the dependency chain and deadlock behind
        # the suspended device.  The source mapper is not published to the
        # frontend until the atomic cutover below.
            if (!$class->_thick_managed_mapper_present(
                $scfg, $tr->{source_map}, "SLT-TG3-SOURCE-$intent{tx}",
                "thick-generations source mapper '$tr->{source_map}'",
            )) {
                $class->_thick_create_mapper_exact(
                    $scfg,
                    ['/sbin/dmsetup', '--verifyudev', 'create', $tr->{source_map},
                        '--readonly', '--uuid', "SLT-TG3-SOURCE-$intent{tx}", '--table',
                        "0 " . int($tr->{size} / 512) . " linear /dev/$vg/$tr->{source} 0"],
                    "creating immutable snapshot source mapper failed",
                    sub { $class->_thick_verify_source_mapper(
                        $scfg, $tr->{source_map}, $tr->{source}, int($tr->{size} / 512),
                        $intent{tx},
                    ) },
                );
            } else {
                $class->_thick_verify_source_mapper(
                    $scfg, $tr->{source_map}, $tr->{source}, int($tr->{size} / 512),
                    $intent{tx},
                );
            }
            $state = $class->_thick_transition_anchor($scfg,
                $vg, $tr->{anchor}, $state, phase => 'SOURCE_READY', _device => $device,
            );
            $tr->{state} = $state;
            $class->_thick_fault_point('C4', $operation, $storeid, $volname);
            return;
        }, $device);
    }

    if ($tr->{state}->{phase} eq 'SOURCE_READY') {
        $class->_with_vg_lock($storeid, $scfg, sub {
        $class->_thick_require_transition_intent(
            $storeid, $scfg, $volname, \%intent,
        );
        my $lvs = $class->_thick_list_volumes_scoped($scfg, $vg, $device);
        my $info = $lvs->{$vg} && $lvs->{$vg}->{$tr->{anchor}};
        die "snapshot transition anchor disappeared before cutover\n" if !$info;
        my $state = decode_anchor_tags($info->{tags} // '');
        die "snapshot transition SOURCE_READY state mismatch\n"
            if $state->{phase} ne 'SOURCE_READY' || $state->{tx} ne $intent{tx}
            || $state->{head} ne $tr->{old} || $state->{old} ne $tr->{old}
            || $state->{new} ne $tr->{new};
        $class->_thick_verify_source_mapper(
            $scfg, $tr->{source_map}, $tr->{source}, int($tr->{size} / 512),
            $intent{tx},
        );
        if (!$class->_thick_mapper_is_suspended(
            $front, $class->_thick_command_deadline($scfg),
        )) {
            $class->_thick_suspend_mapper_exact(
                $scfg, $front,
                "suspending thick-generations frontend for snapshot failed",
            );
        }
        $class->_thick_fault_point('C5', $operation, $storeid, $volname);
        $state = $class->_thick_transition_anchor($scfg,
            $vg, $tr->{anchor}, $state, phase => 'COMMITTED',
            head => $tr->{new}, generation => $tr->{new_gen}, _device => $device,
        );
        if (!$rollback) {
            $class->_change_exact_tags($scfg,
                $vg, $tr->{old},
                PVE::SharedLvmThinThick::generation_tags(
                    sid => $storeid, vol => $volname, role => 'head',
                    generation => $tr->{old_gen},
                ),
                PVE::SharedLvmThinThick::generation_tags(
                    sid => $storeid, vol => $volname, role => 'snapshot',
                    generation => $tr->{old_gen}, snapshot => $snap,
                ),
                "committing immutable snapshot generation failed",
                $device,
            );
        }
        $class->_thick_fault_point('C6', $operation, $storeid, $volname);
        $tr->{state} = $state;
        return;
        }, $device);
    }

    if ($tr->{state}->{phase} eq 'COMMITTED') {
        $class->_with_vg_lock($storeid, $scfg, sub {
        $class->_thick_require_transition_intent(
            $storeid, $scfg, $volname, \%intent,
        );
        my $lvs = $class->_thick_list_volumes_scoped($scfg, $vg, $device);
        my $info = $lvs->{$vg} && $lvs->{$vg}->{$tr->{anchor}};
        die "snapshot transition anchor disappeared before clone publication\n" if !$info;
        my $state = decode_anchor_tags($info->{tags} // '');
        die "snapshot transition COMMITTED state mismatch\n"
            if $state->{phase} ne 'COMMITTED' || $state->{tx} ne $intent{tx}
            || $state->{head} ne $tr->{new} || $state->{old} ne $tr->{old}
            || $state->{new} ne $tr->{new};
        $class->_thick_verify_source_mapper(
            $scfg, $tr->{source_map}, $tr->{source}, int($tr->{size} / 512),
            $intent{tx},
        );
        my $sectors = int($tr->{size} / 512);
        if ($class->_thick_mapper_is_suspended(
            $front, $class->_thick_command_deadline($scfg),
        )) {
            my ($threshold, $batch) = $class->_thick_hydration_tuning(
                $scfg, $tr->{geometry}->{region_sectors},
            );
            $class->_thick_verify_frontend(
                $scfg, $volname, $tr->{old}, int($tr->{old_size} / 512), 'suspended',
            );
            my $command_timeout = $class->_thick_command_deadline($scfg);
            my $meta_devno = $class->_thick_verify_active_lv_identity(
                $scfg, $vg, $tr->{meta}, $device, $command_timeout,
            );
            my $new_devno = $class->_thick_verify_active_lv_identity(
                $scfg, $vg, $tr->{new}, $device, $command_timeout,
            );
            my $source_devno = $class->_thick_verify_source_mapper(
                $scfg, $tr->{source_map}, $tr->{source}, $sectors, $intent{tx},
            );
            my $clone_table =
                "0 $sectors clone $meta_devno $new_devno $source_devno "
                . $tr->{geometry}->{region_sectors} . " "
                . "2 no_hydration no_discard_passdown "
                . "4 hydration_threshold $threshold hydration_batch_size $batch";
            $class->_thick_load_inactive_table_exact(
                $scfg, 'load', $front, $clone_table,
                sub {
                    my ($inactive) = @_;
                    die "inactive dm-clone table postcondition failed\n"
                        if @$inactive != 1
                        || $inactive->[0] ne $clone_table;
                    return 1;
                },
                "loading dm-clone snapshot transition failed",
            );
            $class->_thick_resume_mapper_exact(
                $scfg, $front,
                sub {
                    $class->_thick_verify_clone_frontend(
                        $scfg, $volname, sectors => $sectors,
                        region => $tr->{geometry}->{region_sectors}, meta => $tr->{meta},
                        new => $tr->{new}, source_map => $tr->{source_map},
                        source => $tr->{source}, tx => $intent{tx},
                    );
                    $class->_thick_verify_clone_status(
                        $front, 0, $sectors, $tr->{geometry}->{region_sectors},
                        $class->_thick_command_deadline($scfg),
                    );
                    return 1;
                },
                "publishing dm-clone snapshot transition failed",
            );
        }
        if (!$rollback) {
            $class->_thick_ensure_snapshot_readonly(
                $scfg, $vg, $tr->{old}, $lvs->{$vg}->{$tr->{old}}, $device,
            );
        } else {
            $class->_thick_verify_snapshot_readonly($scfg, $vg, $tr->{source}, $device);
        }
        $class->_thick_fault_point('C7', $operation, $storeid, $volname);
        $state = $class->_thick_transition_anchor($scfg,
            $vg, $tr->{anchor}, $state, phase => 'HYDRATING', _device => $device,
        );
        $tr->{state} = $state;
        $class->_thick_fault_point('C8', $operation, $storeid, $volname);
        return;
        }, $device);
    }

    if ($tr->{state}->{phase} eq 'HYDRATING') {
        $class->_thick_enable_background_hydration(
            $front, $class->_thick_command_deadline($scfg),
            int($tr->{size} / 512), $tr->{geometry}->{region_sectors},
        );
        if ($async_snapshot) {
            my $timeout = $scfg->{'slt-tg-hydration-timeout'} // 3600;
            $class->_with_vg_lock($storeid, $scfg, sub {
                $class->_thick_scope_transition_intent_to_anchor(
                    $storeid, $scfg, $volname, \%intent,
                );
                return;
            }, $device);
            my $scheduled = eval {
                $class->_thick_schedule_materialization(
                    $scfg, $storeid, $volname, $snap, $operation, $intent{tx}, $timeout,
                );
                1;
            };
            if ($scheduled) {
                # COMMITTED data and an exact persistent clone mapping are
                # already authoritative.  Returning now lets PVE resume QEMU;
                # the transaction-scoped worker performs bounded hydration,
                # the linear pivot, and exact cleanup. The signed non-
                # MATERIALIZED anchor blocks another mutation of this volume,
                # while independent volumes in the same VG can participate in
                # the same PVE multi-disk snapshot operation.
                return;
            }
            # systemd-run is a mutating request.  A transport/client error does
            # not prove that systemd rejected it: the exact transaction worker
            # may already be queued or running.  Never start a synchronous
            # second owner after that ambiguous boundary.  Persistent anchor
            # and intent evidence make an explicit resume safe and repeatable.
            die "asynchronous Thick Generations materialization scheduling was not "
                . "confirmed; transaction '$intent{tx}' is preserved. Inspect the exact "
                . "pve-sharedlvmthin-tg-$intent{tx} service/timer and run sharedlvmthin "
                . "thick-resume '$storeid' '$volname': $@";
        }
        $class->_thick_wait_for_hydration(
            $front, $scfg->{'slt-tg-hydration-timeout'} // 3600,
            $class->_thick_command_deadline($scfg),
            int($tr->{size} / 512), $tr->{geometry}->{region_sectors},
        );

        $class->_with_vg_lock($storeid, $scfg, sub {
        $class->_thick_require_transition_intent(
            $storeid, $scfg, $volname, \%intent,
        );
        my $lvs = $class->_thick_list_volumes_scoped($scfg, $vg, $device);
        my $state = decode_anchor_tags($lvs->{$vg}->{$tr->{anchor}}->{tags} // '');
        die "snapshot transition changed before hydration completion\n"
            if $state->{phase} ne 'HYDRATING' || $state->{tx} ne $intent{tx}
            || $state->{head} ne $tr->{new};
        my $already_suspended = $class->_thick_mapper_is_suspended(
            $front, $class->_thick_command_deadline($scfg),
        );
        $class->_thick_verify_clone_frontend(
            $scfg, $volname, sectors => int($tr->{size} / 512),
            region => $tr->{geometry}->{region_sectors}, meta => $tr->{meta},
            new => $tr->{new}, source_map => $tr->{source_map},
            source => $tr->{source}, tx => $intent{tx},
            runtime => $already_suspended ? 'suspended' : 'active',
        );
        $class->_thick_verify_clone_status(
            $front, 1, int($tr->{size} / 512), $tr->{geometry}->{region_sectors},
            $class->_thick_command_deadline($scfg),
        );
        $state = $class->_thick_transition_anchor($scfg,
            $vg, $tr->{anchor}, $state, phase => 'HYDRATION_COMPLETE', _device => $device,
        );
        $tr->{state} = $state;
        $class->_thick_fault_point('C9', $operation, $storeid, $volname);
        return;
        }, $device);
    }

    if ($tr->{state}->{phase} eq 'HYDRATION_COMPLETE') {
        $class->_with_vg_lock($storeid, $scfg, sub {
        $class->_thick_require_transition_intent(
            $storeid, $scfg, $volname, \%intent,
        );
        my $lvs = $class->_thick_list_volumes_scoped($scfg, $vg, $device);
        my $state = decode_anchor_tags($lvs->{$vg}->{$tr->{anchor}}->{tags} // '');
        die "snapshot transition changed before linear pivot\n"
            if $state->{phase} ne 'HYDRATION_COMPLETE' || $state->{tx} ne $intent{tx}
            || $state->{head} ne $tr->{new};
        my $already_suspended = $class->_thick_mapper_is_suspended(
            $front, $class->_thick_command_deadline($scfg),
        );
        $class->_thick_verify_clone_frontend(
            $scfg, $volname, sectors => int($tr->{size} / 512),
            region => $tr->{geometry}->{region_sectors}, meta => $tr->{meta},
            new => $tr->{new}, source_map => $tr->{source_map},
            source => $tr->{source}, tx => $intent{tx},
            runtime => $already_suspended ? 'suspended' : 'active',
        );
        $class->_thick_verify_clone_status(
            $front, 1, int($tr->{size} / 512), $tr->{geometry}->{region_sectors},
            $class->_thick_command_deadline($scfg),
        );
        my $sectors = int($tr->{size} / 512);
        my $new_devno = $class->_thick_verify_active_lv_identity(
            $scfg, $vg, $tr->{new}, $device, $class->_thick_command_deadline($scfg),
        );
        $class->_thick_load_inactive_table_exact(
            $scfg, 'reload', $front, "0 $sectors linear $new_devno 0",
            sub {
                my ($inactive) = @_;
                die "inactive linear pivot table postcondition failed\n"
                    if @$inactive != 1
                    || $inactive->[0] ne "0 $sectors linear $new_devno 0";
                return 1;
            },
            "loading canonical linear frontend failed",
        );
        if (!$already_suspended) {
            $class->_thick_suspend_mapper_exact(
                $scfg, $front,
                "suspending hydrated frontend for linear pivot failed",
            );
        }
        $class->_thick_fault_point('C10', $operation, $storeid, $volname);
        # Re-read the still-active clone target after I/O has drained and
        # before the inactive linear table is published. This closes the race
        # in which metadata could enter ro/Fail after the earlier completion
        # observation. A crash here leaves an exactly classifiable suspended
        # clone plus the deterministic inactive table; resume repeats no data
        # mutation and can safely continue this boundary.
        $class->_thick_verify_clone_status(
            $front, 1, $sectors, $tr->{geometry}->{region_sectors},
            $class->_thick_command_deadline($scfg),
        );
        $class->_thick_resume_mapper_exact(
            $scfg, $front,
            sub {
                return $class->_thick_verify_frontend(
                    $scfg, $volname, $tr->{new}, $sectors,
                );
            },
            "publishing canonical linear frontend failed",
        );
        $state = $class->_thick_transition_anchor($scfg,
            $vg, $tr->{anchor}, $state, phase => 'LINEAR_PIVOTED', _device => $device,
        );
        $tr->{state} = $state;
        return;
        }, $device);
    }

    if ($tr->{state}->{phase} eq 'LINEAR_PIVOTED') {
        $class->_with_vg_lock($storeid, $scfg, sub {
        $class->_thick_require_transition_intent(
            $storeid, $scfg, $volname, \%intent,
        );
        my $lvs = $class->_thick_list_volumes_scoped($scfg, $vg, $device);
        my $info = $lvs->{$vg} && $lvs->{$vg}->{$tr->{anchor}};
        die "linear-pivoted transition anchor disappeared before cleanup\n" if !$info;
        my $state = decode_anchor_tags($info->{tags} // '');
        die "linear-pivoted transition state mismatch\n"
            if $state->{phase} ne 'LINEAR_PIVOTED' || $state->{tx} ne $intent{tx}
            || $state->{head} ne $tr->{new} || $state->{old} ne $tr->{old}
            || $state->{new} ne $tr->{new};
        $class->_thick_verify_frontend(
            $scfg, $volname, $tr->{new}, int($tr->{size} / 512),
        );
        my $after;
        if ($lvs->{$vg}->{$tr->{meta}}) {
            $class->_thick_verify_transition_metadata(
                $storeid, $scfg, $volname, $lvs->{$vg}->{$tr->{meta}},
                tx => $intent{tx}, generation => $tr->{new_gen},
                region => $tr->{geometry}->{region_sectors},
                metadata_bytes => $tr->{geometry}->{metadata_bytes}, name => $tr->{meta},
                device => $device,
            );
        }
        $class->_thick_verify_snapshot_readonly($scfg, $vg, $tr->{source}, $device);
        if ($class->_thick_managed_mapper_present(
            $scfg, $tr->{source_map}, "SLT-TG3-SOURCE-$intent{tx}",
            "thick-generations source mapper '$tr->{source_map}'",
        )) {
            $class->_thick_verify_source_mapper(
                $scfg, $tr->{source_map}, $tr->{source}, int($tr->{size} / 512),
                $intent{tx},
            );
            $class->_thick_remove_mapper_exact(
                $scfg,
                ['/sbin/dmsetup', '--verifyudev', 'remove', $tr->{source_map}],
                "removing detached snapshot source mapper failed",
                sub {
                    return $class->_thick_verify_mapper_absent(
                        $scfg, $tr->{source_map},
                        "detached snapshot source mapper still exists after removal",
                    );
                },
            );
        }
        if ($lvs->{$vg}->{$tr->{meta}}) {
            $class->_thick_deactivate_exact_lvs(
                $scfg, $vg, $device,
                "deactivating detached dm-clone metadata failed",
                $tr->{meta},
            );
            $after = $class->_thick_remove_exact_lv(
                $scfg, $vg, $device, $tr->{meta},
                "removing detached dm-clone metadata failed",
            );
        } else {
            $after = $class->_thick_list_volumes_scoped($scfg, $vg, $device);
        }
        die "detached dm-clone metadata still exists after removal\n"
            if $after->{$vg} && $after->{$vg}->{$tr->{meta}};
        if ($rollback) {
            if ($after->{$vg} && $after->{$vg}->{$tr->{old}}) {
                validate_generation_tags(
                    $after->{$vg}->{$tr->{old}}->{tags} // '', sid => $storeid,
                    vol => $volname, role => 'head', generation => $tr->{old_gen},
                );
                $class->_thick_deactivate_exact_lvs(
                    $scfg, $vg, $device,
                    "deactivating superseded rollback HEAD failed",
                    $tr->{old},
                );
                $after = $class->_thick_remove_exact_lv(
                    $scfg, $vg, $device, $tr->{old},
                    "removing superseded rollback HEAD failed",
                );
            }
            die "superseded rollback HEAD still exists after removal\n"
                if $after->{$vg} && $after->{$vg}->{$tr->{old}};
            my ($kept_snapshot) = $class->_thick_find_snapshot(
                $storeid, $scfg, $volname, $tr->{snapshot}, $after,
            );
            die "rollback source snapshot identity changed\n"
                if $kept_snapshot ne $tr->{source};
        }
        $state = $class->_thick_transition_anchor($scfg,
            $vg, $tr->{anchor}, $state, phase => 'MATERIALIZED', _device => $device,
        );
        $tr->{state} = $state;
        $class->_clear_vg_intent($scfg, $vg, %intent, _device => $device)
            if !$intent{_anchor_scoped};
        return;
    }, $device);
    }
    return;
}

# This is a narrowly qualified in-process PVE call contract, not authority from
# argv, process names, disk ownership, or a persistent bypass flag. Unknown
# upstream stack/argument shapes refuse referenced-tree deletion.
sub _thick_destroy_call_frames {
    my @frames;
    for my $depth (0 .. 95) {
        my (@frame, @args);
        {
            package DB;
            @frame = caller($depth);
            @args = @DB::args;
        }
        last if !@frame;
        push @frames, { sub => $frame[3], args => [@args], package => $frame[0], file => $frame[1] };
    }
    die "qmdestroy caller chain exceeds admission budget\n" if @frames == 96;
    return \@frames;
}

sub _thick_validate_destroy_frames {
    my ($class, $storeid, $volname, $frames) = @_;
    my ($vmid) = $volname =~ /^vm-([1-9][0-9]*)-disk-[0-9]+$/;
    die "referenced-tree delete requires a canonical VM disk\n" if !$vmid;
    my @wanted = (__PACKAGE__ . '::free_image', 'PVE::Storage::vdisk_free',
        'PVE::QemuServer::destroy_vm', 'PVE::AbstractConfig::lock_config_full',
        'PVE::AbstractConfig::lock_config');
    my (%found, $previous);
    for my $name (@wanted) {
        my @indices = grep { ($frames->[$_]->{sub} // '') eq $name } 0 .. $#$frames;
        die "unqualified qmdestroy caller chain: '$name'\n"
            if @indices != 1 || (defined($previous) && $indices[0] <= $previous);
        $previous = $indices[0];
        $found{$name} = $frames->[$indices[0]]->{args};
    }
    my $free = $found{$wanted[0]};
    my $disk = $found{$wanted[1]};
    my $destroy = $found{$wanted[2]};
    my $full = $found{$wanted[3]};
    my $lock = $found{$wanted[4]};
    die "qmdestroy public free_image arguments changed\n"
        if @$free != 6 || $free->[0] ne __PACKAGE__ || $free->[1] ne $storeid
        || ref($free->[2]) ne 'HASH' || $free->[3] ne $volname || $free->[4]
        || ($free->[5] // '') ne 'raw';
    die "qmdestroy vdisk_free identity changed\n"
        if @$disk != 2 || ref($disk->[0]) ne 'HASH' || $disk->[1] ne "$storeid:$volname"
        || ref($disk->[0]->{ids}) ne 'HASH' || !exists($disk->[0]->{ids}->{$storeid})
        || $disk->[0]->{ids}->{$storeid} ne $free->[2];
    die "qmdestroy exact non-skiplock destroy contract is absent\n"
        if @$destroy != 5 || ref($destroy->[0]) ne 'HASH' || $destroy->[0] ne $disk->[0]
        || $destroy->[1] ne $vmid
        || $destroy->[2] || ref($destroy->[3]) ne 'HASH'
        || keys(%{$destroy->[3]}) != 1 || ($destroy->[3]->{lock} // '') ne 'destroyed';
    die "qmdestroy enclosing VM config lock is absent\n"
        if @$lock != 3 || $lock->[0] ne 'PVE::QemuConfig' || $lock->[1] ne $vmid
        || ref($lock->[2]) ne 'CODE';
    die "qmdestroy full config lock contract changed\n"
        if @$full != 4 || $full->[0] ne $lock->[0] || $full->[1] ne $vmid
        || $full->[2] ne '10' || $full->[3] ne $lock->[2];
    return $vmid;
}

sub _thick_validate_destroy_config {
    my ($class, $storeid, $volname, $text, $snapshots) = @_;
    die "qmdestroy config is empty, oversized or contains binary data\n"
        if !defined($text) || !length($text) || length($text) > 4 * 1024 * 1024
        || $text =~ /[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/;
    my ($section, $references) = ('', 0);
    my (%sections, %keys);
    my $volid = "$storeid:$volname";
    for my $line (split(/\n/, $text)) {
        next if $line =~ /^\s*$/;
        # Comments containing the identity are intentionally ambiguous too.
        next if $line =~ /^#/ && index($line, $volid) < 0;
        if ($line =~ /^\[([^\]]+)\]\s*$/) {
            $section = $1;
            die "qmdestroy pending/special/ambiguous section is not admissible\n"
                if $section eq 'PENDING' || $section =~ /^special:/ || $sections{$section}++;
            _thick_snapshot_name($section);
            next;
        }
        die "qmdestroy config has an unknown or duplicate field\n"
            if $line !~ /^([A-Za-z][A-Za-z0-9_-]*):\s*(.*?)\s*$/
            || $keys{"$section:$1"}++;
        my ($key, $value) = ($1, $2);
        die "qmdestroy config contains a lock or unfinished snapshot\n"
            if $key eq 'lock' || $key eq 'snapstate';
        die "qmdestroy config is protected or a template\n"
            if ($key eq 'template' || $key eq 'protection') && $value ne '0';
        next if index($value, $volid) < 0;
        die "qmdestroy volume reference has unknown semantics\n"
            if $key !~ /^(?:(?:ide|sata|scsi|virtio|unused)[0-9]+|efidisk0|tpmstate0)$/
            || $value !~ /^\Q$volid\E(?:,[^\s]*)?$/ || $value =~ /(?:^|,)media=cdrom(?:,|$)/;
        die "qmdestroy config references an unsigned snapshot\n"
            if length($section) && !exists($snapshots->{$section});
        $references++;
    }
    die "qmdestroy scanner/config reference mismatch\n" if !$references;
    return sha256_hex($text);
}

sub _thick_owner_reference_text {
    my ($class, $admission, $refs) = @_;
    my $path = $admission->{path};
    die "qmdestroy has foreign or ambiguous PVE references\n" if @$refs != 1
        || ($refs->[0] ne $path && $refs->[0] ne "/etc/pve/qemu-server/$admission->{vmid}.conf");
    my @before = lstat($path);
    die "qmdestroy owner config is missing or unsafe\n"
        if !@before || !S_ISREG($before[2]);
    my @reference = stat($refs->[0]);
    die "qmdestroy reference does not identify the canonical owner config\n"
        if !@reference || $reference[0] != $before[0] || $reference[1] != $before[1];
    open(my $fh, '<', $path) or die "opening qmdestroy owner config failed: $!\n";
    my @opened = stat($fh);
    die "qmdestroy owner config changed while opening\n"
        if !@opened || $opened[0] != $before[0] || $opened[1] != $before[1];
    my $text = '';
    my $bytes = read($fh, $text, 4 * 1024 * 1024 + 1);
    die "reading qmdestroy owner config failed\n" if !defined($bytes);
    close($fh) or die "closing qmdestroy owner config failed: $!\n";
    my @after = lstat($path);
    die "qmdestroy owner config changed while reading\n"
        if !@after || !S_ISREG($after[2]) || $after[0] != $before[0]
        || $after[1] != $before[1] || $after[7] != $before[7]
        || $after[9] != $before[9] || $bytes != $after[7];
    return $text;
}

sub _thick_destroy_reference_digest {
    my ($class, $storeid, $volname, $admission, $refs) = @_;
    return $class->_thick_validate_destroy_config($storeid, $volname,
        $class->_thick_owner_reference_text($admission, $refs), $admission->{snapshots});
}

sub _thick_tree_delete_admission {
    my ($class, $storeid, $volname, $plan) = @_;
    my $refs = $class->_thick_pve_reference_files($storeid, $volname);
    return undef if !@$refs;
    my $vmid = $class->_thick_validate_destroy_frames(
        $storeid, $volname, $class->_thick_destroy_call_frames());
    my $node = $class->_thin_local_node();
    die "qmdestroy local node is ambiguous\n" if $node !~ /^[A-Za-z0-9][A-Za-z0-9_.-]*$/;
    my $admission = { vmid => $vmid, path => "/etc/pve/nodes/$node/qemu-server/$vmid.conf",
        snapshots => { %{$plan->{snapshots}} } };
    $admission->{digest} = $class->_thick_destroy_reference_digest($storeid, $volname, $admission, $refs);
    return $admission;
}

sub _thick_assert_tree_references {
    my ($class, $storeid, $volname, $admission) = @_;
    my $refs = $class->_thick_pve_reference_files($storeid, $volname);
    if ($admission) {
        $class->_thick_validate_destroy_frames($storeid, $volname, $class->_thick_destroy_call_frames());
        die "qmdestroy owner config changed during tree deletion\n"
            if $class->_thick_destroy_reference_digest($storeid, $volname, $admission, $refs)
                ne $admission->{digest};
    } else {
        die "Thick tree removal refused: PVE still references '$storeid:$volname'\n" if @$refs;
    }
    return;
}

sub _thick_tree_plan {
    my ($class, $storeid, $scfg, $volname, $lvs) = @_;
    my ($state, undef, $anchor) = $class->_thick_anchor($storeid, $scfg, $volname, $lvs);
    my $namespace = $class->_thick_namespace($scfg);
    my $key = object_key($namespace, $volname);
    my $objects = $lvs->{$scfg->{'slt-vgname'}} // {};
    my (%entries, %snapshots, %uuids);
    for my $name (sort keys %$objects) {
        my $info = $objects->{$name};
        my $tags = $info->{tags} // '';
        my $named = $name =~ /^sltg-[A-Za-z]+-\Q$key\E(?:-|$)/;
        my $tagged = $tags =~ /(?:^|,)slt_[^,=]*_sid=\Q$storeid\E(?:,|$)/
            && $tags =~ /(?:^|,)slt_[^,=]*_vol=\Q$volname\E(?:,|$)/;
        next if !$named && !$tagged;
        die "Thick tree contains transition metadata or an unexpected owned object '$name'\n"
            if $name ne $anchor && $name !~ /^sltg-g-\Q$key\E-\d{8}$/;
        my $uuid = $info->{lv_uuid} // '';
        (my $normalized = $uuid) =~ s/-//g;
        die "Thick tree has ambiguous LV identity for '$name'\n"
            if $uuid !~ /^[A-Za-z0-9-]+$/ || !$normalized || $uuids{$normalized}++;
        die "Thick tree has invalid size for '$name'\n"
            if ($info->{lv_size} // '') !~ /^[1-9][0-9]*$/;
        $entries{$name} = { uuid => $uuid, bytes => $info->{lv_size}, tags => $tags };
        next if $name eq $anchor || $name eq $state->{head};
        my $owned = decode_generation_tags($tags);
        die "Thick tree has ambiguous snapshot ownership for '$name'\n"
            if $owned->{sid} ne $storeid || $owned->{vol} ne $volname
            || $owned->{role} ne 'snapshot'
            || generation_name($namespace, $volname, $owned->{generation}) ne $name;
        my $snap = _thick_snapshot_name($owned->{snapshot});
        die "Thick tree has duplicate snapshot name '$snap'\n" if exists($snapshots{$snap});
        $snapshots{$snap} = $name;
    }
    die "Thick tree is missing its exact anchor or HEAD\n"
        if !$entries{$anchor} || !$entries{$state->{head}};
    die "Thick tree exceeds automatic removal budget; use explicit recovery\n"
        if keys(%snapshots) > 32;
    return { anchor => $anchor, head => $state->{head}, generation => $state->{generation},
        entries => \%entries, snapshots => \%snapshots };
}

# A complete kernel inventory, not /dev symlink absence. No activation or
# remote shell interpolation is used; names/UUIDs are compared locally.
sub _thick_tree_kernel_rows {
    my ($class, $ssh) = @_;
    my @command = (@{$ssh // []}, '/usr/bin/env', 'LC_ALL=C', '/sbin/dmsetup',
        'info', '-c', '--noheadings', '--separator=:', '-o', 'name,uuid,open');
    my (@rows, @errors);
    run_command(\@command, timeout => 5,
        outfunc => sub { push @rows, $_[0]; die "oversized Thick tree kernel inventory\n" if @rows > 8192; },
        errfunc => sub { push @errors, $_[0]; });
    die "Thick tree kernel inventory has diagnostics\n" if @errors;
    return [] if @rows == 1 && $rows[0] =~ /^\s*No devices found\s*$/;
    my (%names, @parsed);
    for my $line (@rows) {
        my @fields = split(/:/, $line, -1);
        s/^\s+|\s+$//g for @fields;
        die "malformed Thick tree kernel inventory\n"
            if @fields != 3 || $fields[0] !~ /^[A-Za-z0-9_.+-]+$/
            || $fields[1] !~ /^[A-Za-z0-9_.+-]*$/ || $fields[2] !~ /^\d+$/
            || $names{$fields[0]}++;
        push @parsed, \@fields;
    }
    return \@parsed;
}

sub _thick_tree_check_kernel_rows {
    my ($class, $scfg, $volname, $plan, $rows, $remote) = @_;
    my $vg = $scfg->{'slt-vgname'};
    (my $vg_uuid = $scfg->{'slt-expected-vg-uuid'}) =~ s/-//g;
    my (%expected, %uuid_names);
    for my $name (keys %{$plan->{entries}}) {
        (my $dm = "$vg/$name") =~ s/-/--/g;
        $dm =~ s{/}{-};
        (my $uuid = $plan->{entries}->{$name}->{uuid}) =~ s/-//g;
        $expected{$dm} = "LVM-$vg_uuid$uuid";
    }
    my $front = mapper_name($class->_thick_namespace($scfg), $volname);
    $expected{$front} = 'SLT-TG2-' . object_key($class->_thick_namespace($scfg), $volname);
    $uuid_names{$expected{$_}} = $_ for keys %expected;
    my %observed;
    for my $row (@$rows) {
        my ($name, $uuid, $opens) = @$row;
        next if !exists($expected{$name}) && !exists($uuid_names{$uuid});
        die "Thick tree mapper identity conflict on '$name'\n"
            if !exists($expected{$name}) || $expected{$name} ne $uuid || exists($observed{$name});
        die "Thick tree is still mapped on a peer\n" if $remote;
        $observed{$name} = $opens;
    }
    if (exists($observed{$front})) {
        die "Thick tree HEAD has an open frontend\n" if $observed{$front} != 0;
        $class->_thick_verify_frontend($scfg, $volname, $plan->{head});
    }
    (my $head_dm = "$vg/$plan->{head}") =~ s/-/--/g;
    $head_dm =~ s{/}{-};
    for my $name (keys %observed) {
        next if $name eq $front;
        my $expected_opens = $name eq $head_dm && exists($observed{$front}) ? 1 : 0;
        die "Thick tree has an open or ambiguous backing '$name'\n"
            if $observed{$name} != $expected_opens;
    }
    return 1;
}

sub _thick_tree_peer_absence {
    my ($class, $scfg, $volname, $plan) = @_;
    return 1 if !$scfg->{shared};
    PVE::Cluster::cfs_update();
    my $members = PVE::Cluster::get_members();
    my $nodes = PVE::Cluster::get_nodelist();
    my $local = $class->_thin_local_node();
    die "Thick tree cluster coverage is unavailable\n"
        if ref($members) ne 'HASH' || ref($nodes) ne 'ARRAY' || !@$nodes || @$nodes > 16;
    my %seen;
    my $deadline = $class->_thick_progress_clock() + 60;
    for my $node (sort @$nodes) {
        die "Thick tree cluster node identity is ambiguous\n"
            if $node !~ /^[A-Za-z0-9][A-Za-z0-9_.-]*$/ || $seen{$node}++;
        next if $node eq $local;
        my $member = $members->{$node};
        die "Thick tree peer '$node' is offline or unknown\n"
            if ref($member) ne 'HASH' || !$member->{online}
            || ($member->{ip} // '') !~ /^[A-Fa-f0-9:.]+$/;
        die "Thick tree peer proof exceeded its observation budget\n"
            if $class->_thick_progress_clock() >= $deadline;
        my $ssh = PVE::SSHInfo::ssh_info_to_command(
            { name => $node, ip => $member->{ip} }, '-o', 'BatchMode=yes',
            '-o', 'ConnectTimeout=3', '-o', 'NumberOfPasswordPrompts=0');
        push @$ssh, '--';
        $class->_thick_tree_check_kernel_rows($scfg, $volname, $plan,
            $class->_thick_tree_kernel_rows($ssh), 1);
    }
    die "Thick tree local node is absent from configured membership\n" if !$seen{$local};
    return 1;
}

sub _thick_remove_unreferenced_tree_locked {
    my ($class, $storeid, $scfg, $volname, $lvs) = @_;
    my $vg = $scfg->{'slt-vgname'};
    my $device = "/dev/mapper/$scfg->{'slt-expected-wwid'}";
    my $plan = $class->_thick_tree_plan($storeid, $scfg, $volname, $lvs);
    my $admission = $class->_thick_tree_delete_admission($storeid, $volname, $plan);
    my %remaining = %{$plan->{entries}};
    my $check = sub {
        $class->_verify_mutation_quorum($storeid, $scfg);
        $class->_verify_storage_identity($storeid, $scfg, $device);
        $class->_require_no_vg_intent($scfg, $vg, $device);
        $class->_assert_no_active_storage_worker($vg);
        $class->_thick_assert_tree_references($storeid, $volname, $admission);
        my $current = $class->_thick_tree_plan($storeid, $scfg, $volname,
            $class->_thick_list_volumes_scoped($scfg, $vg, $device));
        die "Thick tree membership or HEAD changed during removal\n"
            if $current->{head} ne $plan->{head} || $current->{anchor} ne $plan->{anchor}
            || $current->{generation} != $plan->{generation}
            || join('|', sort keys %remaining) ne join('|', sort keys %{$current->{entries}});
        for my $name (keys %remaining) {
            my $now = $current->{entries}->{$name};
            my $before = $remaining{$name};
            die "Thick tree object identity changed for '$name'\n"
                if $now->{uuid} ne $before->{uuid} || $now->{bytes} != $before->{bytes}
                || ($name ne $plan->{anchor} && $now->{tags} ne $before->{tags});
            $class->_thick_verify_autoactivation_disabled($scfg, $vg, $name, $device);
        }
        for my $name (values %{$current->{snapshots}}) {
            $class->_thick_verify_snapshot_readonly($scfg, $vg, $name, $device);
        }
        $class->_thick_tree_check_kernel_rows($scfg, $volname, $current,
            $class->_thick_tree_kernel_rows(), 0);
        return $current;
    };
    # Every object, including HEAD and the LAST snapshot, is checked before
    # the first REMOVE_SNAPSHOT intent. Remote UNKNOWN is never absence.
    my $current = $check->();
    $class->_thick_tree_peer_absence($scfg, $volname, $current);
    for my $snap (sort { $plan->{snapshots}->{$a} cmp $plan->{snapshots}->{$b} }
        keys %{$plan->{snapshots}}) {
        $check->();
        $class->_thick_volume_snapshot_delete_locked($scfg, $storeid, $volname, $snap, 1, $admission);
        delete $remaining{$plan->{snapshots}->{$snap}};
    }
    $check->();
    return $admission;
}

sub _thick_free_image {
    my ($class, $storeid, $scfg, $volname, $isBase, $orphan_alloc, $orphan_tree) = @_;
    $class->_require_thick_identity_config($storeid, $scfg);
    my $vg = $scfg->{'slt-vgname'};
    my $device = "/dev/mapper/$scfg->{'slt-expected-wwid'}";
    return $class->_with_vg_lock($storeid, $scfg, sub {
        $class->_require_no_vg_intent($scfg, $vg, $device);
        my $lvs = $class->_thick_list_volumes_scoped($scfg, $vg, $device);
        my $admission;
        my $key = object_key($class->_thick_namespace($scfg), $volname);
        my @generations = grep { /^sltg-g-\Q$key\E-\d{8}$/ } keys %{$lvs->{$vg} // {}};
        my @related = grep {
            my $tags = $lvs->{$vg}->{$_}->{tags} // '';
            /^sltg-[A-Za-z]+-\Q$key\E(?:-|$)/
                || ($tags =~ /(?:^|,)slt_[^,=]*_sid=\Q$storeid\E(?:,|$)/
                    && $tags =~ /(?:^|,)slt_[^,=]*_vol=\Q$volname\E(?:,|$)/)
        } keys %{$lvs->{$vg} // {}};
        if ((@generations > 1 || @related > 2) && !$orphan_alloc && !$orphan_tree) {
            die "automatic Thick tree removal refuses base volumes\n" if $isBase;
            $admission = $class->_thick_remove_unreferenced_tree_locked($storeid, $scfg, $volname, $lvs);
            $lvs = undef;
            $orphan_tree = 1;
        }
        return $class->_thick_free_image_single_locked(
            $storeid, $scfg, $volname, $isBase, $orphan_alloc, $orphan_tree, $lvs, $admission,
        );
    }, $device);
}

# Caller owns the canonical VG lock for the complete tree operation. Never
# re-enter the public wrappers from here: PVE storage locks are not recursive.
sub _thick_free_image_single_locked {
    my ($class, $storeid, $scfg, $volname, $isBase, $require_orphan_alloc,
        $require_orphan_tree, $initial_lvs, $admission) = @_;
    $class->_require_thick_identity_config($storeid, $scfg);

    my $vg = $scfg->{'slt-vgname'};
    my $device = "/dev/mapper/$scfg->{'slt-expected-wwid'}";
    my $command_timeout = $class->_thick_command_deadline($scfg);
    my $namespace = $class->_thick_namespace($scfg);
    my $mapper = mapper_name($namespace, $volname);

    {
        $class->_require_no_vg_intent($scfg, $vg, $device);
        my $lvs = $initial_lvs // $class->_thick_list_volumes_scoped($scfg, $vg, $device);
        die "thick-generations storage '$storeid' is unavailable: VG '$vg' is not visible\n"
            if !$lvs->{$vg};

        my ($state, undef, $anchor) =
            $class->_thick_anchor($storeid, $scfg, $volname, $lvs);
        if ($require_orphan_alloc || $require_orphan_tree) {
            die "orphan recovery requires a canonical materialized ALLOC state\n"
                if $state->{phase} ne 'MATERIALIZED' || $state->{op} ne 'ALLOC'
                || $state->{snapshot} ne 'none';
            die "orphan-allocation recovery requires generation zero\n"
                if $require_orphan_alloc && $state->{generation} != 0;
            $class->_thick_assert_tree_references($storeid, $volname, $admission);
        }
        my $head = $state->{head};
        my $key = object_key($namespace, $volname);
        my @generations = sort grep { /^sltg-g-\Q$key\E-\d{8}$/ } keys %{$lvs->{$vg}};
        die "refusing to delete thick-generations volume '$volname': "
            . "owned snapshots or ambiguous generations remain\n"
            if @generations != 1 || $generations[0] ne $head;
        validate_generation_tags(
            $lvs->{$vg}->{$head}->{tags} // '', sid => $storeid, vol => $volname,
            role => 'head', generation => $state->{generation},
        );
        $class->_thick_verify_autoactivation_disabled($scfg, $vg, $head, $device);
        $class->_thick_verify_autoactivation_disabled($scfg, $vg, $anchor, $device);

        # PVE can call free_image() directly after cancelling a storage mirror
        # without first calling deactivate_volume().  The stable frontend is
        # therefore not ownership proof that the target is still in use.  It
        # is safe to dismantle only after exact identity/dependency checks and
        # a positively verified zero open count.  An open or ambiguous mapper
        # remains a hard refusal.
        if ($class->_thick_frontend_present($scfg, $volname)) {
            $class->_thick_verify_frontend($scfg, $volname, $head);
            my $opens = $class->_thick_frontend_open_count(
                $mapper, $class->_thick_command_deadline($scfg),
            );
            die "refusing to delete open thick-generations volume '$volname'\n"
                if $opens != 0;
            $class->_thick_remove_mapper_exact(
                $scfg,
                ['/sbin/dmsetup', '--verifyudev', 'remove', '--retry', $mapper],
                "removing idle thick-generations frontend '$mapper' before delete failed",
                sub {
                    return $class->_thick_verify_mapper_absent(
                        $scfg, $mapper,
                        "refusing thick-generations delete: frontend '$mapper' removal is unconfirmed",
                    );
                },
            );
        }

        # The frontend can legitimately be absent after a reboot or a completed
        # deactivate_volume() call while one of the private LVs is still active
        # because of interrupted local cleanup.  Never let lvremove's force
        # semantics decide that case.  Deactivate the two exact, signed objects
        # explicitly and device-scoped before recording a destructive intent.
        # Repeating -an for already inactive LVs is deliberately idempotent.
        $class->_thick_deactivate_exact_lvs(
            $scfg, $vg, $device,
            "deactivating thick-generations state for '$vg/$volname' before delete failed",
            $anchor, $head,
        );

        my $tx = $class->_new_transaction_id();
        my %intent = (
            tx => $tx, state => 'OPEN', op => 'REMOVE', object => $anchor,
            before => $class->_vg_state_digest($scfg, $vg, $device),
        );
        $class->_set_vg_intent($scfg, $vg, %intent, _device => $device);

        my $command_error = '';
        eval {
            run_command(
                ['/usr/bin/timeout', '--foreground', '--kill-after=5s', "${command_timeout}s",
                    '/sbin/lvremove', '--devices', $device, '-f', "$vg/$head"],
                errmsg => "removing thick generation '$vg/$head' failed",
            );
            run_command(
                ['/usr/bin/timeout', '--foreground', '--kill-after=5s', "${command_timeout}s",
                    '/sbin/lvremove', '--devices', $device, '-f', "$vg/$anchor"],
                errmsg => "removing thick generation anchor '$vg/$anchor' failed",
            );
        };
        $command_error = $@ if $@;

        my $after = $class->_thick_list_volumes_scoped($scfg, $vg, $device);
        eval { $class->_verify_storage_identity($storeid, $scfg, $device); };
        die "PARTIAL DELETE for '$storeid:$volname': storage identity/availability "
            . "could not be revalidated; OPEN REMOVE intent preserved and no retry attempted. "
            . "After restoring authoritative storage visibility run sharedlvmthin "
            . "thick-recover-volume-delete '$storeid' '$volname': $@"
            if $@;
        # lvm_list_volumes() omits an otherwise healthy VG when it becomes empty.
        # Positive identity revalidation above distinguishes that from disappearance.
        my $after_objects = $after->{$vg} // {};
        my $head_remains = exists($after_objects->{$head});
        my $anchor_remains = exists($after_objects->{$anchor});
        die "PARTIAL DELETE for '$storeid:$volname': head=$head_remains "
            . "anchor=$anchor_remains; OPEN REMOVE intent preserved and no retry attempted. "
            . "Run sharedlvmthin thick-recover-volume-delete '$storeid' '$volname'\n"
            if $head_remains || $anchor_remains;

        warn "thick-generations delete command reported an error, but exact postcondition "
            . "proves both owned objects absent; treating operation as completed without retry: "
            . $command_error
            if $command_error;
        $class->_clear_vg_intent($scfg, $vg, %intent, _device => $device);
        return undef;
    }
}

sub _lazy_free_image {
    my ($class, $storeid, $scfg, $volname, $isBase) = @_;
    return $class->_with_lazy_volume_executor_lock(
        $storeid, $volname,
        sub { $class->_lazy_free_image_locked(
            $storeid, $scfg, $volname, $isBase,
        ) },
    );
}

sub _lazy_free_image_locked {
    my ($class, $storeid, $scfg, $volname, $isBase) = @_;
    $class->_require_thick_identity_config($storeid, $scfg);
    my $vg = $scfg->{'slt-vgname'};
    my $device = "/dev/mapper/$scfg->{'slt-expected-wwid'}";
    my ($zero, $clone, $front) = $class->_lazy_runtime_names($scfg, $volname);
    my $initial_pair = $class->_with_vg_lock($storeid, $scfg, sub {
        $class->_require_no_vg_intent($scfg, $vg, $device);
        my ($state, undef, $anchor) =
            $class->_thick_read_anchor($storeid, $scfg, $volname);
        return [{ %$state }, $anchor];
    }, $device);
    my ($initial, $initial_anchor) = @$initial_pair;
    return $class->_thick_free_image($storeid, $scfg, $volname, $isBase)
        if $initial->{phase} eq 'MATERIALIZED';

    if ($initial->{phase} eq 'LAZY_ACTIVE') {
        # PVE can activate a stopped Lazy disk before a later operation is
        # refused. Reuse the exact normal close lifecycle while the same
        # per-volume executor latch remains held; it proves local ownership,
        # the complete graph and zero frontend opens before any removal.
        $class->_lazy_deactivate_volume_locked(
            $storeid, $scfg, $volname, undef, undef,
        );
    } elsif ($initial->{phase} ne 'LAZY_DORMANT') {
        die "Lazy Thick delete requires local LAZY_ACTIVE or closed LAZY_DORMANT; "
            . "found '$initial->{phase}'\n";
    }

    return $class->_with_vg_lock($storeid, $scfg, sub {
        $class->_require_no_vg_intent($scfg, $vg, $device);
        my ($state, undef, $anchor) =
            $class->_thick_read_anchor($storeid, $scfg, $volname);
        die "Lazy Thick delete anchor identity changed before removal\n"
            if $anchor ne $initial_anchor;
        for my $field (qw(v tx head metadata data_uuid metadata_uuid bytes region)) {
            my $before = defined($initial->{$field}) ? "$initial->{$field}" : '';
            my $after = defined($state->{$field}) ? "$state->{$field}" : '';
            die "Lazy Thick delete object identity changed at '$field' before removal\n"
                if $before ne $after;
        }
        die "Lazy Thick delete requires a closed LAZY_DORMANT volume\n"
            if $state->{phase} ne 'LAZY_DORMANT';
        die "Lazy Thick delete found residual owner authority\n"
            if ($state->{owner_node} // '') ne 'none'
            || ($state->{owner_boot} // '') ne 'none'
            || ($state->{owner_epoch} // '') ne 'none';
        for my $mapper ($front, $clone, $zero) {
            $class->_thick_verify_mapper_absent(
                $scfg, $mapper,
                "refusing Lazy Thick delete while mapper '$mapper' exists",
            );
        }
        for my $lv ($anchor, $state->{metadata}, $state->{head}) {
            $class->_thick_verify_autoactivation_disabled($scfg, $vg, $lv, $device);
        }
        $class->_thick_deactivate_exact_lvs(
            $scfg, $vg, $device,
            "deactivating exact Lazy Thick objects before delete failed",
            $anchor, $state->{metadata}, $state->{head},
        );
        my %intent = (
            tx => $class->_new_transaction_id(), state => 'OPEN', op => 'REMOVE',
            object => $anchor,
            before => $class->_vg_state_digest($scfg, $vg, $device),
        );
        $class->_set_vg_intent($scfg, $vg, %intent, _device => $device);
        my $command_error = '';
        eval {
            for my $lv ($state->{metadata}, $state->{head}, $anchor) {
                run_command(
                    ['/usr/bin/timeout', '--foreground', '--kill-after=5s',
                        $class->_thick_command_deadline($scfg) . 's',
                        '/sbin/lvremove', '--devices', $device, '-f', "$vg/$lv"],
                    errmsg => "removing exact Lazy Thick object '$vg/$lv' failed",
                );
            }
        };
        $command_error = $@ if $@;
        $class->_verify_storage_identity($storeid, $scfg, $device);
        my $after = $class->_thick_list_volumes_scoped($scfg, $vg, $device);
        my $objects = $after->{$vg} // {};
        my @remaining = grep { exists($objects->{$_}) }
            ($state->{metadata}, $state->{head}, $anchor);
        die "PARTIAL LAZY DELETE for '$storeid:$volname': exact object(s) remain ("
            . join(', ', @remaining)
            . "); OPEN REMOVE intent preserved and no retry attempted"
            . ($command_error ? ": $command_error" : "\n")
            if @remaining;
        warn "Lazy Thick delete command reported an error, but exact absence is proven; continuing without retry: $command_error"
            if $command_error;
        $class->_clear_vg_intent($scfg, $vg, %intent, _device => $device);
        return undef;
    }, $device);
}

sub free_image {
    my ($class, $storeid, $scfg, $volname, $isBase) = @_;
    $class->_assert_package_operations_released('volume removal');

    # Auxiliary objects on a Lazy-default storage are deliberately allocated
    # as fully materialized Thick objects.  Their teardown must therefore use
    # the matching Thick path and must not enter the guest-disk-only Lazy
    # executor namespace.
    return $class->_thick_free_image($storeid, $scfg, $volname, $isBase)
        if $class->_is_lazy_mode($scfg)
        && $volname =~ /^vm-\d+-(?:state-[A-Za-z0-9][A-Za-z0-9_.-]*|fleece-\d+|cloudinit)$/;

    return $class->_lazy_free_image($storeid, $scfg, $volname, $isBase)
        if $class->_is_lazy_mode($scfg);

    return $class->_thick_free_image($storeid, $scfg, $volname, $isBase)
        if $class->_is_thick_mode($scfg);

    return $class->_with_mutation_lock($storeid, $scfg, sub {
        return $class->_free_image_locked(
            $storeid, $scfg, $volname, $isBase,
        );
    });
}

sub _thin_recover_orphan {
    my ($class, $scfg, $storeid, $volname) = @_;
    die "thin orphan recovery is unavailable for Thick Generations storage\n"
        if $class->_is_thick_mode($scfg);
    my ($vtype, undef, undef) = $class->parse_volname($volname);
    die "thin orphan recovery requires a canonical guest disk volume\n"
        if $vtype ne 'images' || $volname !~ /^vm-\d+-disk-\d+$/;

    return $class->_with_mutation_lock($storeid, $scfg, sub {
        my $references = $class->_thick_pve_reference_files($storeid, $volname);
        die "thin orphan recovery refused: PVE still references '$storeid:$volname' in "
            . join(', ', @$references) . "\n" if @$references;
        return $class->_free_image_locked($storeid, $scfg, $volname, 0);
    });
}

sub _thin_recover_fenced_owner {
    my ($class, $scfg, $storeid, $volname, $fenced_node) = @_;
    die "fenced Thin owner recovery is unavailable for Thick Generations storage\n"
        if $class->_is_thick_mode($scfg);
    die "fenced Thin owner node is invalid\n"
        if !defined($fenced_node)
        || $fenced_node !~ /^([A-Za-z0-9][A-Za-z0-9_.-]*)$/;
    $fenced_node = $1;
    my $local = $class->_thin_local_node();
    die "refusing fenced-owner recovery for local node '$local'; this command is only for an externally fenced former owner\n"
        if $fenced_node eq $local;
    my (undef, undef, $vmid) = $class->parse_volname($volname);
    my $vg = $scfg->{'slt-vgname'};
    my $pool = "sltp-$vmid";
    my $device = defined($scfg->{'slt-expected-wwid'})
        ? "/dev/mapper/$scfg->{'slt-expected-wwid'}"
        : undef;

    return $class->_with_mutation_lock($storeid, $scfg, sub {
        $class->_verify_owned_volume($storeid, $scfg, $volname);
        my $owner_state = _thin_owner_state_from_tags(
            $class->_thin_pool_tags($vg, $pool, $device),
        );
        die "fenced-owner recovery refused: '$vg/$pool' has no owner schema\n"
            if !$owner_state->{schema};
        my $owner = $owner_state->{owner};
        die "fenced-owner recovery refused: '$vg/$pool' has no persistent owner\n"
            if !defined($owner);
        die "fenced-owner recovery refused: expected owner '$fenced_node', found '$owner'\n"
            if $owner ne $fenced_node;

        # This proves only that the recovery node has no local instance.  The
        # command name and exact fenced-node argument are an explicit operator
        # assertion that external PVE/STONITH fencing has already made the old
        # kernel unable to access the LUN.  The plugin never guesses that fact.
        my $state = $class->_thin_pool_runtime_state($vg, $pool, $device);
        die "fenced-owner recovery refused: local thin-pool runtime state is active\n"
            if $state->{pool_active} || $state->{pool_mapper_active}
            || @{$state->{active_children}};

        my @release = ('/sbin/lvchange');
        push @release, ('--devices', $device) if defined($device);
        push @release, ('--deltag', "pve-slt-owner-node-$fenced_node",
            '--deltag', "pve-slt-owner-epoch-$owner_state->{epoch}", "$vg/$pool");
        run_command(
            \@release,
            errmsg => "clearing explicitly fenced shared thin-pool owner '$fenced_node' failed",
        );
        my $after = _thin_owner_state_from_tags(
            $class->_thin_pool_tags($vg, $pool, $device),
        );
        die "fenced-owner recovery postcondition failed for '$vg/$pool'\n"
            if !$after->{schema} || defined($after->{owner}) || defined($after->{epoch});
        return 'FENCED_THIN_OWNER_CLEARED';
    });
}

sub _thin_adopt_owner_model {
    my ($class, $scfg, $storeid, $volname, $confirmation) = @_;
    die "Thin owner-model adoption is unavailable for Thick Generations storage\n"
        if $class->_is_thick_mode($scfg);
    die "owner-model adoption requires the exact confirmation ALL-NODES-INACTIVE\n"
        if !defined($confirmation) || $confirmation ne 'ALL-NODES-INACTIVE';
    my (undef, undef, $vmid) = $class->parse_volname($volname);
    my $vg = $scfg->{'slt-vgname'};
    my $pool = "sltp-$vmid";
    my $device = defined($scfg->{'slt-expected-wwid'})
        ? "/dev/mapper/$scfg->{'slt-expected-wwid'}"
        : undef;

    return $class->_with_mutation_lock($storeid, $scfg, sub {
        $class->_verify_owned_volume($storeid, $scfg, $volname);
        my $owner = _thin_owner_state_from_tags(
            $class->_thin_pool_tags($vg, $pool, $device),
        );
        die "owner-model adoption refused: legacy/ambiguous owner tags exist\n"
            if defined($owner->{owner}) || defined($owner->{epoch});
        my $state = $class->_thin_pool_runtime_state($vg, $pool, $device);
        die "owner-model adoption refused: local thin-pool runtime state is active\n"
            if $state->{pool_active} || $state->{pool_mapper_active}
            || @{$state->{active_children}};

        # The same explicit offline transaction also hardens already-adopted
        # pools created by older releases.  Disable generic activation on the
        # pool and every exact child before publishing the owner schema.  A
        # partial failure only leaves additional objects disabled and can be
        # retried; it never claims safety prematurely.
        my $members = $class->_thin_pool_members($vg, $pool, $device);
        die "owner-model adoption refused: '$vg/$pool' has no thin members\n"
            if !@$members;
        $class->_disable_and_verify_autoactivation($vg, $pool, $device);
        for my $member (@$members) {
            $class->_disable_and_verify_autoactivation($vg, $member, $device);
        }

        if (!$owner->{schema}) {
            my @adopt = ('/sbin/lvchange');
            push @adopt, ('--devices', $device) if defined($device);
            push @adopt, ('--addtag', THIN_OWNER_SCHEMA_TAG, "$vg/$pool");
            run_command(\@adopt,
                errmsg => "adopting exclusive-owner schema for '$vg/$pool' failed");
        }
        my $after = _thin_owner_state_from_tags(
            $class->_thin_pool_tags($vg, $pool, $device),
        );
        die "owner-model adoption postcondition failed for '$vg/$pool'\n"
            if !$after->{schema} || defined($after->{owner}) || defined($after->{epoch});
        return $owner->{schema}
            ? 'THIN_AUTOACTIVATION_HARDENED'
            : 'THIN_OWNER_MODEL_ADOPTED_AND_HARDENED';
    });
}

sub _free_image_locked {
    my ($class, $storeid, $scfg, $volname, $isBase) = @_;

    $class->_verify_mutation_quorum($storeid, $scfg);
    my $vg = $scfg->{'slt-vgname'};

    $class->_verify_storage_identity($storeid, $scfg);

    my ($vtype, $name, $vmid) = $class->parse_volname($volname);
    my $pool = "sltp-$vmid";

    my $lvs = PVE::Storage::LVMPlugin::lvm_list_volumes($vg);

    my $pool_info = $lvs->{$vg} && $lvs->{$vg}->{$pool};
    my $expected_tag = "pve-slt-sid-$storeid";
    my $pool_owned = 0;
    my $pool_legacy = 0;

    if ($pool_info) {
        die "refusing to modify foreign pool '$vg/$pool': not a thin pool\n"
            if !defined($pool_info->{lv_type}) || $pool_info->{lv_type} ne 't';

        $class->_verify_pool_health($vg, $pool);

        my $tags = $pool_info->{tags} // '';

        if ($tags =~ /(?:^|,)\Q$expected_tag\E(?:,|$)/) {
            $pool_owned = 1;
        } elsif ($tags =~ /(?:^|,)pve-slt-sid-[A-Za-z0-9_-]+(?:,|$)/) {
            die "refusing to modify foreign pool '$vg/$pool': owned by another storage\n";
        } else {
            $pool_legacy = 1;
        }
    }

    die "refusing to modify legacy pool '$vg/$pool': ownership is not positively proven\n"
        if $pool_legacy;

    # Removal is a metadata mutation just like allocation.  A PVE caller on a
    # non-owner node must never be able to delete a disk from a pool which is
    # actively owned by another host, even if a higher-level configuration or
    # storage lock was obtained.  The durable owner tag is the final local
    # safety boundary.  An unowned pool is the valid stopped-VM case; a locally
    # owned pool is the valid hot-remove case.
    if ($pool_owned) {
        my $owner_state = _thin_owner_state_from_tags($pool_info->{tags});
        die "refusing to remove from '$vg/$pool': pool predates the exclusive-owner schema; explicit offline adoption is required\n"
            if !$owner_state->{schema};
        my $local_node = $class->_thin_local_node();
        die "refusing to remove from '$vg/$pool': persistent owner is '$owner_state->{owner}', local node is '$local_node'\n"
            if defined($owner_state->{owner}) && $owner_state->{owner} ne $local_node;
    }

    if ($lvs->{$vg} && $lvs->{$vg}->{$volname}) {
        my $volume_info = $lvs->{$vg}->{$volname};

        die "refusing to remove '$vg/$volname': expected pool '$pool' is missing\n"
            if !$pool_info;

        die "refusing to remove '$vg/$volname': volume is not in expected pool '$pool'\n"
            if !defined($volume_info->{pool_lv})
            || $volume_info->{pool_lv} ne $pool;
    }

    # Remove snapshots belonging to this particular disk first.
    if ($lvs->{$vg}) {
        foreach my $lv (keys %{$lvs->{$vg}}) {
            next if $lv !~ /^snap_\Q$volname\E_/;

            my $snapshot_info = $lvs->{$vg}->{$lv};

            die "refusing to remove '$vg/$lv': expected pool '$pool' is missing\n"
                if !$pool_info;

            die "refusing to remove '$vg/$lv': snapshot is not in expected pool '$pool'\n"
                if !defined($snapshot_info->{pool_lv})
                || $snapshot_info->{pool_lv} ne $pool;

            $class->_verify_snapshot_postcondition($scfg, $volname, $lv, 1);

            run_command(
                ['/sbin/lvremove', '-f', "$vg/$lv"],
                errmsg => "removing snapshot '$vg/$lv' failed",
            );
        }
    }

    # Remove the guest LV itself.
    $lvs = PVE::Storage::LVMPlugin::lvm_list_volumes($vg);

    if ($lvs->{$vg} && $lvs->{$vg}->{$volname}) {
        run_command(
            ['/sbin/lvremove', '-f', "$vg/$volname"],
            errmsg => "removing volume '$vg/$volname' failed",
        );
    }

    #
    # Refresh metadata and remove the per-VM thin pool only when
    # absolutely no VM disk or snapshot belonging to this VM remains.
    #
    $lvs = PVE::Storage::LVMPlugin::lvm_list_volumes($vg);

    die "storage '$storeid' became UNAVAILABLE after deleting '$vg/$volname'; pool cleanup was intentionally NOT attempted\n"
        if !$lvs->{$vg};

    my $in_use = 0;

    if ($lvs->{$vg}) {
        foreach my $lv (keys %{$lvs->{$vg}}) {
            next if $lv eq $pool;
            my $info = $lvs->{$vg}->{$lv};
            if ((defined($info->{pool_lv}) && $info->{pool_lv} eq $pool)
                || $lv =~ /^vm-\Q$vmid\E-disk-\d+$/
                || $lv =~ /^snap_vm-\Q$vmid\E-disk-\d+_/) {
                $in_use = 1;
                last;
            }
        }
    }

    if (!$in_use && $lvs->{$vg} && $lvs->{$vg}->{$pool}) {
        my $pool_info = $lvs->{$vg}->{$pool};

        die "refusing to remove foreign pool '$vg/$pool': not a thin pool\n"
            if !defined($pool_info->{lv_type}) || $pool_info->{lv_type} ne 't';

        if (!$pool_owned) {
            warn "preserving legacy untagged pool '$vg/$pool'; add storage ownership only after administrator review\n"
                if $pool_legacy;
            return undef;
        }

        $class->_verify_pool_health($vg, $pool);

        run_command(
            ['/sbin/lvremove', '-f', "$vg/$pool"],
            errmsg => "removing empty per-VM thin pool '$vg/$pool' failed",
        );
    }

    return undef;
}

sub _thick_recover_resize {
    my ($class, $scfg, $storeid, $volname, $expected_tx, $expected_object) = @_;
    die "resize recovery requires an expected transaction UUID\n"
        if !defined($expected_tx) || $expected_tx !~ /^[0-9a-f]{32}$/;
    die "resize recovery requires an expected anchor identity\n"
        if !defined($expected_object)
        || $expected_object !~ /^sltg-a-[0-9a-f]{24}$/;
    $class->_require_thick_identity_config($storeid, $scfg);
    my $vg = $scfg->{'slt-vgname'};
    my $device = "/dev/mapper/$scfg->{'slt-expected-wwid'}";
    my $namespace = $class->_thick_namespace($scfg);
    my $mapper = mapper_name($namespace, $volname);
    my %resume;

    $class->_with_vg_lock($storeid, $scfg, sub {
        my $intent = $class->_read_vg_intent($scfg, $vg, $device);
        die "volume '$storeid:$volname' has no recoverable OPEN EXTEND intent\n"
            if !$intent || $intent->{state} ne 'OPEN' || $intent->{op} ne 'EXTEND';
        die "resize recovery transaction changed before VG-locked dispatch\n"
            if ($intent->{tx} // '') ne $expected_tx
            || ($intent->{object} // '') ne $expected_object;
        my $lvs = $class->_thick_list_volumes_scoped($scfg, $vg, $device);
        my ($state, $head_info, $anchor) =
            $class->_thick_anchor($storeid, $scfg, $volname, $lvs);
        die "resize recovery intent targets another object\n"
            if $intent->{object} ne $anchor;
        die "resize recovery requires a materialized authoritative HEAD\n"
            if $state->{phase} ne 'MATERIALIZED';
        $class->_require_exact_vg_intent($scfg, $vg, %$intent, _device => $device);
        die "resize recovery requires the exact active frontend\n"
            if !$class->_thick_frontend_present($scfg, $volname);
        my $runtime = $class->_thick_mapper_is_suspended(
            $mapper, $class->_thick_command_deadline($scfg),
        ) ? 'suspended' : 'active';
        $class->_thick_verify_frontend(
            $scfg, $volname, $state->{head}, undef, $runtime,
        );
        my $table = _command_lines(
            ['/usr/bin/timeout', '--foreground', '--kill-after=5s',
                $class->_thick_command_deadline($scfg) . 's',
                '/sbin/dmsetup', 'table', $mapper],
            "reading published frontend size for resize recovery failed",
        );
        die "resize recovery frontend table is not the exact canonical linear map\n"
            if @$table != 1
            || $table->[0] !~ /^0\s+(\d+)\s+linear\s+\S+\s+0$/;
        my $published = int($1) * 512;
        my $target = $head_info->{lv_size};
        die "resize recovery HEAD size is invalid\n"
            if !defined($target) || $target !~ /^\d+$/ || !$target || $target % 512;
        die "resize recovery refuses a backing LV smaller than the published frontend\n"
            if $target < $published;
        $class->_thick_verify_autoactivation_disabled($scfg, $vg, $state->{head}, $device);
        if ($target == $published) {
            $class->_clear_vg_intent($scfg, $vg, %$intent, _device => $device);
            return;
        }
        %resume = (
            intent => $intent, anchor => $anchor, head => $state->{head},
            old_size => $published, new_size => int($target),
        );
        return;
    }, $device);
    return 'RESIZE_RECOVERED' if !%resume;

    # Repeating the complete unpublished tail is idempotent.  The stable
    # frontend still exposes only old_size, so no guest can observe a partial
    # retry.  Exact UUID proof prevents a stale /dev pathname redirect.
    $class->_thick_verify_active_lv_identity(
        $scfg, $vg, $resume{head}, $device, $class->_thick_command_deadline($scfg),
    );
    my $length = $resume{new_size} - $resume{old_size};
    run_command(
        ['/usr/bin/dd', 'if=/dev/zero', "of=/dev/$vg/$resume{head}", 'bs=4M',
            "seek=$resume{old_size}", "count=$length", 'iflag=count_bytes',
            'oflag=seek_bytes,direct', 'conv=fsync,nocreat', 'status=none'],
        errmsg => "zero-initializing unpublished resize tail failed",
    );
    run_command(
        ['/sbin/blockdev', '--flushbufs', "/dev/$vg/$resume{head}"],
        errmsg => "flushing recovered resize tail failed",
    );

    $class->_with_vg_lock($storeid, $scfg, sub {
        my %intent = %{$resume{intent}};
        $class->_require_exact_vg_intent($scfg, $vg, %intent, _device => $device);
        my $lvs = $class->_thick_list_volumes_scoped($scfg, $vg, $device);
        my ($state, $head_info, $anchor) =
            $class->_thick_anchor($storeid, $scfg, $volname, $lvs);
        die "resize recovery authority changed while zeroing\n"
            if $anchor ne $resume{anchor} || $state->{phase} ne 'MATERIALIZED'
            || $state->{head} ne $resume{head}
            || ($head_info->{lv_size} // -1) != $resume{new_size};
        my $runtime = $class->_thick_mapper_is_suspended(
            $mapper, $class->_thick_command_deadline($scfg),
        ) ? 'suspended' : 'active';
        $class->_thick_verify_frontend(
            $scfg, $volname, $resume{head}, int($resume{old_size} / 512), $runtime,
        );
        my $new_sectors = int($resume{new_size} / 512);
        my $head_devno = $class->_thick_verify_active_lv_identity(
            $scfg, $vg, $resume{head}, $device, $class->_thick_command_deadline($scfg),
        );
        $class->_thick_load_inactive_table_exact(
            $scfg, 'reload', $mapper,
            "0 $new_sectors linear $head_devno 0",
            sub {
                my ($inactive) = @_;
                die "resize recovery inactive frontend table postcondition failed\n"
                    if @$inactive != 1
                    || $inactive->[0] ne "0 $new_sectors linear $head_devno 0";
                return 1;
            },
            "loading recovered thick-generations frontend failed",
        );
        my $already_suspended = $runtime eq 'suspended';
        if (!$already_suspended) {
            $class->_thick_suspend_mapper_exact(
                $scfg, $mapper,
                "suspending recovered thick-generations frontend failed",
            );
        }
        $class->_thick_resume_mapper_exact(
            $scfg, $mapper,
            sub {
                return $class->_thick_verify_frontend(
                    $scfg, $volname, $resume{head}, $new_sectors,
                );
            },
            "publishing recovered thick-generations frontend failed",
        );
        $class->_clear_vg_intent($scfg, $vg, %intent, _device => $device);
        return;
    }, $device);
    return 'RESIZE_RECOVERED';
}

sub _thick_volume_resize {
    my ($class, $scfg, $storeid, $volname, $size, $running, $snapname) = @_;
    die "resizing thick-generations snapshots is not supported\n" if defined($snapname);
    die "invalid resize size\n" if !defined($size) || $size !~ /^(\d+)$/ || $size < 1;
    $size = 0 + $1;
    $class->_require_thick_identity_config($storeid, $scfg);

    my $vg = $scfg->{'slt-vgname'};
    my $device = "/dev/mapper/$scfg->{'slt-expected-wwid'}";
    my $command_timeout = $class->_thick_command_deadline($scfg);
    my $namespace = $class->_thick_namespace($scfg);
    my $mapper = mapper_name($namespace, $volname);
    my %resize;

    $class->_with_vg_lock($storeid, $scfg, sub {
        $class->_require_no_vg_intent($scfg, $vg, $device);
        my $lvs = $class->_thick_list_volumes_scoped($scfg, $vg, $device);
        my ($state, $head_info, $anchor) =
            $class->_thick_anchor($storeid, $scfg, $volname, $lvs);
        my $head = $state->{head};
        my $old_size = $head_info->{lv_size};
        die "thick-generations head '$vg/$head' size is unknown\n"
            if !defined($old_size) || $old_size !~ /^\d+$/ || $old_size < 1;
        die "shrinking thick-generations volumes is not supported\n" if $size < $old_size;
        return if $size == $old_size;
        die "thick-generations size is not sector aligned\n"
            if $old_size % 512 || $size % 512;

        my $delta = $size - $old_size;
        $class->_thick_capacity_gate(
            $storeid, $scfg, int(($delta + 1023) / 1024), 0,
        );
        my $frontend = $class->_thick_frontend_present($scfg, $volname) ? 1 : 0;
        $class->_thick_verify_frontend(
            $scfg, $volname, $head, int($old_size / 512),
        ) if $frontend;

        my $tx = $class->_new_transaction_id();
        my %intent = (
            tx => $tx, state => 'OPEN', op => 'EXTEND', object => $anchor,
            before => $class->_vg_state_digest($scfg, $vg, $device),
        );
        $class->_set_vg_intent($scfg, $vg, %intent, _device => $device);

        my $extend_error = '';
        eval {
            run_command(
                ['/usr/bin/timeout', '--foreground', '--kill-after=5s',
                    "${command_timeout}s", '/sbin/lvextend', '--devices', $device,
                    '-L', "${size}B", "$vg/$head"],
                errmsg => "extending thick generation '$vg/$head' failed",
            );
        };
        $extend_error = $@ if $@;
        eval { $class->_verify_storage_identity($storeid, $scfg, $device); };
        die "PARTIAL RESIZE for '$storeid:$volname': storage identity/availability "
            . "could not be revalidated; OPEN EXTEND intent preserved: $@"
            if $@;
        my $after = $class->_thick_list_volumes_scoped($scfg, $vg, $device);
        my ($after_state, $after_head) =
            $class->_thick_anchor($storeid, $scfg, $volname, $after);
        die "PARTIAL RESIZE for '$storeid:$volname': authoritative head changed; "
            . "OPEN EXTEND intent preserved\n"
            if $after_state->{head} ne $head;
        my $new_size = $after_head->{lv_size};
        die "PARTIAL RESIZE for '$storeid:$volname': resulting size is unknown or "
            . "smaller than requested; OPEN EXTEND intent preserved; no retry attempted"
            . ($extend_error ? ": $extend_error" : "\n")
            if !defined($new_size) || $new_size !~ /^\d+$/ || $new_size < $size;
        die "PARTIAL RESIZE for '$storeid:$volname': resulting size is not sector aligned; "
            . "OPEN EXTEND intent preserved\n"
            if $new_size % 512;
        warn "thick-generations lvextend reported an error, but its exact postcondition "
            . "proves the requested size was reached; continuing without retry: $extend_error"
            if $extend_error;
        %resize = (
            intent => \%intent, anchor => $anchor, head => $head,
            old_size => int($old_size), new_size => int($new_size),
            frontend => $frontend,
        );
        return;
    }, $device);
    return if !%resize;

    my $zero_error = '';
    eval {
        $class->_thick_activate_exact_lvs(
            $scfg, $vg, $device,
            "activating extended thick generation '$vg/$resize{head}' failed",
            $resize{head},
        ) if !$resize{frontend};
        $class->_thick_verify_active_lv_identity(
            $scfg, $vg, $resize{head}, $device, $class->_thick_command_deadline($scfg),
        )
            if $resize{frontend};
        my $length = $resize{new_size} - $resize{old_size};
        run_command(
            ['/usr/bin/dd', 'if=/dev/zero', "of=/dev/$vg/$resize{head}", 'bs=4M',
                "seek=$resize{old_size}", "count=$length", 'iflag=count_bytes',
                'oflag=seek_bytes,direct', 'conv=fsync,nocreat', 'status=none'],
            errmsg => "zero-initializing extended range of '$vg/$resize{head}' failed",
        );
        run_command(
            ['/sbin/blockdev', '--flushbufs', "/dev/$vg/$resize{head}"],
            errmsg => "flushing extended thick generation '$vg/$resize{head}' failed",
        );
        $class->_thick_deactivate_exact_lvs(
            $scfg, $vg, $device,
            "deactivating extended thick generation '$vg/$resize{head}' failed",
            $resize{head},
        ) if !$resize{frontend};
    };
    $zero_error = $@ if $@;
    die "PARTIAL RESIZE for '$storeid:$volname': the backing LV may be extended, but "
        . "the new range was not safely published; OPEN EXTEND intent preserved: $zero_error"
        if $zero_error;

    $class->_with_vg_lock($storeid, $scfg, sub {
        my %intent = %{$resize{intent}};
        $class->_require_exact_vg_intent($scfg, $vg, %intent, _device => $device);
        my $lvs = $class->_thick_list_volumes_scoped($scfg, $vg, $device);
        my ($state, $head_info) = $class->_thick_anchor(
            $storeid, $scfg, $volname, $lvs,
        );
        die "PARTIAL RESIZE for '$storeid:$volname': head or size changed before "
            . "publication; OPEN EXTEND intent preserved\n"
            if $state->{head} ne $resize{head}
            || !defined($head_info->{lv_size})
            || $head_info->{lv_size} != $resize{new_size};
        $class->_thick_verify_autoactivation_disabled($scfg, $vg, $resize{head}, $device);

        my $frontend_now = $class->_thick_frontend_present($scfg, $volname) ? 1 : 0;
        die "PARTIAL RESIZE for '$storeid:$volname': frontend presence changed; "
            . "OPEN EXTEND intent preserved\n"
            if $frontend_now != $resize{frontend};
        if ($frontend_now) {
            my $old_sectors = int($resize{old_size} / 512);
            my $new_sectors = int($resize{new_size} / 512);
            $class->_thick_verify_frontend(
                $scfg, $volname, $resize{head}, $old_sectors,
            );
            die "refusing Thick resize publication of an already suspended frontend\n"
                if $class->_thick_mapper_is_suspended(
                    $mapper, $class->_thick_command_deadline($scfg),
                );
            my $head_devno = $class->_thick_verify_active_lv_identity(
                $scfg, $vg, $resize{head}, $device,
                $class->_thick_command_deadline($scfg),
            );
            $class->_thick_load_inactive_table_exact(
                $scfg, 'reload', $mapper,
                "0 $new_sectors linear $head_devno 0",
                sub {
                    my ($inactive) = @_;
                    die "PARTIAL RESIZE for '$storeid:$volname': inactive frontend table "
                        . "postcondition failed; OPEN EXTEND intent preserved\n"
                        if @$inactive != 1
                        || $inactive->[0] ne "0 $new_sectors linear $head_devno 0";
                    return 1;
                },
                "loading extended thick-generations frontend '$mapper' failed",
            );
            $class->_thick_suspend_mapper_exact(
                $scfg, $mapper,
                "suspending thick-generations frontend '$mapper' for resize failed",
            );
            $class->_thick_fault_point('R0', 'RESIZE', $storeid, $volname);
            $class->_thick_resume_mapper_exact(
                $scfg, $mapper,
                sub {
                    return $class->_thick_verify_frontend(
                        $scfg, $volname, $resize{head}, $new_sectors,
                    );
                },
                "publishing extended thick-generations frontend '$mapper' failed",
            );
        }
        $class->_clear_vg_intent($scfg, $vg, %intent, _device => $device);
        return;
    }, $device);
    return;
}

sub volume_resize {
    my ($class, $scfg, $storeid, $volname, $size, $running, $snapname) = @_;
    $class->_assert_package_operations_released('volume resize');

    if ($class->_is_lazy_mode($scfg)) {
        return $class->_lazy_integration_pending('resize')
            if !$class->_lazy_materialized($storeid, $scfg, $volname);
        return $class->_thick_volume_resize(
            $scfg, $storeid, $volname, $size, $running, $snapname,
        );
    }

    return $class->_thick_volume_resize(
        $scfg, $storeid, $volname, $size, $running, $snapname,
    ) if $class->_allocation_mode($scfg) eq 'thick-generations';

    return $class->_with_mutation_lock($storeid, $scfg, sub {
        return $class->_volume_resize_locked(
            $scfg, $storeid, $volname, $size, $running, $snapname,
        );
    });
}

sub _volume_resize_locked {
    my ($class, $scfg, $storeid, $volname, $size, $running, $snapname) = @_;

    die "resizing snapshots is not supported\n" if $snapname;

    $class->_verify_mutation_quorum($storeid, $scfg);
    $class->_verify_storage_identity($storeid, $scfg);
    $class->_verify_owned_volume($storeid, $scfg, $volname);
    my $vg = $scfg->{'slt-vgname'};
    $class->_verify_autoactivation_disabled($vg, $volname);

    die "invalid resize size\n"
        if !defined($size) || $size <= 0;

    #
    # PVE passes the requested size in bytes here.
    # LVM accepts bytes explicitly with the B suffix.
    #
    my $resize_error = '';
    eval {
        run_command([
            '/sbin/lvextend',
            '-L', "${size}B",
            "$vg/$volname",
        ], errmsg => "resizing shared thin LV '$vg/$volname' failed");
    };
    $resize_error = $@ if $@;

    eval { $class->_verify_resize_postcondition($scfg, $volname, $size); };
    my $resize_post_error = $@;
    die(($resize_error || '')
        . "Thin resize outcome is UNKNOWN; exact requested-size postcondition was not proven; "
        . "no retry or shrink was attempted: $resize_post_error")
        if $resize_post_error;
    warn "Thin lvextend reported an error, but exact postcondition proves the requested "
        . "size was reached; continuing without retry: $resize_error"
        if $resize_error;
    $class->_verify_autoactivation_disabled($vg, $volname);

    return;
}

sub volume_snapshot {
    my ($class, $scfg, $storeid, $volname, $snap) = @_;
    $class->_assert_package_operations_released('snapshot creation');

    if ($class->_is_lazy_mode($scfg)) {
        return $class->_lazy_integration_pending('snapshot')
            if !$class->_lazy_materialized($storeid, $scfg, $volname);
        return $class->_thick_volume_snapshot($scfg, $storeid, $volname, $snap);
    }

    return $class->_thick_volume_snapshot($scfg, $storeid, $volname, $snap)
        if $class->_allocation_mode($scfg) eq 'thick-generations';

    return $class->_with_mutation_lock($storeid, $scfg, sub {
        return $class->_volume_snapshot_locked(
            $scfg, $storeid, $volname, $snap,
        );
    });
}

sub _volume_snapshot_locked {
    my ($class, $scfg, $storeid, $volname, $snap) = @_;

    $class->_verify_mutation_quorum($storeid, $scfg);
    my $vg = $scfg->{'slt-vgname'};
    $class->_verify_storage_identity($storeid, $scfg);
    $class->_verify_owned_volume($storeid, $scfg, $volname);
    my $snapvol = "snap_${volname}_${snap}";

    # Establish absence before the single create attempt. This makes a fresh
    # exact postcondition sufficient to classify an ambiguous command result
    # without adopting a pre-existing same-name object.
    $class->_verify_snapshot_postcondition($scfg, $volname, $snapvol, 0);
    my $create_error = '';
    eval {
        run_command(
            ['/sbin/lvcreate', '-n', $snapvol, '-pr', '-s', "$vg/$volname"],
            errmsg => "creating snapshot '$vg/$snapvol' failed",
        );
    };
    $create_error = $@ if $@;

    eval { $class->_verify_snapshot_postcondition($scfg, $volname, $snapvol, 1); };
    my $create_post_error = $@;
    die(($create_error || '')
        . "Thin snapshot create outcome is UNKNOWN; exact new snapshot postcondition was not proven; "
        . "no retry or cleanup was attempted: $create_post_error")
        if $create_post_error;
    warn "Thin snapshot create reported an error, but exact new-object postcondition "
        . "is proven; continuing without retry: $create_error" if $create_error;

    $class->_disable_and_verify_autoactivation($vg, $snapvol);
    $class->_verify_snapshot_postcondition($scfg, $volname, $snapvol, 1);

    return;
}

sub volume_snapshot_delete {
    my ($class, $scfg, $storeid, $volname, $snap) = @_;
    $class->_assert_package_operations_released('snapshot removal');

    if ($class->_is_lazy_mode($scfg)) {
        my ($state) = $class->_thick_read_anchor($storeid, $scfg, $volname);
        return $class->_lazy_integration_pending('snapshot delete')
            if int($state->{v} // 0) == 6
            && ($state->{phase} // '') ne 'MATERIALIZED';
        # A v5 snapshot transition may exist under a Lazy-default alias after
        # that disk converged to ordinary Thick.  Let the exact waiter below
        # observe that one transaction instead of misclassifying it as v6.
    }

    return $class->_thick_volume_snapshot_delete($scfg, $storeid, $volname, $snap)
        if $class->_is_thick_mode($scfg);

    return $class->_with_mutation_lock($storeid, $scfg, sub {
        return $class->_volume_snapshot_delete_locked(
            $scfg, $storeid, $volname, $snap,
        );
    });
}

sub _thick_wait_snapshot_delete_admission {
    my ($class, $scfg, $storeid, $volname, $snap) = @_;
    $snap = _thick_snapshot_name($snap);
    $class->_require_thick_identity_config($storeid, $scfg);
    my $vg = $scfg->{'slt-vgname'};
    my $device = "/dev/mapper/$scfg->{'slt-expected-wwid'}";
    my $budget = $scfg->{'slt-mutation-admission-timeout'} // 600;
    die "invalid snapshot-delete admission timeout\n"
        if $budget !~ /^\d+$/ || $budget < 10 || $budget > 86400;
    my $deadline = $class->_thick_progress_clock() + $budget;
    my $delay_ms = 250;
    my $pinned;

    while (1) {
        $class->_verify_mutation_quorum($storeid, $scfg);
        $class->_verify_storage_identity($storeid, $scfg, $device);
        my $lvs = $class->_thick_list_volumes_scoped($scfg, $vg, $device);
        my ($state, undef, $anchor) =
            $class->_thick_read_anchor($storeid, $scfg, $volname, $lvs);
        my $anchor_info = $lvs->{$vg}->{$anchor}
            // die "snapshot-delete admission anchor disappeared\n";
        my $anchor_uuid = $anchor_info->{lv_uuid} // '';
        die "snapshot-delete admission anchor UUID is unavailable\n"
            if $anchor_uuid eq '';

        if (($state->{phase} // '') eq 'MATERIALIZED') {
            my ($snapshot, $generation, $snapshot_info) = $class->_thick_find_snapshot(
                $storeid, $scfg, $volname, $snap, $lvs,
            );
            my $snapshot_uuid = $snapshot_info->{lv_uuid} // '';
            die "snapshot-delete admission snapshot UUID is unavailable\n"
                if $snapshot_uuid eq '';
            my $receipt = {
                anchor => $anchor, anchor_uuid => $anchor_uuid,
                tx => $state->{tx}, head => $state->{head},
                generation => int($state->{generation}),
                snapshot => $snapshot, snapshot_generation => int($generation),
                snapshot_uuid => $snapshot_uuid,
            };
            if ($pinned) {
                die "snapshot-delete admission transaction changed while waiting\n"
                    if $receipt->{anchor} ne $pinned->{anchor}
                    || $receipt->{anchor_uuid} ne $pinned->{anchor_uuid}
                    || $receipt->{tx} ne $pinned->{tx}
                    || $receipt->{head} ne $pinned->{new}
                    || $receipt->{generation} != $pinned->{generation};
            }
            return $receipt;
        }

        return $class->_lazy_integration_pending('snapshot delete')
            if int($state->{v} // 0) == 6;
        die "snapshot delete requires a materialized Thick HEAD; state is not an exact snapshot transition\n"
            if ($state->{phase} // '') !~ /^(?:COMMITTED|HYDRATING|HYDRATION_COMPLETE|LINEAR_PIVOTED)$/
            || ($state->{op} // '') ne 'SNAPSHOT'
            || ($state->{snapshot} // '') ne $snap
            || ($state->{tx} // '') !~ /^[0-9a-f]{32}$/
            || ($state->{new} // '') eq ''
            || ($state->{head} // '') ne $state->{new};
        my $current = {
            anchor => $anchor, anchor_uuid => $anchor_uuid,
            tx => $state->{tx}, old => $state->{old}, new => $state->{new},
            generation => int($state->{generation}), snapshot => $state->{snapshot},
        };
        if ($pinned) {
            for my $field (qw(anchor anchor_uuid tx old new generation snapshot)) {
                die "snapshot-delete admission identity changed while waiting\n"
                    if $current->{$field} ne $pinned->{$field};
            }
        } else {
            $pinned = $current;
            warn "waiting for exact Thick snapshot transition '$pinned->{tx}' before deleting '$storeid:$volname\@$snap'; no delete effect started\n";
        }

        die "snapshot-delete admission timed out while the exact snapshot transition remained incomplete; no delete effect started\n"
            if $class->_thick_progress_clock() >= $deadline;
        my $remaining_ms = int(($deadline - $class->_thick_progress_clock()) * 1000);
        my $wait_ms = $delay_ms < $remaining_ms ? $delay_ms : $remaining_ms;
        die "snapshot-delete admission timed out; no delete effect started\n"
            if $wait_ms < 1;
        # Observation only.  Do not hold the VG or transition-executor lock:
        # the already-running materializer needs both in executor->VG order.
        $class->_thick_observation_pause($wait_ms);
        $delay_ms *= 2 if $delay_ms < 1000;
    }
}

sub _thick_volume_snapshot_delete {
    my ($class, $scfg, $storeid, $volname, $snap, $require_orphan) = @_;
    $class->_require_thick_identity_config($storeid, $scfg);
    my $device = "/dev/mapper/$scfg->{'slt-expected-wwid'}";
    my $admission = $class->_thick_wait_snapshot_delete_admission(
        $scfg, $storeid, $volname, $snap,
    );
    return $class->_with_vg_lock($storeid, $scfg, sub {
        return $class->_thick_volume_snapshot_delete_locked(
            $scfg, $storeid, $volname, $snap, $require_orphan, undef, $admission,
        );
    }, $device);
}

sub _thick_volume_snapshot_delete_locked {
    my ($class, $scfg, $storeid, $volname, $snap, $require_orphan,
        $tree_admission, $snapshot_admission) = @_;
    $snap = _thick_snapshot_name($snap);
    $class->_require_thick_identity_config($storeid, $scfg);
    my $vg = $scfg->{'slt-vgname'};
    my $device = "/dev/mapper/$scfg->{'slt-expected-wwid'}";
    my $command_timeout = $class->_thick_command_deadline($scfg);

    {
        $class->_require_no_vg_intent($scfg, $vg, $device);
        my $lvs = $class->_thick_list_volumes_scoped($scfg, $vg, $device);
        my ($state, undef, $anchor) =
            $class->_thick_anchor($storeid, $scfg, $volname, $lvs);
        my $anchor_info = $lvs->{$vg}->{$anchor}
            // die "snapshot-delete anchor disappeared after admission\n";
        if ($require_orphan) {
            $class->_thick_assert_tree_references($storeid, $volname, $tree_admission);
        }
        my ($snapshot, $generation, $snapshot_info) = $class->_thick_find_snapshot(
            $storeid, $scfg, $volname, $snap, $lvs,
        );
        $snapshot_admission //= {
            anchor => $anchor, anchor_uuid => ($anchor_info->{lv_uuid} // ''),
            tx => $state->{tx}, head => $state->{head},
            generation => int($state->{generation}),
            snapshot => $snapshot, snapshot_generation => int($generation),
            snapshot_uuid => ($snapshot_info->{lv_uuid} // ''),
        } if $require_orphan;
        die "snapshot-delete admission changed before the VG-locked delete\n"
            if !$snapshot_admission
            || $anchor ne $snapshot_admission->{anchor}
            || ($anchor_info->{lv_uuid} // '') ne $snapshot_admission->{anchor_uuid}
            || $state->{tx} ne $snapshot_admission->{tx}
            || $state->{head} ne $snapshot_admission->{head}
            || int($state->{generation}) != $snapshot_admission->{generation};
        die "snapshot-delete snapshot identity changed after admission\n"
            if $snapshot ne $snapshot_admission->{snapshot}
            || int($generation) != $snapshot_admission->{snapshot_generation}
            || ($snapshot_info->{lv_uuid} // '') ne $snapshot_admission->{snapshot_uuid};
        die "refusing to delete authoritative HEAD as a snapshot\n"
            if $snapshot eq $state->{head};
        $class->_thick_verify_snapshot_readonly($scfg, $vg, $snapshot, $device);
        $class->_thick_verify_autoactivation_disabled($scfg, $vg, $snapshot, $device);

        my $path = "/dev/$vg/$snapshot";
        if (_block_device_exists($path)) {
            my $open = $class->_thick_frontend_open_count(
                $path, $class->_thick_command_deadline($scfg),
            );
            die "refusing to delete open snapshot '$vg/$snapshot'\n" if $open != 0;
        }

        my %intent = (
            tx => $class->_new_transaction_id(), state => 'OPEN',
            op => 'REMOVE_SNAPSHOT', object => $snapshot,
            before => $class->_vg_state_digest($scfg, $vg, $device),
        );
        my $rebased = materialized_rebase_state($state, $intent{tx});
        $class->_thick_fault_point('D0', 'REMOVE_SNAPSHOT', $storeid, $volname);
        $class->_set_vg_intent($scfg, $vg, %intent, _device => $device);
        $class->_thick_fault_point('D1', 'REMOVE_SNAPSHOT', $storeid, $volname);
        eval {
            $class->_change_exact_tags($scfg,
                $vg, $anchor,
                PVE::SharedLvmThinThick::anchor_tags(%$state),
                PVE::SharedLvmThinThick::anchor_tags(%$rebased),
                "rebasing materialized anchor '$vg/$anchor' before snapshot delete failed",
                $device,
            );
            $class->_thick_fault_point('D2', 'REMOVE_SNAPSHOT', $storeid, $volname);
            # Do not infer kernel inactivity from a missing /dev symlink.  udev
            # may lag or be damaged while the exact mapper still exists.
            $class->_thick_deactivate_exact_lvs(
                $scfg, $vg, $device,
                "deactivating snapshot '$vg/$snapshot' before delete failed",
                $snapshot,
            );
            run_command(
                ['/usr/bin/timeout', '--foreground', '--kill-after=5s', "${command_timeout}s",
                    '/sbin/lvremove', '--devices', $device, '-f', "$vg/$snapshot"],
                errmsg => "removing snapshot '$vg/$snapshot' failed",
            );
            $class->_thick_fault_point('D3', 'REMOVE_SNAPSHOT', $storeid, $volname);
        };
        my $error = $@;

        eval { $class->_verify_storage_identity($storeid, $scfg, $device); };
        die "PARTIAL SNAPSHOT DELETE for '$storeid:$volname\@$snap': identity is uncertain; "
            . "OPEN intent preserved and no retry attempted: $@" if $@;
        my $after = $class->_thick_list_volumes_scoped($scfg, $vg, $device);
        my $objects = $after->{$vg} // {};
        die "PARTIAL SNAPSHOT DELETE for '$storeid:$volname\@$snap': exact object remains; "
            . "OPEN intent preserved and no retry attempted"
            . ($error ? ": $error" : "\n") if exists($objects->{$snapshot});
        die "PARTIAL SNAPSHOT DELETE for '$storeid:$volname\@$snap': command failed after "
            . "the exact object disappeared; OPEN intent preserved for manual classification: $error"
            if $error;
        my ($after_state) = $class->_thick_anchor(
            $storeid, $scfg, $volname, $after,
        );
        die "snapshot delete changed authoritative HEAD or generation\n"
            if $after_state->{head} ne $state->{head}
            || int($after_state->{generation}) != int($state->{generation});
        die "snapshot delete did not leave a canonical materialized anchor\n"
            if $after_state->{tx} ne $intent{tx}
            || $after_state->{op} ne 'ALLOC'
            || $after_state->{snapshot} ne 'none'
            || $after_state->{source} ne $after_state->{head}
            || $after_state->{old} ne $after_state->{head}
            || $after_state->{new} ne $after_state->{head};
        $class->_clear_vg_intent($scfg, $vg, %intent, _device => $device);
        $class->_thick_fault_point('D4', 'REMOVE_SNAPSHOT', $storeid, $volname);
        return;
    }
}

sub _thick_recover_orphan_tree {
    my ($class, $scfg, $storeid, $volname) = @_;
    $class->_require_thick_identity_config($storeid, $scfg);
    my $vg = $scfg->{'slt-vgname'};
    my $device = "/dev/mapper/$scfg->{'slt-expected-wwid'}";
    my @snapshots;

    # Inventory under the canonical VG lock, but run each existing exact
    # snapshot-delete transaction separately. This keeps every destructive
    # step independently recoverable after host loss.
    $class->_with_vg_lock($storeid, $scfg, sub {
        $class->_require_no_vg_intent($scfg, $vg, $device);
        my $lvs = $class->_thick_list_volumes_scoped($scfg, $vg, $device);
        my ($state) = $class->_thick_anchor($storeid, $scfg, $volname, $lvs);
        my $references = $class->_thick_pve_reference_files($storeid, $volname);
        die "orphan-tree recovery refused: PVE still references '$storeid:$volname' in "
            . join(', ', @$references) . "\n" if @$references;

        my $namespace = $class->_thick_namespace($scfg);
        my $key = object_key($namespace, $volname);
        my $objects = $lvs->{$vg} // {};
        die "orphan-tree recovery found transition metadata\n"
            if grep { /^sltg-m-\Q$key\E-/ } keys %$objects;

        for my $name (sort grep { /^sltg-g-\Q$key\E-\d{8}$/ } keys %$objects) {
            next if $name eq $state->{head};
            my $owned = decode_generation_tags($objects->{$name}->{tags} // '');
            die "orphan-tree recovery found a generation with ambiguous ownership\n"
                if $owned->{sid} ne $storeid || $owned->{vol} ne $volname
                || $owned->{role} ne 'snapshot'
                || generation_name($namespace, $volname, $owned->{generation}) ne $name;
            _thick_snapshot_name($owned->{snapshot});
            push @snapshots, $owned->{snapshot};
        }
        return;
    }, $device);

    for my $snapshot (@snapshots) {
        $class->_thick_volume_snapshot_delete(
            $scfg, $storeid, $volname, $snapshot, 1,
        );
    }
    return $class->_thick_free_image($storeid, $scfg, $volname, 0, 0, 1);
}

sub _thick_recover_snapshot_delete {
    my ($class, $scfg, $storeid, $volname) = @_;
    $class->_require_thick_identity_config($storeid, $scfg);
    my $vg = $scfg->{'slt-vgname'};
    my $device = "/dev/mapper/$scfg->{'slt-expected-wwid'}";
    my $command_timeout = $class->_thick_command_deadline($scfg);

    return $class->_with_vg_lock($storeid, $scfg, sub {
        my $intent = $class->_read_vg_intent($scfg, $vg, $device);
        die "VG '$vg' has no snapshot-delete transaction to recover\n"
            if !defined($intent);
        die "VG '$vg' intent is not an OPEN snapshot-delete transaction\n"
            if ($intent->{state} // '') ne 'OPEN'
            || ($intent->{op} // '') ne 'REMOVE_SNAPSHOT';

        my %expected = %$intent;
        $class->_require_exact_vg_intent($scfg,
            $vg, %expected, _device => $device,
        );

        my $lvs = $class->_thick_list_volumes_scoped($scfg, $vg, $device);
        die "snapshot-delete recovery cannot see VG '$vg'\n" if !$lvs->{$vg};
        my ($state, undef, $anchor) =
            $class->_thick_anchor($storeid, $scfg, $volname, $lvs);
        my $snapshot = $intent->{object};
        die "snapshot-delete intent targets the authoritative HEAD\n"
            if $snapshot eq $state->{head};

        my $verify_snapshot = sub {
            my ($objects) = @_;
            my $info = $objects->{$snapshot};
            return undef if !defined($info);
            my $owned = decode_generation_tags($info->{tags} // '');
            die "snapshot-delete intent object is not an owned snapshot generation\n"
                if $owned->{sid} ne $storeid
                || $owned->{vol} ne $volname
                || $owned->{role} ne 'snapshot';
            my $namespace = $class->_thick_namespace($scfg);
            die "snapshot-delete intent object name does not match its signed generation\n"
                if generation_name($namespace, $volname, $owned->{generation}) ne $snapshot;
            return $owned;
        };

        my $owned = $verify_snapshot->($lvs->{$vg});
        my $verify_snapshot_inactive = sub {
            $class->_thick_verify_snapshot_readonly($scfg, $vg, $snapshot, $device);
            $class->_thick_verify_autoactivation_disabled($scfg, $vg, $snapshot, $device);
            my $path = "/dev/$vg/$snapshot";
            return if !_block_device_exists($path);
            my $open = $class->_thick_frontend_open_count(
                $path, $class->_thick_command_deadline($scfg),
            );
            die "refusing to recover deletion of open snapshot '$vg/$snapshot'\n"
                if $open != 0;
            return 1;
        };
        $verify_snapshot_inactive->() if defined($owned);

        if ($state->{tx} ne $intent->{tx}) {
            die "snapshot-delete recovery cannot rebase after the exact object disappeared\n"
                if !defined($owned);
            my $rebased = materialized_rebase_state($state, $intent->{tx});
            $class->_change_exact_tags($scfg,
                $vg, $anchor,
                PVE::SharedLvmThinThick::anchor_tags(%$state),
                PVE::SharedLvmThinThick::anchor_tags(%$rebased),
                "recovering materialized anchor '$vg/$anchor' before snapshot delete failed",
                $device,
            );
            $state = $rebased;
        } else {
            die "snapshot-delete recovery found a non-canonical rebased anchor\n"
                if $state->{op} ne 'ALLOC'
                || $state->{snapshot} ne 'none'
                || $state->{source} ne $state->{head}
                || $state->{old} ne $state->{head}
                || $state->{new} ne $state->{head};
        }

        if (defined($owned)) {
            $verify_snapshot_inactive->();
            $class->_thick_deactivate_exact_lvs(
                $scfg, $vg, $device,
                "deactivating snapshot '$vg/$snapshot' during recovery failed",
                $snapshot,
            );
            run_command(
                ['/usr/bin/timeout', '--foreground', '--kill-after=5s', "${command_timeout}s",
                    '/sbin/lvremove', '--devices', $device, '-f', "$vg/$snapshot"],
                errmsg => "removing snapshot '$vg/$snapshot' during recovery failed",
            );
        }

        $class->_verify_storage_identity($storeid, $scfg, $device);
        my $after = $class->_thick_list_volumes_scoped($scfg, $vg, $device);
        die "snapshot-delete recovery cannot confirm VG '$vg'\n" if !$after->{$vg};
        die "snapshot-delete recovery did not remove the exact snapshot object\n"
            if exists($after->{$vg}->{$snapshot});
        my ($final) = $class->_thick_anchor($storeid, $scfg, $volname, $after);
        die "snapshot-delete recovery did not preserve the canonical HEAD\n"
            if $final->{tx} ne $intent->{tx}
            || $final->{head} ne $state->{head}
            || int($final->{generation}) != int($state->{generation})
            || $final->{op} ne 'ALLOC'
            || $final->{snapshot} ne 'none'
            || $final->{source} ne $final->{head}
            || $final->{old} ne $final->{head}
            || $final->{new} ne $final->{head};
        $class->_clear_vg_intent($scfg,
            $vg, %expected, _device => $device,
        );
        return 'SNAPSHOT_DELETE_RECOVERED';
    }, $device);
}

sub _volume_snapshot_delete_locked {
    my ($class, $scfg, $storeid, $volname, $snap) = @_;

    $class->_verify_mutation_quorum($storeid, $scfg);
    my $vg = $scfg->{'slt-vgname'};
    $class->_verify_storage_identity($storeid, $scfg);
    $class->_verify_owned_volume($storeid, $scfg, $volname);
    my $snapvol = "snap_${volname}_${snap}";

    # The origin's ownership is not sufficient proof that an LV with the
    # expected snapshot name is ours.  Revalidate the snapshot itself before
    # the destructive command so a stale/foreign name collision fails closed.
    $class->_verify_snapshot_postcondition($scfg, $volname, $snapvol, 1);

    my $delete_error = '';
    eval {
        run_command(
            ['/sbin/lvremove', '-f', "$vg/$snapvol"],
            errmsg => "removing snapshot '$vg/$snapvol' failed",
        );
    };
    $delete_error = $@ if $@;

    eval { $class->_verify_snapshot_postcondition($scfg, $volname, $snapvol, 0); };
    my $delete_post_error = $@;
    die(($delete_error || '')
        . "Thin snapshot delete outcome is UNKNOWN; exact absence was not proven; "
        . "no retry was attempted: $delete_post_error")
        if $delete_post_error;
    warn "Thin snapshot delete reported an error, but exact absence is proven; "
        . "continuing without retry: $delete_error" if $delete_error;

    return;
}

sub volume_snapshot_rollback {
    my ($class, $scfg, $storeid, $volname, $snap) = @_;
    $class->_assert_package_operations_released('snapshot rollback');

    if ($class->_is_lazy_mode($scfg)) {
        return $class->_lazy_integration_pending('snapshot rollback')
            if !$class->_lazy_materialized($storeid, $scfg, $volname);
        return $class->_thick_volume_snapshot(
            $scfg, $storeid, $volname, $snap, 'ROLLBACK',
        );
    }

    return $class->_thick_volume_snapshot(
        $scfg, $storeid, $volname, $snap, 'ROLLBACK',
    ) if $class->_allocation_mode($scfg) eq 'thick-generations';

    return $class->_with_mutation_lock($storeid, $scfg, sub {
        return $class->_volume_snapshot_rollback_locked(
            $scfg, $storeid, $volname, $snap,
        );
    });
}

sub _volume_snapshot_rollback_locked {
    my ($class, $scfg, $storeid, $volname, $snap) = @_;

    $class->_verify_mutation_quorum($storeid, $scfg);
    my $vg = $scfg->{'slt-vgname'};
    $class->_verify_storage_identity($storeid, $scfg);
    $class->_verify_owned_volume($storeid, $scfg, $volname);
    my $snapvol = "snap_${volname}_${snap}";
    my (undef, undef, $vmid) = $class->parse_volname($volname);
    my $pool = "sltp-$vmid";
    my $expected_tag = "pve-slt-sid-$storeid";
    my $lvs = PVE::Storage::LVMPlugin::lvm_list_volumes($vg);
    my $vg_lvs = $lvs->{$vg} // {};
    my $pool_info = $vg_lvs->{$pool};

    die "rollback pool '$vg/$pool' is missing\n" if !$pool_info;
    die "rollback pool '$vg/$pool' is not a thin pool\n"
        if !defined($pool_info->{lv_type}) || $pool_info->{lv_type} ne 't';

    my $tags = $pool_info->{tags} // '';
    die "rollback pool '$vg/$pool' belongs to another storage\n"
        if $tags =~ /(?:^|,)pve-slt-sid-[A-Za-z0-9_-]+(?:,|$)/
        && $tags !~ /(?:^|,)\Q$expected_tag\E(?:,|$)/;

    for my $lv ($volname, $snapvol) {
        my $info = $vg_lvs->{$lv};
        die "rollback volume '$vg/$lv' is missing\n" if !$info;
        die "rollback volume '$vg/$lv' is not in expected pool '$pool'\n"
            if !defined($info->{pool_lv}) || $info->{pool_lv} ne $pool;
    }

    $class->_verify_snapshot_postcondition($scfg, $volname, $snapvol, 1);

    # Materialize the rollback result before removing the current origin.
    # The old RC3 ordering removed the origin first and could leave the VM
    # disk missing if snapshot creation then failed.
    my $temporary = "slt-rb-$volname-$$";
    PVE::Storage::Plugin::parse_lvm_name($temporary);
    die "temporary rollback LV '$vg/$temporary' already exists\n"
        if $vg_lvs->{$temporary};

    # lvcreate of the replacement can activate the thin pool even though PVE
    # stopped/deactivated the VM before rollback. Establish normal exclusive
    # ownership (and ThinGuard admission) BEFORE that implicit activation.
    $class->_activate_thin_volume_locked($storeid, $scfg, $volname, $snap, undef);

    my $stage = 'CREATE';
    my $rollback_error;

    eval {
        run_command(
            ['/sbin/lvcreate', '-kn', '-n', $temporary, '-s', "$vg/$snapvol"],
            errmsg => "preparing rollback from '$vg/$snapvol' failed",
        );
        $stage = 'PREPARED';

        $class->_disable_and_verify_autoactivation($vg, $temporary);

        $stage = 'REMOVE_ORIGIN';
        run_command(
            ['/sbin/lvremove', '-f', "$vg/$volname"],
            errmsg => "removing '$vg/$volname' for rollback failed",
        );

        $stage = 'RENAME';
        run_command(
            ['/sbin/lvrename', $vg, $temporary, $volname],
            errmsg => "renaming rollback LV '$vg/$temporary' failed",
        );
        $stage = 'RENAMED';
    };

    $rollback_error = $@;

    if ($rollback_error) {
        # A failed client command may already have committed in LVM. Never
        # classify rollback state from the command exit alone and never retry
        # lvcreate/lvremove/lvrename. Re-read the exact names and ownership.
        my $after = PVE::Storage::LVMPlugin::lvm_list_volumes($vg);
        die "$rollback_error"
            . "rollback outcome is UNKNOWN because VG '$vg' could not be re-inventoried; "
            . "no retry or cleanup was attempted\n"
            if !$after->{$vg};
        my $origin = $after->{$vg}->{$volname};
        my $prepared = $after->{$vg}->{$temporary};
        for my $candidate (
            [$origin, "$vg/$volname"], [$prepared, "$vg/$temporary"],
        ) {
            next if !$candidate->[0];
            die "$rollback_error"
                . "rollback outcome is UNKNOWN: '$candidate->[1]' is not in expected pool '$pool'; "
                . "no retry or cleanup was attempted\n"
                if !defined($candidate->[0]->{pool_lv})
                || $candidate->[0]->{pool_lv} ne $pool;
        }

        if ($stage eq 'RENAME' && $origin && !$prepared) {
            warn "rollback rename reported an error, but exact postcondition proves "
                . "the replacement is published as '$vg/$volname'; continuing without retry: "
                . $rollback_error;
        } elsif ($origin && $prepared) {
            die "$rollback_error"
                . "exact reread proves original '$vg/$volname' and prepared replacement "
                . "'$vg/$temporary' both remain; automatic cleanup and retry were NOT performed\n";
        } elsif (!$origin && $prepared) {
            die "$rollback_error"
                . "exact reread proves rollback data is preserved as '$vg/$temporary' while "
                . "the canonical origin is absent; manual recovery is required and no retry was attempted\n";
        } elsif ($origin && !$prepared) {
            die "$rollback_error"
                . "exact reread proves '$vg/$volname' remains and no prepared replacement exists; "
                . "no retry or cleanup was attempted\n";
        } else {
            die "$rollback_error"
                . "CRITICAL rollback state: both canonical origin and prepared replacement are absent; "
                . "no automated action is safe\n";
        }
    }

    $lvs = PVE::Storage::LVMPlugin::lvm_list_volumes($vg);
    my $restored = $lvs->{$vg} && $lvs->{$vg}->{$volname};

    die "rollback postcondition failed: '$vg/$volname' is missing\n"
        if !$restored;
    die "rollback postcondition failed: '$vg/$volname' is not in pool '$pool'\n"
        if !defined($restored->{pool_lv}) || $restored->{pool_lv} ne $pool;
    $class->_verify_autoactivation_disabled($vg, $volname);

    # Successful offline rollback must not publish an unowned live mapper.
    # Use normal teardown, which retains ownership while any sibling is live
    # and releases it only after exact pool/child absence. On earlier failure
    # ownership and objects remain intact for explicit recovery.
    $class->_deactivate_thin_volume_locked($storeid, $scfg, $volname, $snap, undef);
    $class->_deactivate_thin_volume_locked($storeid, $scfg, $volname, undef, undef);

    return;
}

sub _thick_wait_rollback_admission {
    my ($class, $scfg, $storeid, $volname, $snap) = @_;
    $snap = _thick_snapshot_name($snap);
    $class->_require_thick_identity_config($storeid, $scfg);
    my $vg = $scfg->{'slt-vgname'};
    my $device = "/dev/mapper/$scfg->{'slt-expected-wwid'}";
    my $budget = $scfg->{'slt-mutation-admission-timeout'} // 600;
    die "invalid rollback admission timeout\n"
        if $budget !~ /^\d+$/ || $budget < 10 || $budget > 86400;
    my $deadline = $class->_thick_progress_clock() + $budget;
    my $delay_ms = 250;
    my $pinned;

    while (1) {
        $class->_verify_mutation_quorum($storeid, $scfg);
        $class->_verify_storage_identity($storeid, $scfg, $device);
        my $lvs = $class->_thick_list_volumes_scoped($scfg, $vg, $device);
        my ($state, undef, $anchor) =
            $class->_thick_read_anchor($storeid, $scfg, $volname, $lvs);
        my $anchor_info = $lvs->{$vg}->{$anchor}
            // die "rollback admission anchor disappeared\n";
        my $anchor_uuid = $anchor_info->{lv_uuid} // '';
        die "rollback admission anchor UUID is unavailable\n"
            if $anchor_uuid eq '';
        my (undef, $target_generation, $target_info) =
            $class->_thick_find_snapshot(
                $storeid, $scfg, $volname, $snap, $lvs,
            );
        my $target_uuid = $target_info->{lv_uuid} // '';
        die "rollback admission target snapshot UUID is unavailable\n"
            if $target_uuid eq '';

        if (($state->{phase} // '') eq 'MATERIALIZED') {
            if ($pinned) {
                die "rollback admission transaction changed while waiting\n"
                    if $anchor ne $pinned->{anchor}
                    || $anchor_uuid ne $pinned->{anchor_uuid}
                    || ($state->{tx} // '') ne $pinned->{tx}
                    || ($state->{head} // '') ne $pinned->{new}
                    || int($state->{generation} // -1) != $pinned->{generation}
                    || int($target_generation) != $pinned->{target_generation}
                    || $target_uuid ne $pinned->{target_uuid};
            }
            return 1;
        }

        return $class->_lazy_integration_pending('snapshot rollback')
            if int($state->{v} // 0) == 6;
        die "snapshot rollback requires a materialized Thick HEAD; state is not an exact snapshot transition. The guest was not stopped\n"
            if ($state->{phase} // '') !~ /^(?:COMMITTED|HYDRATING|HYDRATION_COMPLETE|LINEAR_PIVOTED)$/
            || ($state->{op} // '') ne 'SNAPSHOT'
            || ($state->{tx} // '') !~ /^[0-9a-f]{32}$/
            || ($state->{old} // '') eq ''
            || ($state->{new} // '') eq ''
            || ($state->{head} // '') ne $state->{new};
        my $current = {
            anchor => $anchor, anchor_uuid => $anchor_uuid,
            tx => $state->{tx}, old => $state->{old}, new => $state->{new},
            generation => int($state->{generation}),
            transition_snapshot => ($state->{snapshot} // ''),
            target_generation => int($target_generation),
            target_uuid => $target_uuid,
        };
        if ($pinned) {
            for my $field (qw(anchor anchor_uuid tx old new generation transition_snapshot target_generation target_uuid)) {
                die "rollback admission identity changed while waiting\n"
                    if $current->{$field} ne $pinned->{$field};
            }
        } else {
            $pinned = $current;
            warn "waiting for exact Thick snapshot transition '$pinned->{tx}' before rollback of '$storeid:$volname' to '$snap'; no guest stop or rollback effect started\n";
        }

        die "rollback admission timed out while the exact snapshot transition remained incomplete; the guest was not stopped\n"
            if $class->_thick_progress_clock() >= $deadline;
        my $remaining_ms = int(($deadline - $class->_thick_progress_clock()) * 1000);
        my $wait_ms = $delay_ms < $remaining_ms ? $delay_ms : $remaining_ms;
        die "rollback admission timed out; the guest was not stopped\n"
            if $wait_ms < 1;
        # Read-only observation outside VG and transition-executor locks.  The
        # already-running materializer needs both in executor->VG order.
        $class->_thick_observation_pause($wait_ms);
        $delay_ms *= 2 if $delay_ms < 1000;
    }
}

sub volume_rollback_is_possible {
    my ($class, $scfg, $storeid, $volname, $snap, $blockers) = @_;

    # AbstractConfig invokes this public storage hook before it stops a running
    # guest.  Refuse a Thick rollback here when the exact HEAD/snapshot pair is
    # not ready, avoiding a needless availability loss.  The mutating callback
    # repeats all authoritative checks while holding its normal locks; this
    # read-only preflight is deliberately not treated as a TOCTOU-proof grant.
    if ($class->_is_thick_mode($scfg)) {
        $class->_assert_package_operations_released('snapshot rollback preflight');
        $class->_thick_wait_rollback_admission(
            $scfg, $storeid, $volname, $snap,
        );
    }

    return 1;
}

# LXC mounts raw block volumes directly on the host. Ask PVE to freeze those
# filesystems around the storage snapshot callback, matching other block
# backends with external snapshots. This hook is not the VM/QGA freeze policy.
sub volume_snapshot_needs_fsfreeze {
    return 1;
}

sub clone_image {
    die "linked clones are not supported by sharedlvmthin\n";
}

sub volume_has_feature {
    my ($class, $scfg, $feature, $storeid, $volname, $snapname, $running) = @_;

    # Never advertise an inherited PVE operation for an unmaterialized v6
    # Lazy object.  Enforcement also exists in every mutating hook because
    # feature discovery is advisory and some callers bypass it.
    return undef if $class->_is_lazy_mode($scfg)
        && !$class->_lazy_materialized($storeid, $scfg, $volname);

    my $features = {
        snapshot => {
            current => 1,
        },
        resize => {
            current => 1,
        },
        copy => {
            current => 1,
            snap => 1,
        },
        # A newly-created LVM thin LV is logically zero-initialized.  Tell
        # qemu-img callers so they can avoid materialising zero extents during
        # clone/import operations.  This reduces write amplification; it is
        # not a substitute for physical thin-pool headroom.
        sparseinit => {
            current => 1,
        },
    };

    # Upstream clone_disk() asks volume_size_info() without the selected
    # snapshot name, then copies from filesystem_path(..., $snapname).  This
    # is safe only when the immutable snapshot and current materialized HEAD
    # have identical geometry.  Prove that invariant from one device-scoped
    # LVM inventory.  Any missing/ambiguous/transitional state is simply an
    # unsupported feature result, so QemuServer refuses before allocating a
    # destination volume.
    if ($feature eq 'copy' && defined($snapname) && $class->_is_thick_mode($scfg)) {
        my $supported = eval {
            $class->_require_thick_identity_config($storeid, $scfg);
            my $vg = $scfg->{'slt-vgname'};
            my $device = "/dev/mapper/$scfg->{'slt-expected-wwid'}";
            $class->_verify_storage_identity($storeid, $scfg, $device);
            my $lvs = $class->_thick_list_volumes_scoped($scfg, $vg, $device);
            my ($state, $head) = $class->_thick_read_anchor(
                $storeid, $scfg, $volname, $lvs,
            );
            die "snapshot copy requires a materialized Thick HEAD\n"
                if ($state->{phase} // '') ne 'MATERIALIZED';
            my (undef, undef, $snapshot) = $class->_thick_find_snapshot(
                $storeid, $scfg, $volname, $snapname, $lvs,
            );
            my $head_size = int($head->{lv_size} // 0);
            my $snapshot_size = int($snapshot->{lv_size} // 0);
            die "snapshot copy geometry is missing\n"
                if $head_size <= 0 || $snapshot_size <= 0;
            die "snapshot copy geometry differs from the current HEAD\n"
                if $head_size != $snapshot_size;
            1;
        };
        return undef if !$supported;
    }

    my ($vtype, $name, $vmid, $basename, $basevmid, $isBase)
        = $class->parse_volname($volname);

    my $key;

    if ($snapname) {
        $key = 'snap';
    } else {
        $key = $isBase ? 'base' : 'current';
    }

    return 1
        if $features->{$feature}
        && $features->{$feature}->{$key};

    return undef;
}

1;

