package integration_tests

import (
	"bytes"
	"io"
	"net"
	"sync/atomic"
	"testing"
	"time"

	"github.com/apernet/hysteria/core/client"
	"github.com/apernet/hysteria/core/internal/integration_tests/mocks"
	"github.com/apernet/hysteria/core/server"
	"github.com/apernet/hysteria/extras/obfs"
	"github.com/stretchr/testify/mock"
	"github.com/stretchr/testify/require"
)

// An asymmetric IPv4 path with MTU 1280 admits at most 1252 UDP payload
// bytes. Salamander's eight bytes leave 1244 bytes for QUIC. Handshakes
// are admitted to isolate the behavior of the authenticated data path.
type mtuPathConn struct {
	net.PacketConn
	armed      atomic.Bool
	dropped    atomic.Uint64
	maxPayload atomic.Int64
}

func (c *mtuPathConn) WriteTo(p []byte, addr net.Addr) (int, error) {
	if c.armed.Load() {
		for old := c.maxPayload.Load(); int64(len(p)) > old; old = c.maxPayload.Load() {
			if c.maxPayload.CompareAndSwap(old, int64(len(p))) {
				break
			}
		}
		if len(p) > 1252 {
			c.dropped.Add(1)
			return len(p), nil
		}
	}
	return c.PacketConn.WriteTo(p, addr)
}

type fixtureObfsFactory struct{}

func (fixtureObfsFactory) New(net.Addr) (net.PacketConn, error) {
	pc, err := net.ListenPacket("udp", "127.0.0.1:0")
	if err != nil {
		return nil, err
	}
	o, err := obfs.NewSalamanderObfuscator([]byte("mtu-fixture"))
	if err != nil {
		pc.Close()
		return nil, err
	}
	return obfs.WrapPacketConn(pc, o), nil
}

func TestSalamanderConstrainedMTUDownload(t *testing.T) {
	for _, size := range []uint16{0, 1200} {
		name := "address_family_default"
		if size != 0 {
			name = "quic_1200"
		}
		t.Run(name, func(t *testing.T) {
			udp, err := net.ListenPacket("udp", "127.0.0.1:0")
			require.NoError(t, err)
			path := &mtuPathConn{PacketConn: udp}
			o, err := obfs.NewSalamanderObfuscator([]byte("mtu-fixture"))
			require.NoError(t, err)
			auth := mocks.NewMockAuthenticator(t)
			auth.EXPECT().Authenticate(mock.Anything, mock.Anything, mock.Anything).Return(true, "mtu-fixture")
			srv, err := server.NewServer(&server.Config{
				Conn: obfs.WrapPacketConn(path, o), TLSConfig: serverTLSConfig(), Authenticator: auth,
				IgnoreClientBandwidth: true,
				QUICConfig:            server.QUICConfig{InitialPacketSize: size, DisablePathMTUDiscovery: true},
			})
			require.NoError(t, err)
			defer srv.Close()
			go srv.Serve()
			listener, err := net.Listen("tcp", "127.0.0.1:0")
			require.NoError(t, err)
			defer listener.Close()
			payload := bytes.Repeat([]byte("mtu-data-fixture"), 8192)
			go func() {
				conn, err := listener.Accept()
				if err != nil {
					return
				}
				defer conn.Close()
				_, _ = conn.Write(payload)
			}()
			cli, _, err := client.NewClient(&client.Config{ServerAddr: udp.LocalAddr(), ConnFactory: fixtureObfsFactory{}, TLSConfig: client.TLSConfig{InsecureSkipVerify: true}})
			require.NoError(t, err)
			defer cli.Close()
			path.armed.Store(true)
			conn, err := cli.TCP(listener.Addr().String())
			require.NoError(t, err)
			defer conn.Close()
			require.NoError(t, conn.SetReadDeadline(time.Now().Add(2*time.Second)))
			data, readErr := io.ReadAll(conn)
			t.Logf("quic_size=%d wire_max=%d dropped=%d received=%d error=%v", size, path.maxPayload.Load(), path.dropped.Load(), len(data), readErr)
			if size == 0 {
				require.Error(t, readErr)
				require.Greater(t, path.dropped.Load(), uint64(0))
			} else {
				require.NoError(t, readErr)
				require.Equal(t, payload, data)
				require.Equal(t, uint64(0), path.dropped.Load())
				require.LessOrEqual(t, path.maxPayload.Load(), int64(1208))
			}
		})
	}
}
