# Fleet Protocol Specification

This document outlines the registration, heartbeat, memory replication, and policy push protocols for the fleet endpoint persona.

* Registration (`POST /v1/fleet/register`) – see plan for request/response details.
* Heartbeat (`fleet.heartbeat` frames) – emitted every 15 seconds.
* Memory push/pull – hybrid approach with monotonic sequences.
* Policy push – fetched on heartbeat ack if version changes.
