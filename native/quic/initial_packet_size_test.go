package quic

import (
	"net"
	"testing"
)

func TestConfiguredInitialPacketSize(t *testing.T) {
	for _, tc := range []struct {
		ip         net.IP
		configured uint16
		want       int64
	}{
		{net.IPv4(127, 0, 0, 1), 0, 1252},
		{net.ParseIP("::1"), 0, 1232},
		{net.IPv4(127, 0, 0, 1), 1200, 1200},
		{net.ParseIP("::1"), 1200, 1200},
	} {
		config := &Config{InitialPacketSize: tc.configured}
		if err := validateConfig(config); err != nil {
			t.Fatal(err)
		}
		filled := populateConfig(config)
		if got := getInitialPacketSize(&net.UDPAddr{IP: tc.ip}, filled); int64(got) != tc.want {
			t.Errorf("configured=%d ip=%s: got %d, want %d", tc.configured, tc.ip, got, tc.want)
		}
	}
	for _, size := range []uint16{1199, 1453} {
		if err := validateConfig(&Config{InitialPacketSize: size}); err == nil {
			t.Errorf("invalid size %d accepted", size)
		}
	}
}
