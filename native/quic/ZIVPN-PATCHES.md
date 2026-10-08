# ZiVPN local QUIC patch

Base: apernet/quic-go `v0.40.1-0.20231112225043-e7f3af208dee` (commit `e7f3af208dee`). Runtime sources and the original MIT license are retained. Upstream integration tests and their static certificate fixtures are excluded from this snapshot.

The original `connection.SetCongestionControl` directly changed the sent-packet handler from the HTTP authentication goroutine while the connection loop could process ACKs. Race detection exposed simultaneous reads/writes during `TestClientServerHandshakeInfo`.

The setter now queues the latest requested controller under a dedicated mutex and schedules the connection loop. That loop drains pending changes between packet operations. Congestion algorithms, authentication and payload bandwidth ceilings remain the same. Multiple pending updates coalesce; applying the update is asynchronous.

`congestion_update_test.go` verifies concurrent setters, loop ownership, the last pending controller and single application. The server handshake integration test is also repeated under the race detector. Changes to this fork should remain small and documented until the older QUIC dependency is migrated.

## Initial packet size and explicit CUBIC selection

`Config.InitialPacketSize` optionally overrides the initial QUIC UDP payload size,
excluding wrapper overhead. Zero retains the IPv4/IPv6 defaults; explicit values
are validated from 1200 to 1452 bytes. Both client and server packet handlers and
MTU discoverers receive the value. Disable path MTU discovery to keep it fixed.
`initial_packet_size_test.go` checks defaults, validation and configuration copying;
the core integration test exercises an MTU-constrained Salamander path.

`Config.UseCubic` selects the upstream CUBIC algorithm already present in this
snapshot. The default remains Reno: the original `NewCubicSender` constructor
received `reno=true`. The new flag passes `reno=false` explicitly for CUBIC and
is preserved when copying configuration. Core integration tests exercise complete
Reno and CUBIC downloads on finite queues and slow links. Application-level
controller replacement remains serialized by the patch described above.

See `docs/BBR_FIX.md` in the repository root for configuration, telemetry limits
and synthetic regression results.
