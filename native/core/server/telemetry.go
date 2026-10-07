package server

import (
	"context"
	"errors"
	"sync"
	"sync/atomic"
	"time"

	"github.com/apernet/quic-go"
	"github.com/apernet/quic-go/logging"
)

// Only numeric transport metadata is collected, never frames or payloads.
type TransportStats struct {
	rtt   atomic.Int64
	sent  atomic.Uint64
	acked atomic.Uint64
	lost  atomic.Uint64
	pto   atomic.Uint32
}
type TransportView struct {
	RTTMS float64 `json:"rtt_ms"`
	Sent  uint64  `json:"sent_packets"`
	Acked uint64  `json:"acked_packets"`
	Lost  uint64  `json:"lost_packets"`
	PTO   uint32  `json:"pto_count"`
}

func (s *TransportStats) View() TransportView {
	if s == nil {
		return TransportView{}
	}
	return TransportView{float64(s.rtt.Load()) / float64(time.Millisecond), s.sent.Load(), s.acked.Load(), s.lost.Load(), s.pto.Load()}
}

type TransportRegistry struct{ entries sync.Map }

func (r *TransportRegistry) Lookup(ctx context.Context) *TransportStats {
	key := ctx.Value(quic.ConnectionTracingKey)
	if key == nil {
		return nil
	}
	if v, ok := r.entries.Load(key); ok {
		return v.(*TransportStats)
	}
	return nil
}
func (r *TransportRegistry) Tracer(ctx context.Context, _ logging.Perspective, _ quic.ConnectionID) *logging.ConnectionTracer {
	key := ctx.Value(quic.ConnectionTracingKey)
	if key == nil {
		return nil
	}
	s := &TransportStats{}
	r.entries.Store(key, s)
	return &logging.ConnectionTracer{
		UpdatedMetrics: func(stats *logging.RTTStats, _ logging.ByteCount, _ logging.ByteCount, _ int) {
			s.rtt.Store(int64(stats.SmoothedRTT()))
		},
		SentLongHeaderPacket: func(_ *logging.ExtendedHeader, _ logging.ByteCount, _ logging.ECN, _ *logging.AckFrame, _ []logging.Frame) {
			s.sent.Add(1)
		},
		SentShortHeaderPacket: func(_ *logging.ShortHeader, _ logging.ByteCount, _ logging.ECN, _ *logging.AckFrame, _ []logging.Frame) {
			s.sent.Add(1)
		},
		AcknowledgedPacket: func(_ logging.EncryptionLevel, _ logging.PacketNumber) { s.acked.Add(1) },
		LostPacket:         func(_ logging.EncryptionLevel, _ logging.PacketNumber, _ logging.PacketLossReason) { s.lost.Add(1) },
		UpdatedPTOCount:    func(n uint32) { s.pto.Store(n) },
		Close:              func() { r.entries.Delete(key) },
	}
}

// ClassifyClose uses typed QUIC errors, without storing error strings or credentials.
func ClassifyClose(err error) string {
	if err == nil {
		return "normal"
	}
	var idle *quic.IdleTimeoutError
	var handshake *quic.HandshakeTimeoutError
	var app *quic.ApplicationError
	var transport *quic.TransportError
	if errors.As(err, &idle) {
		return "idle_timeout"
	}
	if errors.As(err, &handshake) {
		return "handshake_timeout"
	}
	if errors.As(err, &app) {
		if app.Remote {
			return "peer_closed"
		}
		if app.ErrorCode == closeErrCodeTrafficLimitReached {
			return "access_revoked"
		}
		return "local_closed"
	}
	if errors.As(err, &transport) {
		return "transport_error"
	}
	return "other"
}
