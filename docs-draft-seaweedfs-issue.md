# DRAFT (not posted): lock ring keeps a filer that other filers cannot reach (no filer-to-filer health)

SeaweedFS 4.51, S3 gateways running as `weed s3 -filer=...` next to several filers, masters in Raft, filers with local leveldb2 stores
and mutual TLS, boxes behind a provider network where reachability is not symmetric.

Observed
- Every S3 PutObject / CompleteMultipartUpload takes the distributed object-write lock (`s3api_server.go`, `NewShortLivedLock`;
  there is no switch to turn it off when filers are configured). The lock owner is chosen by the lock ring.
- The ring is fed by the masters: `KeepConnected` adds a filer to the lock ring on connect and removes it only when its stream to the
  masters ends (`master_grpc_server.go`). Detection of a dead peer is the gRPC keepalive (60 s + 20 s constants).
- With a partial partition (box B reachable from the masters and from some filers, but not from filer A), B stays in the ring.
  Writes handled by A whose lock key hashes to B fail after several 60 s dial timeouts:
  `completeMultipartUpload /buckets/x/k error: ... dial tcp B:grpc: i/o timeout` then `withObjectWriteLock: failed to acquire lock`.
  The client sees `upload failed` (no data loss), about 1 in N writes through A (N filers) until the partition heals; the ring never
  drops B.

Expected
- A filer that another filer cannot reach for the lock RPC should be skipped (route to the next ring owner, or a short negative cache
  as `unreachableOwners` already does for reads), or the ring membership should consider filer-to-filer reachability.

Suggestions
- Reuse the `unreachableOwners` idea for the lock path: after a transport error to the owner, pick the next owner for a few seconds.
- A configurable (flag) lock acquisition timeout instead of the compiled constants, and a flag to disable the object write lock for
  setups that do not need cross-filer conditional writes.
