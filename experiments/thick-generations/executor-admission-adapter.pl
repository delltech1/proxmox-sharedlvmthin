#!/usr/bin/perl

BEGIN {
    for my $name (qw(PERL5OPT PERL5LIB PERLLIB PERL_USE_UNSAFE_INC)) {
        die "unsafe Perl environment variable $name is set\n"
            if defined($ENV{$name}) && length($ENV{$name});
    }
}

use strict;
use warnings;

use B qw(SVp_IOK SVp_NOK svref_2object);
use Digest::SHA qw(sha256_hex);
use Fcntl qw(O_NOFOLLOW O_RDONLY);
use Getopt::Long qw(GetOptions);
use JSON::PP ();

my ($module, $module_sha256, $action);
GetOptions(
    'module=s' => \$module,
    'module-sha256=s' => \$module_sha256,
    'action=s' => \$action,
) or die "invalid adapter arguments\n";

die "absolute admission module path is required\n" if !defined($module);
my ($untainted_module) =
    $module =~ m{\A(/[A-Za-z0-9_.+@%:,=-]+(?:/[A-Za-z0-9_.+@%:,=-]+)*)\z};
die "absolute admission module path is required\n"
    if !defined($untainted_module) || $untainted_module =~ m{/\.\.?/};
$module = $untainted_module;
die "invalid admission module digest\n"
    if ($module_sha256 // '') !~ /\A[a-f0-9]{64}\z/;
die "invalid adapter action\n"
    if ($action // '') !~ /\A(?:validate|reserve|bind|dispatch|finish|close)\z/;

my @module_lstat = lstat($module);
die "admission module path is unavailable\n" if !@module_lstat;
die "admission module path must not be a symlink\n" if -l _;
sysopen(my $module_fh, $module, O_RDONLY | O_NOFOLLOW)
    or die "cannot open admission module: $!\n";
binmode($module_fh);
local $/;
my $module_bytes = <$module_fh>;
close($module_fh) or die "cannot close admission module: $!\n";
die "admission module digest mismatch\n"
    if sha256_hex($module_bytes) ne $module_sha256;
$module_bytes =~ /\A(.*)\z/s or die "cannot untaint verified admission module bytes\n";
$module_bytes = $1;

# Execute the exact bytes that were hashed.  Reopening the pathname with
# require would create a digest-to-execution TOCTOU window.
my $quoted_module = $module;
$quoted_module =~ s/([\\"])/\\$1/g;
my $loaded = eval qq{#line 1 "$quoted_module"\n} . $module_bytes;
die "verified admission module bytes failed to load: $@" if $@;
die "verified admission module bytes did not return true\n" if !$loaded;

my $stdin = <STDIN>;
die "adapter input is missing\n" if !defined($stdin);
my $json = JSON::PP->new->utf8->canonical->allow_nonref(0);
my $input = eval { $json->decode($stdin) };
die "adapter input is invalid JSON: $@" if $@;
die "adapter input must be an object\n" if ref($input) ne 'HASH';
die "adapter input is not the exact canonical transport encoding\n"
    if $stdin ne $json->encode($input) . "\n";

my %allowed_input = (
    validate => [qw(record)],
    reserve => [qw(requested existing continuity_proven attempt_fresh_proven)],
    bind => [qw(record claim)],
    dispatch => [qw(persisted claim binding_persisted)],
    finish => [qw(record evidence)],
    close => [qw(current expected)],
);
my %allowed = map { $_ => 1 } @{$allowed_input{$action}};
for my $key (keys %$input) {
    die "unknown adapter input field '$key'\n" if !$allowed{$key};
}

# JSON booleans are blessed references.  The admission contract deliberately
# accepts only scalar numeric 0/1 proof values, never general truthiness.
sub reject_boolean_references {
    my ($value, $path) = @_;
    if (ref($value) eq 'HASH') {
        reject_boolean_references($value->{$_}, "$path.$_") for keys %$value;
    } elsif (ref($value) eq 'ARRAY') {
        reject_boolean_references($value->[$_], "$path\[$_\]") for 0 .. $#$value;
    } elsif (ref($value)) {
        die "adapter input $path contains a reference-valued scalar\n";
    }
}
reject_boolean_references($input, 'root');

sub exact_json_integer {
    my ($value) = @_;
    return 0 if ref($value);
    my $flags = svref_2object(\$value)->FLAGS;
    return ($flags & SVp_IOK) && !($flags & SVp_NOK);
}

sub require_numeric_bit {
    my ($value, $path) = @_;
    die "adapter input $path must be the JSON integer 0 or 1\n"
        if !exact_json_integer($value) || ($value != 0 && $value != 1);
}

sub require_lab_record {
    my ($record, $path, $nullable) = @_;
    return if $nullable && !defined($record);
    die "adapter input $path must be a lab executor record\n"
        if ref($record) ne 'HASH' || ($record->{kind} // '') ne 'THICK_EXECUTOR_LAB';
    die "adapter input $path.schema must be the JSON integer 1\n"
        if !exact_json_integer($record->{schema}) || $record->{schema} != 1;
}

if ($action eq 'validate') {
    require_lab_record($input->{record}, 'record', 0);
} elsif ($action eq 'reserve') {
    require_lab_record($input->{requested}, 'requested', 0);
    require_lab_record($input->{existing}, 'existing', 1) if exists($input->{existing});
    require_numeric_bit($input->{continuity_proven}, 'continuity_proven');
    require_numeric_bit($input->{attempt_fresh_proven}, 'attempt_fresh_proven');
} elsif ($action eq 'bind') {
    require_lab_record($input->{record}, 'record', 0);
    require_lab_record($input->{claim}->{identity}, 'claim.identity', 0)
        if ref($input->{claim}) eq 'HASH';
    require_numeric_bit($input->{claim}->{startup_proven}, 'claim.startup_proven')
        if ref($input->{claim}) eq 'HASH';
} elsif ($action eq 'dispatch') {
    require_lab_record($input->{persisted}, 'persisted', 0);
    require_lab_record($input->{claim}->{identity}, 'claim.identity', 0)
        if ref($input->{claim}) eq 'HASH';
    require_numeric_bit($input->{binding_persisted}, 'binding_persisted');
} elsif ($action eq 'finish') {
    require_lab_record($input->{record}, 'record', 0);
    require_lab_record($input->{evidence}->{identity}, 'evidence.identity', 0)
        if ref($input->{evidence}) eq 'HASH';
    for my $field (qw(cgroup_terminal pending_jobs_absent io_terminal storage_postcondition_proven)) {
        require_numeric_bit($input->{evidence}->{$field}, "evidence.$field")
            if ref($input->{evidence}) eq 'HASH';
    }
} elsif ($action eq 'close') {
    require_lab_record($input->{current}, 'current', 0);
    require_lab_record($input->{expected}, 'expected', 0);
}

my %dispatch = (
    validate => \&PVE::SharedLvmAdmission::validate_executor_record,
    reserve => \&PVE::SharedLvmAdmission::evaluate_executor_reserve,
    bind => \&PVE::SharedLvmAdmission::evaluate_executor_bind,
    dispatch => \&PVE::SharedLvmAdmission::evaluate_executor_dispatch,
    finish => \&PVE::SharedLvmAdmission::evaluate_executor_finish,
    close => \&PVE::SharedLvmAdmission::evaluate_executor_close,
);
my $result = eval {
    $action eq 'validate'
        ? ($dispatch{$action}->($input->{record}), {allowed => 1, action => 'VALID_LAB_RECORD'})[1]
        : $dispatch{$action}->(%$input)
};
die "admission model evaluation failed: $@" if $@;
die "admission model returned a non-object\n" if ref($result) ne 'HASH';
print $json->encode($result), "\n";
