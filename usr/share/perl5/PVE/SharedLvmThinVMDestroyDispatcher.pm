package PVE::SharedLvmThinVMDestroyDispatcher;

use strict;
use warnings;
use Digest::SHA qw(sha256_hex);
use Fcntl qw(O_RDONLY O_NOFOLLOW :mode);
use JSON::PP ();
use Time::HiRes qw(clock_gettime CLOCK_MONOTONIC);

# Installed fresh-process orchestration. In particular,
# observe never calls resume_finalizing, execute, or journal append. All test
# dependencies below are trusted in-process DI, never CLI or environment knobs.
my $JSON = JSON::PP->new->canonical;
my $PRODUCTION_USED = 0;
our ($LAUNCHER_SOURCE_SHA256, $DISPATCHER_SOURCE_SHA256);
my $LAUNCHER = '/usr/libexec/pve-sharedlvmthin/sharedlvmthin-vm-destroy-dispatch';
my $DISPATCHER = '/usr/share/perl5/PVE/SharedLvmThinVMDestroyDispatcher.pm';
sub _json { $JSON->encode($_[0]) }
sub _copy { $JSON->decode(_json($_[0])) }
sub _hash { sha256_hex(_json($_[0])) }
sub _need ($$) { die "VM_DESTROY_DISPATCH_REFUSED: $_[1]\n" if !$_[0] }
sub _root { _need($> == 0 && $< == 0, 'root required') }
sub _txid { defined($_[0]) && !ref($_[0]) && scalar($_[0] =~ /\A[0-9a-f]{32}\z/) }
sub _vmid { defined($_[0]) && !ref($_[0]) && scalar($_[0] =~ /\A[1-9][0-9]{0,8}\z/) }

sub _owned_read {
    my ($path, $limit) = @_;
    _need(defined($path) && scalar($path =~ m{\A/(?:[A-Za-z0-9_.+-]+/)*[A-Za-z0-9_.+-]+\z}), 'unsafe fixed path');
    my @parts = split('/', $path); shift @parts; pop @parts;
    my $parent = '';
    for my $part (@parts) {
        $parent .= "/$part";
        my @st = lstat($parent);
        _need(@st && S_ISDIR($st[2]) && $st[4] == 0 && !($st[2] & 0022), 'unsafe evidence parent');
    }
    my @before = lstat($path);
    _need(@before && S_ISREG($before[2]) && $before[4] == 0 && $before[3] == 1
        && !($before[2] & 0022) && $before[7] > 0 && $before[7] <= $limit, 'unsafe evidence file');
    sysopen(my $fh, $path, O_RDONLY | O_NOFOLLOW) or die "cannot open dispatcher evidence: $!\n";
    my @open = stat($fh);
    _need(@open && $open[0] == $before[0] && $open[1] == $before[1], 'evidence inode changed');
    my $raw = '';
    while (1) {
        my $n = sysread($fh, my $chunk, 65536);
        _need(defined($n), 'evidence read failed'); last if !$n;
        $raw .= $chunk; _need(length($raw) <= $limit, 'evidence size budget');
    }
    my @after = stat($fh); close($fh) or die "cannot close dispatcher evidence: $!\n";
    my @named = lstat($path);
    my $identity = join(':', @before[0,1,2,3,4,7,9,10]);
    _need(@named && $identity eq join(':', @after[0,1,2,3,4,7,9,10])
        && $identity eq join(':', @named[0,1,2,3,4,7,9,10]) && length($raw) == $before[7], 'evidence changed while reading');
    return $raw;
}

