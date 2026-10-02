use strict;
use warnings;
use FindBin;
use lib "$FindBin::Bin/lib", "$FindBin::Bin/../../usr/share/perl5";
use Test::More;
use JSON::PP;
use Digest::SHA qw(sha1_hex);
use PVE::SharedLvmThinThick qw(anchor_tags generation_tags lazy_object_tags
    anchor_name generation_name mapper_name object_key clone_geometry decode_anchor_tags);
use PVE::SharedLvmThinVMDestroyInventory;
use PVE::SharedLvmThinVMDestroyPlanAdapter;

# Hermetic source-tree fixture: retain the real allocation-mode validator but
# do not read a host's installed profile marker. Production collector unchanged.
no warnings 'redefine';
local *PVE::Storage::Custom::SharedLvmThinPlugin::_package_flavor = sub { 'dual' };
use warnings;

my $JSON = JSON::PP->new->canonical;
sub copy { $JSON->decode($JSON->encode($_[0])) }
sub dm_head { my $name = $_[0]->{head}; $name =~ s/-/--/g; return "testvg-$name" }
sub row {
    my ($name, $uuid, $attr, $tags, %extra) = @_;
    return { vg_name => 'testvg', lv_name => $name, lv_uuid => $uuid, lv_size => '4194304',
        lv_attr => $attr, lv_tags => $tags, pool_lv => '', origin => '', data_lv => '', metadata_lv => '', %extra };
}
sub fixture {
    my ($mode) = @_;
    my $vol = 'vm-101-disk-0';
    my $cfg = { ids => { store => { type => 'sharedlvmthin', shared => 1, 'slt-vgname' => 'testvg',
        'slt-expected-vg-uuid' => 'vguuid', 'slt-expected-pv-uuid' => 'pvuuid', 'slt-expected-wwid' => '60001',
        'slt-vg-reserve-gib' => 1,
        'slt-allocation-mode' => $mode eq 'thin' ? 'thin' : $mode eq 'lazy' ? 'thick-generations-lazy' : 'thick-generations' } } };
    my $f = { cfg => $cfg, rows => [], dm => [], calls => [], boot => 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
        vg_tags => '', pv_uuid => 'pvuuid', vol => "store:$vol" };
    if ($mode eq 'thin') {
        $f->{rows} = [
            row('sltp-101', 'pooluuid', 'twi---tz--', 'pve-slt-sid-store,pve-slt-owner-v1',
                data_lv => '[sltp-101_tdata]', metadata_lv => '[sltp-101_tmeta]'),
            row('[sltp-101_tdata]', 'datauuid', 'Twi-------', ''),
            row('[sltp-101_tmeta]', 'metauuid', 'ewi-------', ''),
            row($vol, 'voluuid', 'Vwi---tz--', '', pool_lv => 'sltp-101'),
            row("snap_${vol}_a", 'snapuuid', 'Vri---tz--', '', pool_lv => 'sltp-101', origin => $vol),
        ];
    } else {
        my $anchor = anchor_name('vguuid', $vol);
        my $head = generation_name('vguuid', $vol, 0);
        my $tx = 'a' x 32;
        my $state = { v => 5, sid => 'store', vol => $vol, phase => 'MATERIALIZED', tx => $tx,
            op => 'ALLOC', snapshot => 'none', source => $head, old => $head, new => $head,
            head => $head, generation => 0, region => 2048 };
        if ($mode eq 'lazy') {
            my $meta = 'sltg-lazy-meta';
            $state = { %$state, v => 6, phase => 'LAZY_DORMANT', policy => 'lazy-zero', bytes => 4194304,
                metadata => $meta, data_uuid => 'datauuid', metadata_uuid => 'metauuid', zero_source => 'dm-zero',
                publication => 1, owner_node => 'none', owner_boot => 'none', owner_epoch => 'none' };
            my $geometry = clone_geometry(4194304, 2048);
            push @{$f->{rows}}, row($head, 'datauuid', '-wi-------', join(',', @{lazy_object_tags(
                sid => 'store', vol => $vol, tx => $tx, kind => 'data', bytes => 4194304, region => 2048)}));
            push @{$f->{rows}}, row($meta, 'metauuid', '-wi-------', join(',', @{lazy_object_tags(
                sid => 'store', vol => $vol, tx => $tx, kind => 'metadata', bytes => $geometry->{metadata_bytes}, region => 2048)}),
                lv_size => "$geometry->{metadata_bytes}");
        } else {
            push @{$f->{rows}}, row($head, 'headuuid', '-wi-------', join(',', @{generation_tags(
                sid => 'store', vol => $vol, role => 'head', generation => 0)}));
        }
        push @{$f->{rows}}, row($anchor, 'anchoruuid', '-wi-------', join(',', @{anchor_tags(%$state)}));
        $f->{state} = $state; $f->{anchor} = $anchor; $f->{head} = $head;
    }
    push @{$f->{rows}}, row('[lvol0_pmspare]', 'spareuuid', 'ewi-------', '');
    return $f;
}
sub collector {
    my ($f) = @_;
    return PVE::SharedLvmThinVMDestroyInventory->new(storage_id => 'store', vmid => 101,
        config_reader => sub { copy($f->{cfg}) }, identity_reader => sub {
            $f->{identity_reads}++;
            ['node-a', $f->{boot_drift} && $f->{identity_reads} > 1
                ? 'bbbbbbbb-bbbb-cccc-dddd-eeeeeeeeeeee' : $f->{boot}]
        },
        dm_reader => sub { copy($f->{dm}) }, runner => sub {
            my ($argv) = @_;
            push @{$f->{calls}}, [@$argv];
            die 'injected probe error' if $f->{fail};
            my ($tool) = $argv->[0] =~ m{/([^/]+)$};
            my ($kind, $rows);
            if ($tool eq 'vgs') { $kind = 'vg'; $rows = [{vg_name => 'testvg', vg_uuid => 'vguuid', vg_tags => $f->{vg_tags}}] }
            elsif ($tool eq 'pvs') { $kind = 'pv'; $rows = [{pv_uuid => $f->{pv_uuid}, pv_name => '/dev/mapper/60001', vg_uuid => 'vguuid'}] }
            elsif ($tool eq 'lvs') { $kind = 'lv'; $rows = copy($f->{rows}); $f->{lvs_reads}++;
                $rows->[0]->{lv_size}++ if $f->{drift} && $f->{lvs_reads} > 1;
            } else { die "unexpected tool $tool" }
            return $JSON->encode({report => [{$kind => $rows}]});
        });
}
sub refused {
    my ($code, $regex, $label) = @_;
    my $ok = eval { $code->(); 1 }; my $error = $@;
    ok(!$ok, "$label refused"); like($error, $regex, "$label diagnostic");
}

