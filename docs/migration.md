# Migration Plan - Legacy to Next Gen Cluster

Objective: Migrate applications and data from the legacy cluster (`~/projects/kluster-code`) to the new cluster, minimizing downtime for critical services like `hath` and ensuring data integrity.

## 1. General Migration Strategy

The migration will follow a stop-copy-start approach for each application to ensure data consistency:
1.  **Scale down** the application in the legacy cluster to stop writes.
2.  **Migrate the data** (PVCs, S3 objects).
3.  **Deploy** the application in the new cluster pointing to the new storage.
4.  **Verify** and update DNS/Ingress.

## 2. Data Migration Details

### 2.1 Local Storage PVCs
Some legacy applications use local path storage (e.g., early `hath` implementation).
-   **Strategy**: Copy data from the old node's local path to the new storage system (likely a different storage class or JuiceFS in the new cluster).
-   **Tools**: `rsync`, `tar` over SSH, or a backup/restore tool like Velero.

### 2.2 JuiceFS / Object Storage (S3)
If the user decides to move the S3 bucket to a different region (as noted in legacy README), this will be the largest data migration task.
-   **Strategy**: Stop all services using JuiceFS to ensure consistency. Copy objects from the source S3 bucket to the target S3 bucket in the new region.
-   **Tools**: `aws s3 sync` or GCP equivalent if moving to Google Cloud Storage.
-   **Downtime**: This operation will cause significant downtime for all services depending on JuiceFS, proportional to the volume of data.

## 3. Application-Specific Migration Plans

### 3.1 HatH (Hentai@Home)
-   **Priority**: High. Downtime must be limited to a few hours.
-   **Storage**: Legacy code shows usage of Local Storage PVC (50Gi). If it has been moved to JuiceFS, refer to the JuiceFS migration strategy.
-   **Migration Steps**:
    1.  Identify current storage location (Local PVC host path or JuiceFS).
    2.  Prepare deployment in the new cluster (using the new Pulumi framework).
    3.  Stop `hath` in the legacy cluster.
    4.  Copy the 50Gi data to the new storage location. This should take less than an hour on a gigabit link.
    5.  Start `hath` in the new cluster.
    6.  Verify connection and operation.

### 3.2 Other Services
Other services (Authelia, Nextcloud, Syncthing, etc.) will follow the general migration strategy. Data stored in database (PostgreSQL/MariaDB) will need to be dumped and restored or migrated using replication if supported.

## 4. Rollback Plan
In case of failure during migration:
1.  Abandon the migration of the specific application.
2.  Scale up the deployment in the legacy cluster to resume service.
3.  Investigate and resolve the issue before attempting again.