sub _fresh_bootstrap {
    _root();
    _need(!$PRODUCTION_USED++, 'one production attempt per fresh process');
    _need(!grep({ m{\APVE/} && $_ ne 'PVE/SharedLvmThinVMDestroyDispatcher.pm' } keys %INC),
        'PVE modules were loaded before the fresh-process source boundary');
    require File::Find;
    my (%before, $bytes, $files);
    my $deadline = clock_gettime(CLOCK_MONOTONIC) + 30;
    File::Find::find({ no_chdir => 1, wanted => sub {
        _need(clock_gettime(CLOCK_MONOTONIC) < $deadline, 'PVE source snapshot deadline');
        my $path = $File::Find::name;
        my @st = lstat($path);
        _need(@st && !S_ISLNK($st[2]), 'symlink in PVE source tree');
        return if S_ISDIR($st[2]);
        return if $path !~ /\.pm\z/;
        _need(++$files <= 4096, 'PVE source file budget');
        my $raw = _owned_read($path, 8 * 1024 * 1024);
        $bytes += length($raw); _need($bytes <= 128 * 1024 * 1024, 'PVE source byte budget');
        $before{$path} = sha256_hex($raw);
    } }, '/usr/share/perl5/PVE');
    _need($files, 'PVE source inventory empty');
    _need(defined($DISPATCHER_SOURCE_SHA256) && defined($LAUNCHER_SOURCE_SHA256)
        && scalar($DISPATCHER_SOURCE_SHA256 =~ /\A[0-9a-f]{64}\z/)
        && scalar($LAUNCHER_SOURCE_SHA256 =~ /\A[0-9a-f]{64}\z/)
        && ($before{$DISPATCHER} // '') eq $DISPATCHER_SOURCE_SHA256,
        'dispatcher changed since launcher load');
    my $launcher_sha256 = sha256_hex(_owned_read($LAUNCHER, 8 * 1024 * 1024));
    _need($launcher_sha256 eq $LAUNCHER_SOURCE_SHA256, 'launcher changed since initial load');
    require PVE::SharedLvmThinVMDestroyRuntime;
    require PVE::SharedLvmThinVMDestroyInventory;
    require PVE::SharedLvmThinVMDestroyPlanAdapter;
    require PVE::SharedLvmThinVMDestroyPlanV3;
    require PVE::SharedLvmThinVMDestroyRecoveryDescriptor;
    require PVE::SharedLvmThinVMDestroyJournal;
    require PVE::SharedLvmThinVMDestroyFinalizationObserver;
    require PVE::SharedLvmThinVMDestroy;
    PVE::SharedLvmThinVMDestroy::_load_pve();
    require PVE::INotify;
    require PVE::API2::Qemu;
    my %loaded;
    for my $key (sort keys %INC) {
        next if $key !~ m{\APVE/};
        my $path = "/usr/share/perl5/$key";
        _need(defined($INC{$key}) && !ref($INC{$key}) && $INC{$key} eq $path && exists($before{$path}),
            'loaded PVE module escaped pinned installed source tree');
        _need(sha256_hex(_owned_read($path, 8 * 1024 * 1024)) eq $before{$path}, 'PVE source changed during load');
        $loaded{$path} = $before{$path};
    }
    my $identity = { schema => 'sharedlvmthin-fresh-pve-load/v1', source_sha256 => _hash(\%loaded),
        module_count => scalar(keys %loaded), launcher_sha256 => $launcher_sha256,
        dispatcher_sha256 => $before{$DISPATCHER} };
    my $pid = $$;
    return { identity => $identity, check => sub {
        _need($$ == $pid, 'forked/reused dispatcher process');
        _need(sha256_hex(_owned_read($LAUNCHER, 8 * 1024 * 1024)) eq $launcher_sha256,
            'launcher changed since fresh load');
        my %now;
        my $until = clock_gettime(CLOCK_MONOTONIC) + 30;
        for my $key (sort keys %INC) {
            next if $key !~ m{\APVE/};
            my $path = "/usr/share/perl5/$key";
            _need(clock_gettime(CLOCK_MONOTONIC) < $until, 'loaded source recheck deadline');
            _need(($INC{$key} // '') eq $path && exists($loaded{$path}), 'new/substituted PVE runtime module');
            $now{$path} = sha256_hex(_owned_read($path, 8 * 1024 * 1024));
        }
        _need(_json(\%now) eq _json(\%loaded), 'installed source changed since fresh load');
        return 1;
    } };
}

sub _raw_config {
    my ($vmid, $node) = @_;
    _need(_vmid($vmid) && defined($node) && !ref($node)
        && scalar($node =~ /\A[A-Za-z0-9][A-Za-z0-9_.-]*\z/), 'raw config identity unknown');
    # /etc/pve/qemu-server is a local-node symlink. Resolve only through the
    # authoritative node identity, never through a caller-selected path.
    return _owned_read("/etc/pve/nodes/$node/qemu-server/$vmid.conf", 4 * 1024 * 1024);
}

sub _observe_journal {
    my ($txid, $durable) = @_;
    _need(_txid($txid), 'invalid journal observation txid');
    require PVE::Tools;
    my ($output, $errors) = ('', '');
    local %ENV = (PATH => '/usr/sbin:/usr/bin:/sbin:/bin', LC_ALL => 'C', LANG => 'C');
    PVE::Tools::run_command(['/usr/bin/python3', '-I',
        '/usr/libexec/pve-sharedlvmthin/sharedlvmthin-vm-destroy',
        $durable ? '_observe-durable' : 'observe', '--request-id', $txid],
        timeout => 30, outfunc => sub { $output .= $_[0] . "\n"; _need(length($output) <= 32 * 1024 * 1024 + 4096, 'journal output budget') },
        errfunc => sub { $errors .= $_[0]; _need(length($errors) <= 8192, 'journal diagnostic budget') });
    _need($errors eq '', 'journal observation diagnostics');
    my $value = eval { $JSON->decode($output) };
    _need(!$@ && ref($value) eq 'HASH' && $output eq JSON::PP->new->canonical->ascii->encode($value) . "\n", 'noncanonical journal observation');
    return $value;
}

sub new {
    my ($class, %args) = @_;
    my %defaults = (
        bootstrap => \&_fresh_bootstrap,
        runtime_factory => sub { PVE::SharedLvmThinVMDestroyRuntime->new(@_) },
        collector_factory => sub { PVE::SharedLvmThinVMDestroyInventory->new(@_) },
        raw_reader => \&_raw_config,
        journal_factory => sub { require PVE::SharedLvmThinVMDestroyJournal; PVE::SharedLvmThinVMDestroyJournal->new(@_) },
        journal_recovery_factory => sub { require PVE::SharedLvmThinVMDestroyJournal; PVE::SharedLvmThinVMDestroyJournal->open_existing(@_) },
        descriptor_factory => sub { require PVE::SharedLvmThinVMDestroyRecoveryDescriptor; PVE::SharedLvmThinVMDestroyRecoveryDescriptor->new(@_) },
        finalization_factory => sub { PVE::SharedLvmThinVMDestroyFinalizationObserver->new() },
        executor_factory => sub { PVE::SharedLvmThinVMDestroy->new(@_) },
        journal_observer => \&_observe_journal,
        durable_journal_observer => sub { _observe_journal($_[0], 1) },
    );
    _need(!grep({ !exists($defaults{$_}) || ref($args{$_}) ne 'CODE' } keys %args), 'unknown/invalid dispatcher dependency');
    return bless { %defaults, %args, attempted => 0, pid => $$ }, $class;
}

sub _groups {
    my ($cfg, $volids) = @_;
    _need(ref($cfg) eq 'HASH' && ref($cfg->{ids}) eq 'HASH' && ref($volids) eq 'ARRAY' && @$volids, 'empty/unknown storage plan');
    my (%groups, %seen, %physical_names);
    for my $volid (@$volids) {
        _need(defined($volid) && !ref($volid) && !$seen{$volid}++, 'duplicate volume selection');
        my ($sid) = $volid =~ /\A([A-Za-z0-9][A-Za-z0-9_.-]*):/;
        my $scfg = defined($sid) ? $cfg->{ids}->{$sid} : undef;
        _need(ref($scfg) eq 'HASH' && ($scfg->{type} // '') eq 'sharedlvmthin' && $scfg->{shared}, 'unsupported/unshared volume storage');
        my $physical = { map { $_ => $scfg->{$_} } qw(slt-vgname slt-expected-vg-uuid slt-expected-pv-uuid slt-expected-wwid) };
        for my $field (keys %$physical) {
            _need(defined($physical->{$field}) && !ref($physical->{$field})
                && scalar($physical->{$field} =~ /\A[A-Za-z0-9+_.-]+\z/), 'incomplete physical storage identity');
        }
        my $key = _hash($physical);
        _need(!exists($physical_names{$scfg->{'slt-vgname'}}) || $physical_names{$scfg->{'slt-vgname'}} eq $key, 'same VG name has conflicting identities');
        $physical_names{$scfg->{'slt-vgname'}} = $key;
        $groups{$key} //= { physical => $physical, volids => [], collector_sid => $sid };
        push @{$groups{$key}->{volids}}, $volid;
        $groups{$key}->{collector_sid} = $sid if $sid lt $groups{$key}->{collector_sid};
    }
    for my $group (values %groups) {
        my @aliases = sort grep { ref($cfg->{ids}->{$_}) eq 'HASH'
            && ($cfg->{ids}->{$_}->{type} // '') eq 'sharedlvmthin'
            && ($cfg->{ids}->{$_}->{'slt-vgname'} // '') eq $group->{physical}->{'slt-vgname'} } keys %{$cfg->{ids}};
        for my $sid (@aliases) {
            _need($cfg->{ids}->{$sid}->{shared}, 'unshared sibling alias');
            for my $field (keys %{$group->{physical}}) {
                _need(($cfg->{ids}->{$sid}->{$field} // '') eq $group->{physical}->{$field}, 'sibling alias physical mismatch');
            }
        }
        $group->{aliases} = \@aliases;
        $group->{volids} = [sort @{$group->{volids}}];
    }
    return [map { $groups{$_} } sort keys %groups];
}

sub _prepare {
    my ($self, $vmid, $conf, $cfg, $volids, $runtime) = @_;
    my $raw = $self->{raw_reader}->($vmid, $runtime->{node});
    my (@envelopes, @collections);
    for my $group (@{_groups($cfg, $volids)}) {
        my $collector = $self->{collector_factory}->(storage_id => $group->{collector_sid}, vmid => $vmid);
        my $bundle = $collector->plan(_copy($group->{volids}));
        my $physical = $group->{physical};
        my $scope = { node => $runtime->{node}, boot_id => $runtime->{boot_id},
            vg_uuid => $physical->{'slt-expected-vg-uuid'}, pv_uuid => $physical->{'slt-expected-pv-uuid'},
            wwid => $physical->{'slt-expected-wwid'}, storage_ids => $group->{aliases}, config_sha256 => _hash($cfg) };
        _need(ref($bundle) eq 'HASH' && ($bundle->{authority} // '') eq 'NONE'
            && ref($bundle->{inventory}) eq 'HASH' && _json($bundle->{inventory}->{scope}) eq _json($scope), 'collector scope differs from runtime/config');
        my $v2 = PVE::SharedLvmThinVMDestroyPlanAdapter->adapt_plan(plan_version => 2,
            storage_plan => $bundle->{storage_plan}, raw_config => $raw, locked_config => $conf,
            storecfg => $cfg, bindings => $bundle->{bindings}, expected_scope => $scope);
        push @envelopes, { collector_sid => $group->{collector_sid}, executor_v2 => $v2,
            foreign_evidence => $bundle->{foreign_evidence}, foreign_evidence_sha256 => $bundle->{foreign_evidence_sha256} };
        push @collections, { collector => $collector, volids => $group->{volids}, baseline => _copy($bundle) };
    }
    my $plan = PVE::SharedLvmThinVMDestroyPlanV3->compose(scopes => \@envelopes);
    _need(_json($plan->{volids}) eq _json($volids), 'multi-VG union differs from locked config');
    # Inventory currently retains global DM rows in each scoped raw foreign
    # baseline. Do not dispatch a known-unobservable composition: removing a
    # target mapper in VG B would invalidate VG A's immutable foreign baseline.
    # Preserve the baseline unchanged; require explicit quiescent inventories.
    my (%runtime_names, %runtime_uuids);
    for my $scope (@{$plan->{scopes}}) {
        for my $root (@{$scope->{executor_v2}->{roots}}) {
            for my $dm (@{$root->{runtime_ids}}) {
                $runtime_names{$dm->{name}} = $scope->{scope_id};
                $runtime_uuids{$dm->{uuid}} = $scope->{scope_id};
            }
        }
    }
    for my $scope (@{$plan->{scopes}}) {
        _need(ref($scope->{foreign_evidence}->{dm}) eq 'ARRAY', 'raw foreign DM inventory missing');
        for my $row (@{$scope->{foreign_evidence}->{dm}}) {
            _need(ref($row) eq 'ARRAY' && @$row == 3, 'raw foreign DM identity malformed');
            _need(!exists($runtime_names{$row->[0]}) && !exists($runtime_uuids{$row->[1]}),
                'cross-VG target DM remains in foreign baseline; explicit quiescence required');
        }
    }
    return ($plan, \@collections);
}

sub _observe_storage {
    my ($self, $plan) = @_;
    $plan = PVE::SharedLvmThinVMDestroyPlanV3->validate($plan);
    my @proofs;
    for my $scope (@{$plan->{scopes}}) {
        my $bundle = PVE::SharedLvmThinVMDestroyPlanV3->inventory_bundle($plan, $scope->{scope_id});
        my $collector = $self->{collector_factory}->(storage_id => $scope->{collector_sid}, vmid => $plan->{vmid});
        my $proof = $collector->observe($bundle);
        _need(ref($proof) eq 'HASH' && ($proof->{authority} // '') eq 'NONE'
            && ($proof->{result} // '') eq 'EXACT_PLANNED_STORAGE_ABSENT'
            && ($proof->{plan_sha256} // '') eq $scope->{storage_plan_sha256}
            && ($proof->{foreign_evidence_sha256} // '') eq $scope->{foreign_evidence_sha256}, 'scoped storage absence is UNKNOWN');
        push @proofs, { scope_id => $scope->{scope_id}, observation => $proof };
    }
    return { authority => 'NONE', status => 'ABSENT', foreign_unchanged => 'YES',
        object_ids => _copy($plan->{object_ids}), scopes => \@proofs, plan_sha256 => $plan->{plan_sha256} };
}

sub execute {
    my ($self, $vmid, $txid) = @_;
    _root(); _need(_vmid($vmid) && _txid($txid), 'invalid VM/transaction identity');
    _need($self->{pid} == $$ && !$self->{attempted}++, 'dispatcher already attempted or forked; no retry');
    my $fresh = $self->{bootstrap}->();
    _need(ref($fresh) eq 'HASH' && ref($fresh->{check}) eq 'CODE' && ref($fresh->{identity}) eq 'HASH', 'fresh-process boundary missing');
    my $runtime = $self->{runtime_factory}->();
    my $verify = sub {
        _need($fresh->{check}->(), 'fresh-process source boundary failed');
        my $value = $runtime->verify($vmid);
        _need(ref($value) eq 'HASH' && ($value->{authority} // '') eq 'NONE'
            && ($value->{status} // '') eq 'QUALIFIED' && ($value->{vmid} // '') eq "$vmid", 'runtime scope/purpose not executable');
        $value = _copy($value); $value->{fresh_process} = _copy($fresh->{identity});
        return $value;
    };
    my $before = $verify->(); # MUST precede Executor::execute's VM lock.
    my $journal = $self->{journal_factory}->(request_id => $txid);
    my $descriptor = $self->{descriptor_factory}->(txid => $txid);
    my $finalization = $self->{finalization_factory}->();
    my ($locked_runtime, $prepared_plan, $collections, $descriptor_ack, $descriptor_attempted, $journal_head);
    my $executor_runtime_reads = 0;
    my $executor = $self->{executor_factory}->(
        verify_runtime => sub {
            _need(@_ == 1 && defined($_[0]) && "$_[0]" eq "$vmid", 'unexpected executor runtime identity');
            # Existing executor has exactly two runtime boundaries: initial
            # locked admission, then the final recheck AFTER durable DISPATCHED
            # and immediately before native destroy. Neither is a reusable
            # callback capability at an arbitrary stage.
            _need(($executor_runtime_reads == 0 && !defined($locked_runtime)
                    && !defined($prepared_plan) && !defined($journal_head))
                || ($executor_runtime_reads == 1 && defined($locked_runtime)
                    && defined($prepared_plan) && $descriptor_ack && ($journal_head // '') eq 'DISPATCHED'),
                'unexpected executor runtime callback phase');
            $executor_runtime_reads++;
            my $now = $verify->(); PVE::SharedLvmThinVMDestroyRuntime->assert_same($before, $now);
            $locked_runtime //= _copy($now); return $now;
        },
        prepare_plan => sub {
            _need(defined($locked_runtime) && !defined($prepared_plan), 'plan outside qualified locked execution');
            ($prepared_plan, $collections) = $self->_prepare(@_, $locked_runtime);
            return _copy($prepared_plan);
        },
        observe_plan => sub {
            _need($descriptor_ack && _json($_[0]) eq _json($prepared_plan), 'observation plan/descriptor mismatch');
            return $self->_observe_storage($_[0]);
        },
        observe_finalization => sub { $finalization->observe(@_) },
        journal => sub {
            my ($stage, $receipt) = @_;
            _need(ref($receipt) eq 'HASH' && ($receipt->{txid} // '') eq $txid
                && ($receipt->{vmid} // '') eq "$vmid" && _json($receipt->{runtime}) eq _json($before)
                && _json($receipt->{plan}) eq _json($prepared_plan), 'journal core differs from exact prepared inputs');
            if ($stage eq 'PREPARED' || $stage eq 'DISPATCHED') {
                PVE::SharedLvmThinVMDestroyRuntime->assert_same($before, $verify->());
            }
            if ($stage eq 'PREPARED') {
                _need(!$descriptor_attempted++, 'descriptor create already attempted; no adoption');
                my $context = { schema => 'sharedlvmthin-vm-destroy-executor-core/v1',
                    receipt => { map { $_ => $receipt->{$_} } qw(schema txid vmid config runtime plan) } };
                my $ack = $descriptor->create(_copy($context));
                _need(ref($ack) eq 'HASH' && ($ack->{authority} // '') eq 'NONE'
                    && JSON::PP::is_bool($ack->{durable}) && $ack->{durable}
                    && _json($ack->{descriptor}->{context}) eq _json($context), 'descriptor durable exact acknowledgement missing');
                $descriptor_ack = 1;
            }
            _need($descriptor_ack, 'descriptor must precede every journal publication');
            if ($stage eq 'DISPATCHED') {
                for my $collection (@$collections) {
                    my $now = $collection->{collector}->plan(_copy($collection->{volids}));
                    _need(_json($now) eq _json($collection->{baseline}), 'storage inventory changed before dispatch');
                }
            }
            my $ack = $journal->append($stage, $receipt);
            _need(defined($ack) && !ref($ack) && "$ack" eq '1', 'journal acknowledgement missing');
            $journal_head = $stage;
            return 1;
        },
    );
    return $executor->execute($vmid, $txid);
}

sub complete_finalizing_absent {
    my ($self, $vmid, $txid) = @_;
    _root(); _need(_vmid($vmid) && _txid($txid), 'invalid VM/transaction identity');
    _need($self->{pid} == $$ && !$self->{attempted}++, 'dispatcher already attempted or forked; no retry');
    my $fresh = $self->{bootstrap}->();
    _need(ref($fresh) eq 'HASH' && ref($fresh->{check}) eq 'CODE' && ref($fresh->{identity}) eq 'HASH',
        'fresh-process boundary missing');
    my $check = sub { _need($fresh->{check}->(), 'fresh-process source boundary failed'); return _copy($fresh->{identity}) };
    $check->();
    my $descriptor = $self->{descriptor_factory}->(txid => $txid)->read();
    _need(ref($descriptor) eq 'HASH' && ($descriptor->{authority} // '') eq 'NONE'
        && ($descriptor->{txid} // '') eq $txid && JSON::PP::is_bool($descriptor->{durable}) && $descriptor->{durable}
        && ref($descriptor->{descriptor}) eq 'HASH'
        && ref($descriptor->{descriptor}->{context}) eq 'HASH', 'durable recovery descriptor unavailable');
    my $context = _copy($descriptor->{descriptor}->{context});
    _need(($context->{schema} // '') eq 'sharedlvmthin-vm-destroy-executor-core/v1'
        && ref($context->{receipt}) eq 'HASH' && ($context->{receipt}->{txid} // '') eq $txid
        && ($context->{receipt}->{vmid} // '') eq "$vmid"
        && ref($context->{receipt}->{runtime}) eq 'HASH'
        && ref($context->{receipt}->{runtime}->{fresh_process}) eq 'HASH'
        && _json($context->{receipt}->{runtime}->{fresh_process}) eq _json($check->()),
        'descriptor VM/transaction/fresh-source identity mismatch');
    my $journal = $self->{journal_recovery_factory}->(request_id => $txid, expected_context => _copy($context));
    my $finalization = $self->{finalization_factory}->();
    my $runtime = $self->{runtime_factory}->(
        fresh_process_reader => $check,
        journal_reader => sub {
            _need(@_ == 1 && $_[0] eq $txid, 'unexpected completion journal identity');
            $check->(); return $self->{durable_journal_observer}->($txid);
        },
        finalization_reader => sub {
            _need(@_ == 1 && "$_[0]" eq "$vmid", 'unexpected completion finalization identity');
            $check->(); return $finalization->observe($vmid);
        },
    );
    my $forbidden = sub { die "VM_DESTROY_DISPATCH_REFUSED: effectful callback forbidden in journal-only completion\n" };
    my $executor = $self->{executor_factory}->(
        map({ $_ => $forbidden } qw(verify_runtime prepare_plan observe_plan observe_finalization journal)),
        read_journal => sub { $check->(); return $journal->read_journal(@_) },
        verify_finalizing_absent => sub {
            my ($requested_vmid, %args) = @_;
            _need("$requested_vmid" eq "$vmid" && ($args{txid} // '') eq $txid
                && _json($args{expected_context}) eq _json($context), 'completion immutable request mismatch');
            $check->();
            my $proof = $runtime->verify_finalizing_absent($requested_vmid, %args);
            $check->();
            _need(ref($proof) eq 'HASH' && ($proof->{status} // '') eq 'OBSERVED'
                && ($proof->{purpose} // '') eq 'FINALIZATION_OBSERVATION_ONLY'
                && ($proof->{authority} // '') eq 'NONE', 'completion runtime purpose mismatch');
            return $proof;
        },
        journal_cas => sub {
            my ($stage, $receipt, $head) = @_;
            _need($stage eq 'COMPLETE' && ref($receipt) eq 'HASH'
                && _json({map {$_ => $receipt->{$_}} qw(schema txid vmid config runtime plan)})
                    eq _json($context->{receipt}), 'completion append outside exact immutable core');
            $check->();
            return $journal->append_cas($stage, $receipt, $head);
        },
    );
    # No execute/resume fallback, collector, raw-config read or descriptor create.
    return $executor->complete_finalizing_absent($vmid, $txid);
}

sub observe {
    my ($self, $txid) = @_;
    _root(); _need(_txid($txid), 'invalid transaction identity');
    # No runtime verification, PVE load boundary, plan rebuild, executor,
    # append, or finalization. Crash/reboot cannot turn an observation into
    # execute authority. Existing helpers validate their own durable formats.
    my $descriptor = $self->{descriptor_factory}->(txid => $txid)->read();
    my $journal = $self->{journal_observer}->($txid);
    _need(ref($descriptor) eq 'HASH' && ($descriptor->{authority} // '') eq 'NONE'
        && ($descriptor->{txid} // '') eq $txid, 'descriptor observation identity mismatch');
    _need(ref($journal) eq 'HASH' && ($journal->{schema} // '') eq 'sharedlvmthin-vm-destroy-journal/v1'
        && ($journal->{authority} // '') eq 'NONE' && ref($journal->{records}) eq 'ARRAY'
        && @{$journal->{records}} <= 32, 'journal observation unknown');
    for my $row (@{$journal->{records}}) {
        _need(ref($row) eq 'HASH' && ($row->{request_id} // '') eq $txid
            && _json($row->{context}) eq _json($descriptor->{descriptor}->{context}), 'journal/descriptor context mismatch');
    }
    return { schema => 'sharedlvmthin-vm-destroy-dispatch-observation/v1', authority => 'NONE',
        status => 'OBSERVE_ONLY', txid => $txid, descriptor => $descriptor, journal => $journal };
}

1;