subtest 'plugin decoder cannot mutate the raw observation baseline' => sub {
    my $read_anchor = \&PVE::Storage::Custom::SharedLvmThinPlugin::_thick_read_anchor;
    no warnings 'redefine';
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_read_anchor = sub {
        my @result = $read_anchor->(@_);
        # Model decoder normalization/cache scratch in its input inventory.
        # Neither it nor numeric scalar flag changes are external LVM drift.
        my $view = $_[4]->{testvg};
        $view->{$_}->{decoder_scratch} = 'local-only' for keys %$view;
        return @result;
    };
    for my $mode (qw(thick lazy)) {
        my $f = fixture($mode);
        my $bundle = collector($f)->plan([$f->{vol}]);
        is($bundle->{authority}, 'NONE', "$mode copied decoder input accepted");
        ok(!exists($bundle->{foreign_evidence}->{lvs}->{lvol0_pmspare}->{decoder_scratch}),
            "$mode raw foreign baseline excludes decoder scratch");
        $f = fixture($mode); $f->{drift} = 1;
        refused(sub { collector($f)->plan([$f->{vol}]) }, qr/changed during collection/,
            "$mode real probe drift still refused with mutating decoder");
    }
};

for my $mode (qw(thin thick lazy)) {
    subtest "$mode complete collection and typed adapter" => sub {
        my $f = fixture($mode); my $c = collector($f);
        my $bundle = $c->plan([$f->{vol}]);
        is($bundle->{authority}, 'NONE', 'no authority');
        is($bundle->{storage_plan}->{volumes}->[0]->{mode}, $mode, 'correct mode');
        is($bundle->{inventory}->{opaque_foreign_objects}->[0]->{name}, 'lvol0_pmspare', 'spare retained without VM ownership');
        ok(!exists($bundle->{bindings}->{objects}->{'lv:lvol0_pmspare'}), 'spare not a destroy binding');
        my $raw = "scsi0: $f->{vol}\n";
        my $adapted = PVE::SharedLvmThinVMDestroyPlanAdapter->adapt_plan(
            storage_plan => $bundle->{storage_plan}, raw_config => $raw,
            locked_config => {scsi0 => $f->{vol}, digest => sha1_hex($raw)},
            storecfg => $f->{cfg}, bindings => $bundle->{bindings}, expected_scope => $bundle->{inventory}->{scope});
        is($adapted->{authority}, 'NONE', 'typed adapter remains inert');
        for my $argv (@{$f->{calls}}) {
            like($argv->[0], qr{^/sbin/(?:lvs|vgs|pvs)$}, 'only reporting executable');
            ok(grep({ $_ eq '--readonly' } @$argv), 'readonly set');
            ok(grep({ $_ eq '/dev/mapper/60001' } @$argv), 'WWID scoped');
        }
        $f->{rows} = [grep { $_->{lv_uuid} eq 'spareuuid' } @{$f->{rows}}];
        is($c->observe($bundle)->{result}, 'EXACT_PLANNED_STORAGE_ABSENT', 'post-delete absence with unchanged spare');
        $f->{rows}->[0]->{lv_size}++;
        refused(sub { $c->observe($bundle) }, qr/raw foreign namespace drift/, 'foreign spare size drift');
    };
}

