package bbr

import (
	"github.com/apernet/quic-go/congestion"
	"testing"
	"time"
)

// A 2x congestion window must not turn into a 2x sending rate.
func TestPacerUsesBBRPacingRate(t *testing.T) {
	for _, gain := range []float64{0.75, 1, 1.25} {
		b := NewBbrSender(DefaultClock{}, congestion.InitialPacketSizeIPv4)
		b.maxBandwidth.Update(Bandwidth(8000000), 1)
		b.congestionWindowGain = 2
		b.pacingRate = Bandwidth(8000000 * gain)
		want := congestion.ByteCount(1000000 * gain)
		if got := b.bandwidthForPacer(); got != want {
			t.Errorf("gain %.2f: pacer sends %d bytes/s, want %d bytes/s", gain, got, want)
		}
	}
}

// The remembered ACK height is the excess delivery, not the expected delivery.
func TestAckHeightTracksExcessBytes(t *testing.T) {
	tracker := newMaxAckHeightTracker(10)
	now := time.Unix(1, 0)
	tracker.Update(Bandwidth(8000000), false, 1, 10, 1, now, 20000)
	extra := tracker.Update(Bandwidth(8000000), false, 1, 20, 2, now.Add(10*time.Millisecond), 5000)
	if extra != 15000 {
		t.Fatalf("unexpected excess %d", extra)
	}
	if got := tracker.Get(); got != extra {
		t.Fatalf("stored ACK height %d, want excess %d", got, extra)
	}
}
