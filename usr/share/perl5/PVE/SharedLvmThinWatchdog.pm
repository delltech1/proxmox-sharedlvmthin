# Copyright (C) 2026 BASTRIX Project Contributors
# SPDX-License-Identifier: GPL-3.0-only

package PVE::SharedLvmThinWatchdog;

use strict;
use warnings;
use IO::Socket::UNIX;
use Socket qw(SOCK_STREAM);

sub new {
    my ($class, %args) = @_;
    my $self = {
        state => 'DISARMED',
        socket => undef,
        socket_factory => $args{socket_factory},
        allow_real_watchdog => $args{allow_real_watchdog} ? 1 : 0,
        socket_path => $args{socket_path} // '/run/watchdog-mux.sock',
    };
    return bless($self, $class);
}

sub state { return $_[0]->{state}; }

sub _connect {
    my ($self) = @_;
    if (defined($self->{socket_factory})) {
        my $socket = $self->{socket_factory}->();
        die "watchdog socket factory returned no socket\n" if !defined($socket);
        return $socket;
    }
    die "real watchdog arming is disabled\n" if !$self->{allow_real_watchdog};
    die "refusing non-canonical real watchdog socket path\n"
        if $self->{socket_path} ne '/run/watchdog-mux.sock';
    return IO::Socket::UNIX->new(
        Type => SOCK_STREAM(),
        Peer => $self->{socket_path},
    ) || die "unable to open PVE watchdog-mux socket: $!\n";
}

sub _write_exact {
    my ($self, $payload) = @_;
    my $socket = $self->{socket};
    die "watchdog socket is not open\n" if !defined($socket);
    my $written = $socket->syswrite($payload, length($payload));
    die "watchdog write failed: $!\n" if !defined($written);
    die "watchdog short write: $written bytes\n" if $written != length($payload);
    return 1;
}

sub arm {
    my ($self, $decision) = @_;
    die "watchdog cannot arm from $self->{state}\n" if $self->{state} ne 'DISARMED';
    die "watchdog admission decision is unavailable\n" if ref($decision) ne 'HASH';
    die "watchdog admission is not positively safe\n"
        if !$decision->{safe} || ($decision->{action} // '') ne 'ACTIVATE_EXCLUSIVE';

    $self->{state} = 'ARMING';
    eval {
        $self->{socket} = $self->_connect();
        $self->_write_exact("\0");
    };
    if (my $error = $@) {
        # Never send magic close after an uncertain partial arm. Closing the
        # socket without V makes watchdog-mux fail closed if it accepted us.
        eval { $self->{socket}->close() if defined($self->{socket}); };
        $self->{socket} = undef;
        $self->{state} = 'FENCING';
        die $error;
    }
    $self->{state} = 'ARMED';
    return 1;
}

sub refresh {
    my ($self, $decision) = @_;
    return 0 if $self->{state} ne 'ARMED';
    if (ref($decision) ne 'HASH'
        || !$decision->{safe}
        || ($decision->{action} // '') ne 'REFRESH_WATCHDOG') {
        # Keep the client registered but stop refreshing. The PVE multiplexer
        # will expire it. A later healthy sample cannot resurrect this epoch.
        $self->{state} = 'FENCING';
        return 0;
    }
    eval { $self->_write_exact("\0"); };
    if ($@) {
        $self->{state} = 'FENCING';
        return 0;
    }
    return 1;
}

sub clean_disarm {
    my ($self, %proof) = @_;
    die "watchdog cannot cleanly disarm from $self->{state}\n"
        if $self->{state} ne 'ARMED';
    for my $field (qw(all_qemu_absent all_mappers_absent owner_epochs_released)) {
        die "clean watchdog disarm requires positive $field proof\n"
            if !defined($proof{$field}) || $proof{$field} ne '1';
    }
    $self->_write_exact('V');
    die "watchdog socket close failed: $!\n" if !$self->{socket}->close();
    $self->{socket} = undef;
    $self->{state} = 'DISARMED';
    return 1;
}

sub abandon_for_fencing {
    my ($self) = @_;
    die "watchdog is not in fencing state\n" if $self->{state} ne 'FENCING';
    # Deliberately close without V. watchdog-mux treats this as a failed
    # client and stops hardware-watchdog updates. This method is never called
    # by tests against the real socket.
    die "watchdog socket close failed: $!\n" if !$self->{socket}->close();
    $self->{socket} = undef;
    $self->{state} = 'FENCE_TRIGGERED';
    return 1;
}

sub DESTROY {
    my ($self) = @_;
    return if !defined($self->{socket});
    # Never magic-close implicitly. An unexpected guardian exit while armed
    # must remain fail-closed and let watchdog-mux fence the host.
    eval { $self->{socket}->close(); };
    $self->{socket} = undef;
}

1;
