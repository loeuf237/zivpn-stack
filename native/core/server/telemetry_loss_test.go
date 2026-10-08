package server

import (
	"github.com/apernet/quic-go/logging"
	"testing"
)

func TestLateACKCountsDeliveryOnce(t *testing.T) {
	s := &TransportStats{}
	s.rememberLoss(logging.Encryption1RTT, 5, logging.PacketLossTimeThreshold)
	s.rememberLoss(logging.Encryption1RTT, 6, logging.PacketLossReorderingThreshold)
	s.rememberLoss(logging.EncryptionHandshake, 5, logging.PacketLossTimeThreshold)
	frames := []logging.Frame{&logging.AckFrame{AckRanges: []logging.AckRange{{Smallest: 5, Largest: 6}}}}
	s.observeACKs(frames)
	s.observeACKs(frames)
	v := s.View()
	if v.Lost != 3 || v.LostTime != 2 || v.LostReordering != 1 || v.LateAcked != 2 {
		t.Fatalf("bad loss accounting: %+v", v)
	}
}

func TestLateACKRingIsBounded(t *testing.T) {
	s := &TransportStats{}
	s.rememberLoss(logging.Encryption1RTT, 1, logging.PacketLossTimeThreshold)
	s.rememberLoss(logging.Encryption1RTT, 4097, logging.PacketLossTimeThreshold)
	s.observeACKs([]logging.Frame{&logging.AckFrame{AckRanges: []logging.AckRange{{Smallest: 0, Largest: 1}}}})
	if s.View().LateAcked != 0 {
		t.Fatal("evicted packet was miscounted")
	}
	s.observeACKs([]logging.Frame{&logging.AckFrame{AckRanges: []logging.AckRange{{Smallest: 0, Largest: 1 << 50}}}})
	if v := s.View(); v.LateAcked != 1 || v.LossTrackingEvicted != 1 {
		t.Fatalf("bad bounded tracking: %+v", v)
	}
}
