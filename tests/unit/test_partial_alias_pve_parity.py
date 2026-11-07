"""Optional real PVE parser/plugin parity, with synthetic config only."""
import json
import os
import pathlib
import subprocess
import unittest

from test_thick_registration_preview import HELPER, PLANNER, PreviewTests


@unittest.skipUnless(os.environ.get('SLT_REAL_PVE_PARITY') == '1', 'requires installed PVE API')
class PartialAliasPveParityTests(unittest.TestCase):
    def test_generated_candidate_passes_real_pve_alias_guard(self):
        fixture = PreviewTests()
        report, raw = fixture.partial()
        proposal = HELPER.build_plan(report, 'labvg', None, 'missing-lazy', 0,
                                     PLANNER, raw, resume_existing='present-eager')
        old = HELPER.normalize_pve_config(raw['pve_parsed_config'][0])
        new = proposal['api_parameters']
        source = pathlib.Path(__file__).resolve().parents[2] / 'usr/share/perl5'
        perl = r'''
            use strict; use warnings; use JSON::PP; use PVE::Storage;
            use PVE::Storage::Custom::SharedLvmThinPlugin;
            my $line = do { local $/; <STDIN> }; my $v = decode_json($line);
            my $class = 'PVE::Storage::Custom::SharedLvmThinPlugin';
            my $loaded = $INC{'PVE/Storage/Custom/SharedLvmThinPlugin.pm'};
            die "wrong plugin source" unless index($loaded, $ARGV[0]) == 0;
            my $old = $v->{old}; my $new = $v->{new};
            delete $old->{storage}; delete $new->{storage};
            my $cfg = { ids => { old => $old, new => $new } };
            $class->_require_thick_identity_config('new', $new);
            $class->_verify_same_vg_alias_configuration('old', $old, $cfg);
            for my $field (qw(slt-lock-timeout slt-tg-region-size-kib slt-expected-min-paths)) {
                my %changed = %$new; $changed{$field} = ($old->{$field} //
                    ($field eq 'slt-tg-region-size-kib' ? 1024 : 30)) + 1;
                my $bad = {ids => {old => $old, new => \%changed}};
                die "mismatch accepted: $field" if eval { $class->_verify_same_vg_alias_configuration('old', $old, $bad); 1 };
            }
            print "PVE_ALIAS_PARITY_PASS\n";
        '''
        result = subprocess.run(['/usr/bin/perl', '-I' + str(source), '-I/usr/share/perl5',
                                 '-e', perl, str(source)],
                                input=json.dumps({'old': old, 'new': new}), text=True,
                                capture_output=True, timeout=30, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('PVE_ALIAS_PARITY_PASS', result.stdout)


if __name__ == '__main__':
    unittest.main()
