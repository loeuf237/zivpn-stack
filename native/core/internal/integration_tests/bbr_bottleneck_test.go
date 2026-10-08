package integration_tests

import (
	"bytes"
	"io"
	"net"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/apernet/hysteria/core/client"
	"github.com/apernet/hysteria/core/internal/integration_tests/mocks"
	"github.com/apernet/hysteria/core/server"
	"github.com/stretchr/testify/mock"
	"github.com/stretchr/testify/require"
)

type bottleneckPacket struct {
	data []byte
	addr net.Addr
}

// The test link has a finite queue, a fixed transmission rate and propagation
// delay. It never changes the host interfaces or production listeners.
type bottleneckConn struct {
	net.PacketConn
	queue   chan bottleneckPacket
	done    chan struct{}
	once    sync.Once
	queued  sync.WaitGroup
	sent    atomic.Uint64
	dropped atomic.Uint64
}

func (c *bottleneckConn) WriteTo(b []byte, addr net.Addr) (int, error) {
	c.sent.Add(1)
	select {
	case <-c.done:
		return 0, net.ErrClosed
	default:
	}
	select {
	case c.queue <- bottleneckPacket{append([]byte(nil), b...), addr}:
	default:
		c.dropped.Add(1)
	}
	return len(b), nil
}
func (c *bottleneckConn) Close() error {
	c.once.Do(func() { close(c.done) })
	err := c.PacketConn.Close()
	c.queued.Wait()
	return err
}
func newBottleneckConn(pc net.PacketConn, rate int, queuePackets int) *bottleneckConn {
	c := &bottleneckConn{PacketConn: pc, queue: make(chan bottleneckPacket, queuePackets), done: make(chan struct{})}
	c.queued.Add(1)
	go func() {
		defer c.queued.Done()
		for {
			select {
			case <-c.done:
				return
			case p := <-c.queue:
				timer := time.NewTimer(time.Duration(len(p.data)) * time.Second / time.Duration(rate))
				select {
				case <-timer.C:
				case <-c.done:
					timer.Stop()
					return
				}
				c.queued.Add(1)
				go func(p bottleneckPacket) {
					defer c.queued.Done()
					timer := time.NewTimer(100 * time.Millisecond)
					select {
					case <-timer.C:
						_, _ = pc.WriteTo(p.data, p.addr)
					case <-c.done:
						timer.Stop()
					}
				}(p)
			}
		}
	}()
	return c
}

func TestBBRFiniteQueueDownload(t *testing.T) {
	runBBRDownload(t, 250000, 32, 196608)
}

func TestBBRSlowLinkDownload(t *testing.T) {
	runBBRDownload(t, 40000, 8, 49152)
}

func runBBRDownload(t *testing.T, rate, queuePackets, blocks int) {
	runAdaptiveDownload(t, rate, queuePackets, blocks, "bbr")
}

func TestRenoFiniteQueueDownload(t *testing.T) {
	runAdaptiveDownload(t, 250000, 32, 196608, "reno")
}

func TestRenoSlowLinkDownload(t *testing.T) {
	runAdaptiveDownload(t, 40000, 8, 49152, "reno")
}

func TestCubicFiniteQueueDownload(t *testing.T) {
	runAdaptiveDownload(t, 250000, 32, 196608, "cubic")
}

func TestCubicSlowLinkDownload(t *testing.T) {
	runAdaptiveDownload(t, 40000, 8, 49152, "cubic")
}

func runAdaptiveDownload(t *testing.T, rate, queuePackets, blocks int, controller string) {
	t.Helper()
	udp, err := net.ListenUDP("udp", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
	require.NoError(t, err)
	link := newBottleneckConn(udp, rate, queuePackets)
	auth := mocks.NewMockAuthenticator(t)
	auth.EXPECT().Authenticate(mock.Anything, mock.Anything, mock.Anything).Return(true, "bbr-fixture")
	srv, err := server.NewServer(&server.Config{Conn: link, TLSConfig: serverTLSConfig(), Authenticator: auth, IgnoreClientBandwidth: true, CongestionControl: controller})
	require.NoError(t, err)
	defer srv.Close()
	go srv.Serve()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	require.NoError(t, err)
	defer listener.Close()
	payload := bytes.Repeat([]byte("bbr-queue-fixture"), blocks)
	go func() {
		conn, err := listener.Accept()
		if err != nil {
			return
		}
		defer conn.Close()
		_, _ = conn.Write(payload)
	}()
	cli, _, err := client.NewClient(&client.Config{ServerAddr: udp.LocalAddr(), TLSConfig: client.TLSConfig{InsecureSkipVerify: true}})
	require.NoError(t, err)
	defer cli.Close()
	conn, err := cli.TCP(listener.Addr().String())
	require.NoError(t, err)
	defer conn.Close()
	require.NoError(t, conn.SetReadDeadline(time.Now().Add(50*time.Second)))
	start := time.Now()
	received, err := io.ReadAll(conn)
	require.NoError(t, err)
	require.Equal(t, payload, received)
	elapsed := time.Since(start)
	loss := 100 * float64(link.dropped.Load()) / float64(link.sent.Load())
	t.Logf("controller=%s bytes=%d elapsed=%.2fs useful_rate=%.0fB/s queue_drops=%d sent=%d drop_percent=%.2f", controller, len(received), elapsed.Seconds(), float64(len(received))/elapsed.Seconds(), link.dropped.Load(), link.sent.Load(), loss)
	if rate >= 65536 {
		require.Less(t, loss, 20.0, "BBR must not persistently overrun a finite bottleneck queue")
	}
	require.Greater(t, float64(len(received))/elapsed.Seconds(), float64(rate)*0.5, "BBR must retain useful link utilization")
}
