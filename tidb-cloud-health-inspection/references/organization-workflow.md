# Organization Workflow

An organization run is lightweight cluster inventory and health screening, not a complete inspection of every cluster.

1. Paginate to explicit completion.
2. Verify organization ownership.
3. Keep active logical clusters and list non-active records separately.
4. Remove branch and resource-pool records from logical-cluster inventory.
5. Normalize canonical Plan and deduplicate cluster IDs.
6. Collect tenant-safe metadata, SQL QPS/latency, product-exposed RU/RCU and logical storage where available.
7. Exclude shared physical-pool metrics and topology from logical-cluster output.
8. Isolate failures as `UNREACHABLE` and continue.
9. Return coverage, exceptions, and drill-down targets.

Default organization screening does not run SQL Top-N or the full Dedicated/BYOC component catalog for every target.