my @cases = (
    ['omitted Thin metadata', 'thin', sub { $_[0]->{rows} = [grep { $_->{lv_uuid} ne 'metauuid' } @{$_[0]->{rows}}] }, qr/missing or multiply owned/],
    ['duplicate UUID', 'thin', sub { $_[0]->{rows}->[1]->{lv_uuid} = 'pooluuid' }, qr/duplicate normalized LV UUID/],
    ['normalized UUID alias', 'thin', sub { $_[0]->{rows}->[1]->{lv_uuid} = 'pool-uuid' }, qr/duplicate normalized LV UUID/],
    ['missing UUID', 'thin', sub { $_[0]->{rows}->[1]->{lv_uuid} = '' }, qr/incomplete\/duplicate/],
    ['missing field', 'thin', sub { delete $_[0]->{rows}->[1]->{lv_uuid} }, qr/non-scalar report field/],
    ['duplicate LV name', 'thin', sub { push @{$_[0]->{rows}}, copy($_[0]->{rows}->[1]) }, qr/incomplete\/duplicate/],
    ['foreign owner', 'thin', sub { $_[0]->{rows}->[0]->{lv_tags} .= ',pve-slt-owner-node-other,pve-slt-owner-epoch-' . ('a' x 32) }, qr/Thin owner is foreign/],
    ['foreign storage tag', 'thin', sub { $_[0]->{rows}->[0]->{lv_tags} =~ s/sid-store/sid-foreign/ }, qr/foreign storage ownership/],
    ['foreign Thin disk', 'thin', sub { push @{$_[0]->{rows}}, row('vm-202-disk-1','uuid2','Vwi---tz--','',pool_lv=>'sltp-101') }, qr/Thin pool\/VM ownership/],
    ['foreign snapshot', 'thin', sub { $_[0]->{rows}->[4]->{origin} = 'vm-202-disk-0' }, qr/snapshot origin/],
    ['missing structural reference', 'thin', sub { $_[0]->{rows}->[0]->{data_lv} = '' }, qr/structural LV references/],
    ['wrong PV', 'thin', sub { $_[0]->{pv_uuid} = 'wrong' }, qr/PV\/WWID/],
    ['in-flight bridge', 'thick', sub { $_[0]->{vg_tags} = 'pve-slt-bridge-v1' }, qr/bridge reservation/],
    ['LV drift', 'thick', sub { $_[0]->{drift} = 1 }, qr/changed during collection/],
    ['boot drift', 'thick', sub { $_[0]->{boot_drift} = 1 }, qr/config\/node\/boot changed/],
    ['collector failure', 'thick', sub { $_[0]->{fail} = 1 }, qr/injected probe error/],
    ['unclassified plugin object', 'thick', sub { push @{$_[0]->{rows}}, row('sltg-orphan','orphan','-wi-------','') }, qr/unclassified plugin-owned/],
    ['renamed DM UUID alias', 'thick', sub { push @{$_[0]->{dm}}, ['renamed','LVM-vguuidheaduuid',0] }, qr/renamed scoped DM/],
    ['missing DM UUID', 'thick', sub { push @{$_[0]->{dm}}, [dm_head($_[0]),'',0] }, qr/DM name\/UUID conflict/],
    ['open DM', 'thick', sub { push @{$_[0]->{dm}}, [dm_head($_[0]),'LVM-vguuidheaduuid',1] }, qr/open backing\/runtime/],
    ['unknown SLT mapper', 'thick', sub { push @{$_[0]->{dm}}, ['sltg-orphan','SLT-TG2-unknown',0] }, qr/unclassified or renamed/],
    ['duplicate DM UUID', 'thick', sub { push @{$_[0]->{dm}}, ['one','LVM-vguuidheaduuid',0], ['two','LVM-vguuidheaduuid',0] }, qr/duplicate DM UUID/],
    ['DM collector unavailable', 'thick', sub { $_[0]->{dm} = undef }, qr/DM inventory unavailable/],
    ['LAZY_ACTIVE', 'lazy', sub {
        my $f = $_[0]; $f->{state}->{phase} = 'LAZY_ACTIVE';
        @{$f->{state}}{qw(owner_node owner_boot owner_epoch)} = ('node-a',$f->{boot},'b' x 32);
        (grep { $_->{lv_name} eq $f->{anchor} } @{$f->{rows}})[0]->{lv_tags} = join(',', @{anchor_tags(%{$f->{state}})});
    }, qr/not unowned DORMANT/],
    ['dormant clone exists', 'lazy', sub { push @{$_[0]->{dm}}, ['other','SLT-TG6-CLONE-' . ('a' x 32),0] }, qr/DORMANT Lazy runtime exists/],
);
for my $case (@cases) {
    subtest $case->[0] => sub {
        my $f = fixture($case->[1]); $case->[2]->($f);
        refused(sub { collector($f)->plan([$f->{vol}]) }, $case->[3], $case->[0]);
    };
}

