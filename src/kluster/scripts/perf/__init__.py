"""`perf`: the control planes' peaks that the cluster cannot see of itself, recorded outside it and imported.

A from-scratch `up` installs the monitoring component partway through, and a
rebuild replaces the worker its stores live on, so the first minutes of every
bring-up and rebuild, which carry the peak, reach no store in the cluster
(Aetf/kluster-ops#790 §4.5). The operator runs `perf record` around such a
run, and `perf import` once the history store is back:

-   `record` (`perf.record`) samples every control plane every few seconds
    through Talos, its pressure, disk statistics, load and memory, and etcd's
    metrics through the API server's node proxy once the API answers, into a
    file of Prometheus exposition, each series under the name and the `job`
    the cluster's own scrape gives it (`perf.procfs`, `perf.exposition`).
-   `import` (`perf.history`) writes that file into the history store where
    the store holds nothing of the same node and job, so the readings
    (`kluster.lib.monitoring.readings`) read a recorded period as they read a
    scraped one.

How it reaches the cluster is `perf.cluster`; the command line is `perf.cli`.
operations.md §8.5 is when it runs and what to check.
"""
