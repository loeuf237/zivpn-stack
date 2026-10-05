# ZiVPN local QUIC patch

Base: apernet/quic-go `v0.40.1-0.20231112225043-e7f3af208dee` (commit `e7f3af208dee`). Runtime sources and the original MIT license are retained. Upstream integration tests and their static certificate fixtures are excluded from this snapshot.

The original `connection.SetCongestionControl` directly changed the sent-packet handler from the HTTP authentication goroutine while the connection loop could process ACKs. Race detection exposed simultaneous reads/writes during `TestClientServerHandshakeInfo`.

The setter now queues the latest requested controller under a dedicated mutex and schedules the connection loop. That loop drains pending changes between packet operations. Congestion algorithms, authentication and payload bandwidth ceilings remain the same. Multiple pending updates coalesce; applying the update is asynchronous.

`congestion_update_test.go` verifies concurrent setters, loop ownership, the last pending controller and single application. The server handshake integration test is also repeated under the race detector. Changes to this fork should remain small and documented until the older QUIC dependency is migrated.