subtest 'materialized Thick exact zero-open DM binding' => sub {
    my $f = fixture('thick');
    (my $name = $f->{head}) =~ s/-/--/g;
    push @{$f->{dm}}, ["testvg-$name", 'LVM-vguuidheaduuid', 0];
    my $bundle = collector($f)->plan([$f->{vol}]);
    is(scalar(keys %{$bundle->{bindings}->{runtimes}}), 1, 'one exact DM binding');
    is((values %{$bundle->{bindings}->{runtimes}})[0]->{uuid}, 'LVM-vguuidheaduuid', 'actual kernel UUID');
};

subtest 'two-disk Thin root is losslessly bound once by executor adapter v2' => sub {
    my $f = fixture('thin');
    push @{$f->{rows}}, row('vm-101-disk-1','secondlv','Vwi---tz--','',pool_lv=>'sltp-101'),
        row('snap_vm-101-disk-1_b','secondsnap','Vri---tz--','',pool_lv=>'sltp-101',origin=>'vm-101-disk-1');
    my $c = collector($f);
    refused(sub { $c->plan([$f->{vol}]) }, qr/omits\/adds owned volume/, 'partial shared pool selection');
    my $bundle = $c->plan([$f->{vol}, 'store:vm-101-disk-1']);
    my $root = $bundle->{inventory}->{roots}->[0];
    is_deeply($root->{volume_ids}, ['store:vm-101-disk-0','store:vm-101-disk-1'], 'both heads in root');
    is(scalar(@{$root->{object_ids}}), 7, 'pool data meta two heads two snapshots');
    my %objects = map { $_->{id} => $_ } @{$bundle->{inventory}->{objects}};
    is($objects{'lv:snap_vm-101-disk-1_b'}->{volume_id}, 'store:vm-101-disk-1', 'snapshot belongs to exact head');
    is($objects{'lv:sltp-101'}->{volume_id}, '', 'pool structural ownership not invented');
    my $raw = "scsi0: $f->{vol}\nscsi1: store:vm-101-disk-1\n";
    my $adapted = PVE::SharedLvmThinVMDestroyPlanAdapter->adapt_plan(
        plan_version => 2, storage_plan => $bundle->{storage_plan}, raw_config => $raw,
        locked_config => {scsi0=>$f->{vol}, scsi1=>'store:vm-101-disk-1', digest=>sha1_hex($raw)},
        storecfg=>$f->{cfg}, bindings=>$bundle->{bindings}, expected_scope=>$bundle->{inventory}->{scope});
    is($adapted->{schema}, 'sharedlvmthin-vm-destroy-executor-plan/v2', 'normalized executor schema');
    $f->{rows} = [grep { $_->{lv_uuid} eq 'spareuuid' } @{$f->{rows}}];
    is($c->observe($bundle)->{result}, 'EXACT_PLANNED_STORAGE_ABSENT', 'shared pool absence and spare retention');
};

done_testing();
