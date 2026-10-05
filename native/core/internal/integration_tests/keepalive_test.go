package integration_tests

import (
	"context"
	"crypto/tls"
	"net"
	"net/http"
	"testing"
	"time"

	"github.com/apernet/quic-go"
	"github.com/apernet/quic-go/http3"
	"github.com/stretchr/testify/mock"
	"github.com/stretchr/testify/require"

	"github.com/apernet/hysteria/core/internal/integration_tests/mocks"
	"github.com/apernet/hysteria/core/internal/protocol"
	"github.com/apernet/hysteria/core/server"
)

// A client with no keepalive models an idle Android tunnel behind a NAT.
// Server keepalive must sustain the negotiated tunnel without reconnecting.
func TestServerKeepsIdleClientConnected(t *testing.T) {
	for _, keepalive := range []bool{false, true} {
		name := "without_keepalive"
		if keepalive {
			name = "with_keepalive"
		}
		t.Run(name, func(t *testing.T) {
			udp, err := net.ListenUDP("udp", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
			require.NoError(t, err)
			auth := mocks.NewMockAuthenticator(t)
			auth.EXPECT().Authenticate(mock.Anything, "idle-fixture", mock.Anything).Return(true, "idle-user").Once()
			period := time.Duration(0)
			if keepalive {
				period = time.Second
			}
			srv, err := server.NewServer(&server.Config{
				Conn: udp, TLSConfig: serverTLSConfig(), Authenticator: auth,
				QUICConfig: server.QUICConfig{MaxIdleTimeout: 4 * time.Second, KeepAlivePeriod: period},
			})
			require.NoError(t, err)
			defer srv.Close()
			go srv.Serve()
			clientUDP, err := net.ListenUDP("udp", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
			require.NoError(t, err)
			defer clientUDP.Close()
			var connection quic.EarlyConnection
			transport := &http3.RoundTripper{
				TLSClientConfig: &tls.Config{InsecureSkipVerify: true},
				EnableDatagrams: true,
				QuicConfig:      &quic.Config{MaxIdleTimeout: 4 * time.Second, EnableDatagrams: true},
				Dial: func(ctx context.Context, _ string, tlsConfig *tls.Config, config *quic.Config) (quic.EarlyConnection, error) {
					var err error
					connection, err = quic.DialEarly(ctx, clientUDP, udp.LocalAddr(), tlsConfig, config)
					return connection, err
				},
			}
			defer transport.Close()
			request, err := http.NewRequest(http.MethodPost, "https://"+protocol.URLHost+protocol.URLPath, nil)
			require.NoError(t, err)
			protocol.AuthRequestToHeader(request.Header, protocol.AuthRequest{Auth: "idle-fixture"})
			response, err := transport.RoundTrip(request)
			require.NoError(t, err)
			require.Equal(t, protocol.StatusAuthOK, response.StatusCode)
			response.Body.Close()
			select {
			case <-connection.Context().Done():
				if keepalive {
					t.Fatalf("idle authenticated connection closed despite server keepalive: %v", context.Cause(connection.Context()))
				}
			case <-time.After(6 * time.Second):
				if !keepalive {
					t.Fatal("baseline did not reproduce idle closure")
				}
			}
		})
	}
}
