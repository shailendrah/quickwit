# Technical Design Document: Cloud-Native Data Pipeline on OKE

## 1. Problem Definition
**Objective:** Architect a scalable, distributed system to process large-scale document data and generate a searchable index using a cost-efficient, cloud-native approach.

### The Challenges
* **Scale:** Dataset volume exceeds single-machine processing capabilities, requiring distributed compute.
* **Coordination:** Orchestrating the hand-off between data transformation and indexing without manual intervention.
* **Persistence:** Ensuring data survives the lifecycle of ephemeral compute nodes (VMs).
* **Cost Efficiency:** Infrastructure must scale dynamically to match workload demand and minimize idle costs.

---

## 2. System Architecture
The solution leverages **KubeRay** on **Oracle Container Engine for Kubernetes (OKE)**. It utilizes a "Scatter-Gather" pattern to decouple data processing from indexing.

### Infrastructure Components
* **Compute:** OKE Managed Cluster with autoscaling Node Pools.
* **Orchestration:** KubeRay Operator managing a RayCluster (1 Head Node, N Worker Nodes).
* **Storage:** OCI Object Storage used as the Primary Data Lake and Final Index Sink.

---

## 3. The "Scatter-Gather" Pipeline

### Phase A: The Scatter (Data Transformation)
**Role:** Ray Workers (Stateless Tasks)
1.  **Ingestion:** Stream Parquet files from `bucket/raw-data/`.
2.  **Processing:** Transform tabular data into `ndjson` format.
3.  **Staging:** Compress files to `.zip` and upload to `bucket/processed-ndjson/`.
4.  **Communication:** Pass the file URI/Object path to the Indexer Actor.

### Phase B: The Gather (Indexing)
**Role:** Ray Indexer (Stateful Actor)
1.  **Trigger:** Receives notifications from Workers via Actor method calls.
2.  **Ingestion:** Pulls the specific `ndjson.zip` from staging.
3.  **Indexing:** Executes **Quickwit API** calls to process documents.
4.  **Finalization:** Writes Quickwit "splits" (index segments) to `bucket/quickwit-splits/`.

---

## 4. Workflow How-To

| Step | Layer | Action |
| :--- | :--- | :--- |
| **1. Provision** | **OCI/OKE** | Create an OKE cluster. Install KubeRay via Helm/Kubectl. |
| **2. Deploy** | **KubeRay** | Apply a `RayCluster` YAML defining the resource requirements for Head/Workers. |
| **3. Execute** | **Ray Core** | Submit the Python job. The Head Node distributes file lists to Workers. |
| **4. Process** | **Python** | Workers execute transformation logic using Pandas/PyArrow. |
| **5. Index** | **Quickwit** | The specialized Indexer Actor manages the search index state. |
| **6. Scale Down** | **OKE** | Once processing is finished, the Cluster Autoscaler terminates idle VMs. |

---

## 5. Key Advantages
* **Decoupled Logic:** Your application code focuses on data (Python/Ray), while K8s handles the "plumbing" (VMs/IPs).
* **No Custom Operator:** By using the existing KubeRay Operator, you avoid writing custom K8s management code.
* **Stateful Reliability:** Using a Ray Actor ensures that the indexing sequence is managed even as workers finish at different times.
* **High Performance:** OCI's high-bandwidth backbone between OKE and Object Storage minimizes I/O bottlenecks.
