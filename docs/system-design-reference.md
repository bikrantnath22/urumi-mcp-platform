# Urumi: detailed system reference

Read the [architecture and tradeoffs](system-design.md) first. This reference describes the implementation reviewed on 26 October 2026. Paths are relative to the repository root. Runtime explanations are followed by HTTP routes, database fields, tools and a complete source index. The test map records live checks and fixture tests separately.

The index covers all 94 gateway modules, 39 dashboard source files, 55 chart files, 17 gateway tables and 17 migrations present at review time. Sections A–G describe behavior, H–J describe interfaces/data/tools, K–P locate the implementation, and Q–S cover verification, tradeoffs and chart configuration.

## A. Complete runtime map

| Component | Namespace / location | Interface and job | State / availability |
|---|---|---|---|
| Browser | User device | React workspace, chat, management, owner setup launch | Session cookies; temporary account-scoped recovery IDs; no upstream secrets |
| External MCP client | User device | One `/mcp` URL and personal Bearer API key | Client controls its local tool-list display; server authorization remains fresh |
| Traefik | k3s infrastructure, normally `kube-system` | Public HTTP host/path routing; managed-store ForwardAuth | Controller infrastructure, not dashboard Nginx |
| Dashboard | `platform` | Nginx serves Vite's React assets on 8080 | One Deployment replica; no application database inside the container |
| Gateway | `platform` | FastAPI on 8000, browser REST, inbound MCP, outbound MCP, Groq loop | PostgreSQL plus process-local caches and HTTP counters |
| Gateway migrate init container | Gateway pod | `alembic upgrade head` before application startup | Shares the gateway image and its migration history |
| PostgreSQL | `platform` | One server, separate `gateway` and `notes` databases/roles | One StatefulSet/PVC; headless Service; not a replicated cluster |
| Notes MCP | `platform` | Custom Streamable HTTP MCP on 8000; shared team notes | Separate Notes database/role on the PostgreSQL instance |
| Store provisioner | `platform` | Private worker; PostgreSQL scheduling, Helm, Kubernetes and service verification | One Deployment; no public Service; projected Kubernetes API token |
| Original demo store | `store` | `store.localtest.me`, WordPress, MySQL, Woo MCP and analytics | Separate original installation; not automatically migrated into a member's managed store |
| Managed store | `urumi-store-<UUID hex>` | Same store chart, unique host, identities, credentials and volumes | One namespace for each store; no shared WordPress/MySQL database |
| WordPress/WooCommerce | Each store namespace | Apache on 80, public storefront/admin; REST for MCP | MySQL and WordPress-content PVC; one Recreate Deployment replica |
| MySQL | Each store namespace | SQL on 3306 for that store | One StatefulSet, ordinary ClusterIP Service, data PVC |
| Woo MCP | Each store namespace | Custom Python MCP on 8000, product/order/media REST adapter | Read/write Woo credential and separate media application password |
| Analytics MCP | Each store namespace | Custom Python MCP on 8000, bounded sales/order reads | Separate read-only Woo credential |
| Bootstrap Job | Each store namespace | WP-CLI, scripts and PHP helpers; initial configuration and seed data | Terminal Job completion, not a permanently running service |
| CoreDNS | Kubernetes infrastructure | Service DNS resolution | Required permitted DNS egress; exact deployed selectors are cluster configuration |
| Local-path provisioner | Kubernetes infrastructure | Dynamically creates/reclaims local storage | Observed version 0.0.30; node-local durability, not replicated storage |
| Kubernetes API/controllers | Cluster control plane | Workloads, endpoints, RBAC, admission, policies, Secrets and PV lifecycle | Cluster state; not a replacement for application backups |
| Groq | External HTTPS API | Model inference for the gateway's chat loop | `GROQ_API_KEY`; provider outage/quota is separate from application budgets |
| Tavily / other remote MCPs | Operator-connected external services | Additional tools through the same gateway | Generic URL registry; no locally written Tavily server is claimed |

The README's local setup maps host ports 80/443 through k3d's load-balancer container. Traefik then selects the host/path route. Kubernetes Services select ready endpoints; whether Traefik uses native Service balancing or pod endpoints is controller configuration. There is no cloud load balancer or service mesh in this deployment. One backend replica does not provide application redundancy.

### Network paths and public exposure

| Caller → destination | Purpose | Boundary |
|---|---|---|
| Browser → Traefik → dashboard | UI assets | Public app host, `/` |
| Browser/client → Traefik → gateway | REST and MCP | Public app host, `/api` and `/mcp`; application auth |
| Shopper → Traefik → WordPress | Browse, cart, COD checkout, WP login | Public store host; managed stores pass the access gate |
| Traefik → gateway | Managed-store access decision | ForwardAuth; store-state check, not shopper login |
| Gateway → Notes/Woo/analytics | User business tool execution | Exact approved destinations plus live tool authorization |
| Gateway → external MCP/Groq | External tools/model | Restricted destination validation and permitted HTTPS egress |
| Gateway/worker → PostgreSQL | Identity, state, queue, audits | Dedicated database role/credential; internal service |
| Notes → PostgreSQL | Shared notes | Separate logical DB and role |
| Woo/analytics → own WordPress | Business REST | Namespace/pod/port policies; separate credentials |
| WordPress/bootstrap → own MySQL | Application/bootstrap SQL | Same store namespace, fixed service and credential |
| Worker → Kubernetes API | Provision/observe/delete namespace; observe PVs | Scoped SA, fixed routes, RBAC/admission, verified TLS |
| Worker → owned WP/Woo/analytics | Readiness and private discovery | Explicit provisioning-only exception; not a member bypass |
| WordPress setup plugin → gateway | Redeem one-use owner capability | Store/session-bound token; API-route reachability alone grants nothing |

Default-deny policies are combined with explicit DNS and necessary ingress/egress rules. Policies use namespaces, pod labels and ports; they do not enforce HTTP route names. A compatible enforcing network implementation is required. Rendered policy checks are not evidence of every live packet being denied.

## B. Data ownership and credentials

State is stored in four places:

1. **Gateway PostgreSQL:** users, grants, API-key hashes, server definitions, audits, chats, quotas, stores, jobs and resource identity records.
2. **Business data:** MySQL per store holds WordPress/WooCommerce data; its content PVC holds uploads/plugins/themes. Notes uses its separate PostgreSQL database.
3. **Kubernetes state:** manifests, UIDs, RBAC, policies, Secrets, Jobs and Helm release records. A resource name alone is not ownership evidence.
4. **Ephemeral/client state:** process caches, HTTP counters, temporary Helm files, browser pending-request identifiers and static asset cache. Losing these must not erase the durable operation record.

| Secret/reference | Used by | Meaning |
|---|---|---|
| `postgres-admin` | PostgreSQL initialization | Initial database administrative credential |
| `gateway-db-credentials` | Gateway and provisioner | Gateway database role password |
| `notes-db-credentials` | Notes and DB initialization | Notes-only database credential |
| `gateway-auth-secrets` | Gateway | JWT signing key, Fernet key, bootstrap admin configuration |
| `gateway-llm-secrets` | Gateway | Groq API credential |
| `store-secrets` in each store | Selected store workloads | Independent credentials listed below; managed Secret is immutable |
| Projected worker SA token + CA | Provisioner/Helm kubeconfig | Kubernetes authentication and verified server trust; token is read fresh/referenced by file |
| TLS Secret reference in production values | Traefik default TLSStore | App/original store/managed wildcard certificate; private key is not copied to tenants |
| Optional existing image-pull Secrets | ServiceAccounts in their own namespace | Registry authentication, separate from application credentials |

`store-secrets` keys: `DB_ROOT_PASSWORD`, `DB_PASSWORD`, `WP_ADMIN_USER`, `WP_ADMIN_PASSWORD`, `WP_ADMIN_EMAIL`, `WC_RW_KEY`, `WC_RW_SECRET`, `WC_RO_KEY`, `WC_RO_SECRET`, `WP_MEDIA_USER`, `WP_MEDIA_APP_PASSWORD`. This lists field names only. No real secret values were read or embedded in these documents.

The generated initial WordPress admin password is bootstrap material. Owner setup changes the WordPress account's password, so the immutable Secret's original password is not a way to retrieve the owner's later password. Woo keys and media credentials serve independent integrations. API-key hashes, Argon2 password hashes and Fernet ciphertext have different roles: lookup/verifier, password verification and recoverable server credential encryption respectively.

The original store at `store.localtest.me` remains a separate store. A product appears on the store whose MCP server was called. Removing the old store's grants and granting the new store's tools changes the member's available destinations; it does not move products or orders between databases.

## C. Authorization matrix

| Action | Member / owner | Platform administrator |
|---|---|---|
| Use a tool | Needs that server/tool grant and an enabled valid destination | Broad tool access, subject to enabled/valid destination and quotas |
| Own chat/history/API keys | Own records only | Administrative key management is separate; no ownership transfer of chat |
| Create a store | Allowed when creation enabled and quotas permit | Same scheduling/creation rules; admin controls settings |
| View a managed store | Own stores | Administrative visibility |
| Delete a Ready/Failed store | Owner | May delete another owner's store |
| Disable/enable public store access | No | Administrator, with current revision/state checks |
| Start/redeem WP password setup | Exact owner, Ready/enabled/guarded store | Only if this administrator is also that store's owner |
| Register servers, grant tools, invite users, Monitoring | No | Administrator |

Ordinary browser requests do not extend server-side session expiry. The session has a 30-minute idle limit and eight-hour absolute limit; explicit renewal is capped by the absolute limit. User/session revocation is checked on the server. Request-size validation, CSRF checks and role checks remain relevant even when a UI button is hidden.

### Trust boundaries

- A member can invoke only granted tools, but a granted write tool uses the upstream's shared integration credential. The credential stays hidden; its permitted business power is still real.
- The model sees untrusted tool descriptions/results. System instructions and bounded selection help, but only the gateway's fresh authorization is the security boundary.
- An administrator is trusted to manage tool registrations/grants. SSRF and credential protection reduce the damage from an unsafe endpoint; they do not turn arbitrary upstream behavior into trusted content.
- A compromised worker has meaningful power in authorized managed-store namespaces. Restricted RBAC, admission rules, ownership guards and lack of PV-delete/platform-Secret access reduce the scope; they are not a claim that compromise is harmless.
- Network isolation, encrypted registry credentials and non-root containers are independent controls. Registry encryption protects stored upstream credentials. Kubernetes Secret encoding is a separate mechanism, and RBAC controls API access to those Secrets.
- The gateway and PostgreSQL are dependencies for a managed store's maintenance decision. Failure closes access rather than silently bypassing the gate.

## D. Detailed request and chat behavior

### Tool discovery and execution

The registry contains an enabled flag, endpoint, timeout, optional encrypted static credential and observed status. Initial Woo/analytics/Notes entries are bootstrapped idempotently. Administrators can manage generic registrations; store-generated entries remain under the store lifecycle.

`list_gateway_tools` loads the user and grants, discovers with bounded concurrency, tolerates individual server errors, and rechecks relevant state before returning permitted qualified names. `execute_gateway_tool` is the shared enforcement point. Before the external operation it commits an audit attempt; afterwards it records duration and outcome. A tool's annotations or description are not proof that the call is safe or authorized.

Client-facing tool names use `<server-slug>__<upstream-tool>`. Long qualified names presented to the LLM may use deterministic hashed aliases, mapped back within the turn. This is a provider-name compatibility step, not a different authorization identity. MCP clients retain the qualified gateway names.

### Chat loop and image destination

1. Authenticate the browser user and claim an idempotent chat turn in their conversation.
2. Build compact recent context; keep the saved full history separately.
3. Build a directory from currently permitted tools. Offer a small active schema set and bounded metadata search when needed.
4. Reserve an estimated model budget, call Groq, inspect the proposed tool name/arguments and map any alias back.
5. Execute through the common gateway core as that user; save progress and give the result back to the model.
6. Stop at the final answer or action/time/budget limit; persist a final or interrupted result for replay.

The current defaults include a 90-second chat bound, at most three upstream business tool actions, 512 maximum model output tokens, a 2,400-character compact-history bound and a 12,000-token estimated per-message budget. UTF-8-size estimates are not exact tokenizer accounting, pricing or a guarantee of provider quota availability. Tool search limits the active set to four definitions and about 6,000 schema characters. Compound requests seed that set from each clause. Later searches blend new matches with the previous active definitions within the same limits, and return accurate loaded/retained metadata. The tool set remains scoped to the current user and turn. Parallel model tool calls are disabled.

Image upload accepts bounded image bytes, checks their type and routes through an authorized store-specific media tool. The selected image destination is bound to the subsequent product write, preventing the attachment from silently selecting the original store. A member granted several stores must select the intended destination. The media account has the exact expected role capabilities (`read`, `upload_files`, `edit_posts`, `edit_others_posts`); it is not a WordPress administrator.

Chat is a bounded request/JSON-response flow with stored progress and running indicators, not model-token streaming. Relevant tool results can be sent to Groq as inference context; credentials stay private, but this is not local-only inference. Chat-turn replay is durable recovery, not a cache of product/order answers. If an external write may have succeeded but its reply was lost, the system reports uncertainty and avoids automatically repeating it. Universal exactly-once business writes and universal human approval for every mutating tool are not implemented guarantees.

## E. Worker state, ownership and storage protocol

### State and scheduling

| Object | States / role |
|---|---|
| Store | `Provisioning`, `Ready`, `Failed`, `Deleting`, `Deleted`; access enabled/disabled is a separate field |
| Operation | `Pending`, `Running`, `Succeeded`, `Failed`; `provision` or `delete` action |
| Claim | Operation/store/owner/generation, attempt, lease token, address snapshot and fixed deadline |
| Saved storage | Claim UID first, optional PV name, then complete PV UID binding; evidence is extended, never replaced with another identity |

Creation uses an account-scoped request UUID and fingerprint. Retrying the same accepted request returns the existing operation; changing content under the same identifier conflicts. The PostgreSQL admission lock serializes quota-sensitive transitions. Work selection prioritizes deletion and overdue cleanup/recovery, then new work with owner fairness. Row locking and `SKIP LOCKED` keep competing workers from taking the same claim. Transaction-scoped advisory locks release with the transaction; see [PostgreSQL's locking documentation](https://www.postgresql.org/docs/17/explicit-locking.html).

The lease is 60 seconds, renewal normally every 20 seconds with a ten-second renewal timeout. Worker local task capacity defaults to 100 and polling to one second; **100 is not the global allowance for simultaneous store creation**. PostgreSQL settings enforce the configured fleet-wide concurrent creation limit. Failed and Deleting stores continue to consume store quota until deletion is complete. Original provisioning deadlines remain fixed across recovery.

### Provisioning stages and their proof

| Stage | Why it exists / condition before proceeding |
|---|---|
| Namespace | Deterministic name, store/platform ownership metadata; persist actual namespace UID before granting scoped access |
| Credentials | Create/recover exact owned immutable Secret; save its UID before workload installation |
| Helm plan/config | Fixed release/chart/profile, target host, Secret references, managed guardrails, remaining deadline; no passwords in overrides |
| Helm execution | Private workspace, verified API TLS, fixed argv, no shell, bounded pipes; monitor claim and actual resource identity |
| Release inspection/recovery | Validate owned release, saved values, supported revisions/state and original bootstrap timeout; deployed retries reuse it, supported interrupted managed releases can reconcile |
| Workloads | Check MySQL StatefulSet, WordPress/Woo/analytics Deployments and bootstrap Job with current generation/replicas/ownership; poll managed Job completion while retaining the renewable lease/creation slot |
| Storage | Both `data-mysql-0` and `wordpress-data` bound to exact PVC and PV UIDs; persist then read back |
| WordPress | Read storefront, test separate Woo RW/RO credentials with reads, authenticate media account; no verification business writes |
| MCP registration/discovery | Save both scoped registrations disabled; private claim-authorized discovery checks required core tools |
| Final verification | Repeat workload/storage checks, verify exact maintenance Middleware/Ingress and live claim |
| Terminal commit | Lock/recheck stored evidence, live lease and original deadline; atomically enable registrations, mark Ready/Succeeded and audit |

Observed allowances use PostgreSQL time and bounded local monotonic deadlines. Lock waits, transaction completion time or local clock assumptions cannot revive an expired lease/deadline. Worker network calls are made outside its short ownership transactions. Cancellation is awaited through active HTTP/subprocess cleanup before a claim can safely be released.

### Managed bootstrap and interrupted Helm recovery

The static store uses a post-install/post-upgrade bootstrap hook. For managed stores, `bootstrap.managedJob=true` creates an ordinary durable Job without those hook annotations. It has bounded retries and the original active deadline.

Managed installation uses `--wait=false`. The worker reads persisted release state, then polls readiness every five seconds under its renewable claim and creation slot. Stopping the worker cancels its active calls/processes but leaves the Kubernetes Job available to continue.

Replacement attempts can reconcile supported `failed`, `pending-install` or `pending-upgrade` managed releases. They first verify the claim, actual namespace/credential Secret, saved release identity/chart/values and original bootstrap configuration. Deployed releases are reused.

For a pending Helm operation, `store_helm_recovery.py` decodes the pinned Helm-3 Secret format and tests UID, resourceVersion, old release payload and old pending status in one atomic JSON Patch. It changes that abandoned operation to failed, not deployed. A subsequent normal guarded upgrade with `--reuse-values` reconciles resources and records its own result. No force replacement, reset-values operation or bootstrap Job deletion is used. Decoded release data is capped at 1 MiB and supported revisions/history at eight.

The 23 October live test had two simultaneous Running jobs with live leases on attempt 1 before a controlled worker restart. Both reached Ready/Succeeded on attempt 2 with their original namespace/Secret UIDs, operation IDs and deadlines. This is the current recovery evidence. Separate fixture/transaction tests cover other failed/pending/lost-reply/cancellation cases; no generic chart-upgrade controller is implied.

### Helm subprocess safeguards

`_MAX_INPUT = 64 * 1024` limits stdin configuration to 64 KiB. `_MAX_OUTPUT = 1024 * 1024` bounds combined child output at 1 MiB. `_STOP_GRACE_SECONDS = 3` is the TERM grace before forced process-group shutdown. These bounds protect memory and shutdown behavior; they are unrelated to passwords, replica counts or the time allowed to provision a store.

Helm receives explicit argv and copied/isolated environment, never a shell command assembled from a store name. Temporary directories/files use 0700/0600. The kubeconfig refers to mounted token/CA files. The pod's projected token requests a one-hour lifetime, and Kubernetes rotates it. Worker shutdown is given a 90-second pod grace period; active requests and process cleanup finish before the HTTP client/database close. On timeout/cancellation the runner stops descendants, escalates if needed and waits for reaping before workspace removal. It does not blindly retry an uncertain installation. A later owned attempt must inspect saved state before supported reconciliation.

### Bootstrap and PHP helpers

WordPress/WooCommerce execute in PHP, so WP-CLI helpers use the application's own APIs and storage conventions. `keys.php` verifies/reuses the two Woo credentials with serialized setup and transactional checks. `media.php` creates/verifies the restricted account and application password without rotating a previously committed identity on retry. `wordpress-setup.php` handles the owner's later password form. They are application/bootstrap files, not extra public backend services.

`bootstrap.sh` waits for configuration/database, installs WordPress with the chosen store name, installs the pinned Woo version, sets permalinks/store pages/COD/shipping, then verifies keys/media and applies site and customer-account settings. New stores start without products; bootstrap does not read, create or change product records. Replays reuse the pinned plugin; conflicting installed versions stop safely.

The setup plugin mounts at `/opt/urumi/mu-plugins`, outside the writable WordPress data PVC. Mounting a read-only nested directory under an otherwise fresh data path previously caused root-owned content directories and prevented non-root plugin installation. The external mount fixes the directory ownership/startup sequence without running WordPress as root.

### Deletion and late storage recovery

Deletion first disables store/MCP access and queues work. Before deleting the owned namespace the worker captures available PVC identity, including Pending claims. It learns and persists matching PV names/UIDs, then later confirms disappearance. Replaced resources or cross-store claim references cannot be adopted.

A PVC may disappear before the worker has observed the final PV. Cleanup therefore scans bounded PV pages and matches claim/store identity against saved evidence. `PAGE_SIZE = 100`, `MAX_PAGES = 100`, `_MAX_CONTINUE = 4096` cap response work and continuation-token size. Continuation tokens are opaque and encoded; a consistent resource version, no repeated token, no duplicate identities and completion of every page are required. Hitting a cap/expired cursor produces no partial claim of cleanup success. An empty scan alone is not universal deletion proof. Kubernetes typed PV lists may omit apiVersion/kind on individual items; after validating the list envelope, the pager supplies only the expected v1/PersistentVolume defaults before strict item-identity validation.

The worker cannot delete PVs. The storage controller acts on reclaim policy `Delete`; cleanup waits for evidence of absence and retains identifiers/audits. No namespace/PV absence statement proves forensic erasure of physical disk data. If the node or provisioner cannot finish reclaiming storage, cleanup must remain incomplete rather than silently succeeding.

## F. Owner password setup and maintenance

~~~mermaid
sequenceDiagram
    participant O as Store owner
    participant G as Gateway
    participant D as PostgreSQL
    participant W as WordPress setup plugin
    O->>G: Request setup for an enabled Ready store
    G->>D: Validate owner, session and store identities
    G->>D: Save setup token hash, expiry and audit event
    D-->>G: Commit setup token
    G-->>O: Return token and validated setup destination
    O->>W: POST token to the store setup page
    W-->>O: Show password and confirmation form
    O->>W: Submit password, confirmation, token and form nonce
    Note over O,W: Password goes directly to WordPress
    W->>W: Validate password, nonce and configured administrator
    W->>G: Redeem setup token with store ID
    G->>D: Recheck owner, session, store state, identities and expiry
    G->>D: Consume token once and record audit event
    D-->>G: Commit redemption
    G-->>W: Setup authorized with 204 No Content
    Note over G,D: Gateway stores the token hash and never the chosen password
    W->>W: Hash and save password using WordPress
    W->>W: Verify password and revoke WordPress sessions
    W->>W: Set website title to the store name
    W-->>O: Show password saved and WordPress login link
~~~

The setup capability lasts five minutes, is stored as a hash and is bound to browser session, owner, store generation, access revision and namespace UID. It is posted, not put in the URL or browser persistent storage. Disablement, deletion, identity/session changes and expiry invalidate it. Passwords do not enter gateway setup requests. The password policy is 6–16 characters. New stores use username `admin`; compatible older accounts retain their existing username.

Capability consumption and the WordPress password write span separate systems. There is no distributed transaction: a consumed capability followed by a failed WP write requires issuing a new setup capability. Non-owner administrators cannot use this to reset another store owner's password.

The public maintenance gate checks state, not the shopper's identity. Disabling a store keeps products/orders/volumes/grants, returns maintenance at the public host and disables its MCP entries. Re-enabling permits access again if state/revision checks pass. Deleting is a separate destructive lifecycle request.

## G. Monitoring, caching and bounded resources

| Signal | Origin | What it does and does not prove |
|---|---|---|
| Per-server calls/errors/latency/P95 | Durable tool audit rows | Historical confirmed executions; denied and uncertain calls stay separate |
| Tool call time buckets | PostgreSQL aggregates | Twelve buckets across selected 1/6/24-hour window |
| Store counts/durations/jobs | Stores and operations | Queue, running, overdue, completion/failure evidence |
| Upstream status | Last recorded discovery/call | Last observation, not continuous active probing |
| Gateway HTTP totals | This gateway process | Request/error/rejected/cancelled counts and average; resets on restart |
| Request IDs | Safe JSON middleware/logging | Correlates requests/audits; not full distributed OpenTelemetry tracing |
| Worker probes | Local scheduler heartbeat | Heartbeat within 90 seconds; readiness also needs a successful queue observation within 90 seconds; probes do no network/DB work |
| Dashboard alerts | Snapshot-derived rules | In-page when refreshed; no email/SMS/background alert delivery |

The Monitoring endpoint is administrator-only, performs bounded read-only database aggregation, uses a 20-second in-process cache and returns no raw arguments/credentials. Browser polling is sequential, normally every 30 seconds while visible, with timeout, abort and stale-result protection. HTTP process counters exclude probes/Monitoring polls. A cache hit does not avoid authentication.

Alert thresholds in source are: available jobs waiting over two minutes, running leases expired by more than 60 seconds, any overdue/failed creations or uncertain calls, and at least 20% confirmed tool errors after at least ten confirmed calls. The monitoring transaction uses REPEATABLE READ / READ ONLY with a two-second statement timeout; snapshot construction has an eight-second bound. These are snapshot-derived alerts, not a continuously running alert engine.

Tool catalogue caching stores only deep-copied schema metadata, normally for `woo`, `analytics`, `notes`. It bounds entries/bytes, expires by monotonic time, coalesces concurrent misses and rejects late invalidated results. Changed server identity/credential participates in the cache identity. Cached discovery does not make the server's observed status newer. Orders and tool results are not cached.

PostgreSQL rate-limit rows hold per-user budgets across that user's API keys, login limits and global chat limits. Grants and key activity are checked from the database. Schema/monitoring caches and HTTP counters belong to the gateway process. The project does not use Redis.

### Source resource requests and limits

These are local source settings: platform defaults plus values-apps-local.yaml / values-provisioner-local.yaml, and store defaults. Production overrides are described in section S. They are not a live resource dump. CPU `m` means one-thousandth of a CPU core; memory `Mi`/`Gi` are binary units. A request influences scheduling, while a limit constrains usage. A StatefulSet with one PVC is persistent, not automatically highly available.

| Source workload | CPU request | Memory request | CPU limit | Memory limit |
| --- | --- | --- | --- | --- |
| postgres | 100m | 256Mi | 500m | 512Mi |
| notes | 50m | 64Mi | 300m | 256Mi |
| gateway | 100m | 128Mi | 1 | 512Mi |
| dashboard | 25m | 32Mi | 300m | 128Mi |
| storeProvisioner | 100m | 128Mi | 1 | 768Mi |
| db | 100m | 256Mi | 500m | 768Mi |
| wordpress | 100m | 256Mi | 1000m | 768Mi |
| bootstrap | 100m | 256Mi | 1000m | 768Mi |
| wooMcp | 50m | 64Mi | 300m | 192Mi |
| analytics | 50m | 64Mi | 300m | 256Mi |

| Workload | Probe / completion behavior | Runtime UID |
| --- | --- | --- |
| Gateway | Startup/liveness /healthz; readiness /readyz checks DB | 10001 |
| Dashboard | Startup/readiness/liveness /healthz | 101 |
| PostgreSQL | Startup/readiness/liveness pg_isready | 999 |
| Notes | Startup/liveness /healthz; readiness /readyz checks DB | 10001 |
| Provisioner | Local heartbeat command; fresh scheduler and queue readiness | 10001 |
| WordPress | Startup/readiness/liveness /wp-login.php; final provisioning also probes store credentials | 33 |
| MySQL | Readiness/liveness mysqladmin ping | 999 |
| Woo MCP and analytics | Readiness/liveness /healthz; service discovery checked separately by provisioner | 10001 |
| Bootstrap Job | OnFailure restart, backoffLimit 6 and bounded active deadline; terminal completion verified; no continuous HTTP probes | 33 |

Health endpoint success is not proof of a complete storefront purchase or every downstream dependency. Jobs use completion/deadline semantics; do not describe the bootstrap Job as having continuous service probes. Containers use non-root identities and restrictive security contexts; selected writable data/runtime mounts are explicit. A source value or rendered template is not a measurement of current live limits.

### Managed-store guardrails

| ResourceQuota dimension | Value |
| --- | --- |
| requests.cpu | 1 |
| requests.memory | 1536Mi |
| limits.cpu | 4 |
| limits.memory | 4Gi |
| requests.storage | 4Gi |
| persistentvolumeclaims | 2 |
| pods | 8 |
| services | 6 |
| secrets | 12 |
| configmaps | 8 |

| LimitRange category | Setting |
| --- | --- |
| enabled | true |
| container | {"default": {"cpu": "500m", "memory": "512Mi"}, "defaultRequest": {"cpu": "100m", "memory": "128Mi"}, "max": {"cpu": "2", "memory": "1Gi"}, "min": {"cpu": "10m", "memory": "32Mi"}} |
| volume | {"max": {"storage": "2Gi"}, "min": {"storage": "1Gi"}} |

Two 2-GiB store PVCs fit the 4-GiB aggregate quota. The base platform PostgreSQL PVC is 1 GiB. These are distinct from per-user/global store-count and concurrent-creation settings in PostgreSQL. The production values pair two 10-GiB claims with a 20-GiB quota.

## H. Complete gateway HTTP and MCP surface

This is the 49 decorated HTTP routes found in gateway source, plus the separately mounted MCP endpoint. Dependencies alone do not reveal every check: some handlers perform additional role/ownership/capability checks. The table records that distinction. The gateway also exposes generated FastAPI documentation/OpenAPI routes at `/api/docs` and `/api/openapi.json`; these are not listed as administrator-only endpoints.

| Method and route | Access / role | Source handler |
| --- | --- | --- |
| `PATCH /api/admin/users/{user_id}/active` | Platform administrator; CSRF for authenticated mutations | `admin_access.set_active` |
| `GET /api/admin/users/{user_id}/keys` | Platform administrator; CSRF for authenticated mutations | `admin_access.member_keys` |
| `POST /api/admin/users/{user_id}/keys` | Platform administrator; CSRF for authenticated mutations | `admin_access.issue_key` |
| `DELETE /api/admin/users/{user_id}/keys/{key_id}` | Platform administrator; CSRF for authenticated mutations | `admin_access.revoke_member_key` |
| `GET /api/admin/security-events` | Platform administrator; CSRF for authenticated mutations | `admin_access.security_events` |
| `GET /api/audit` | Signed-in user; own records, or administrator-wide visibility | `audit.read_audit` |
| `POST /api/auth/login` | Public login; credential verification and login limits | `auth.login` |
| `GET /api/auth/me` | Signed-in current user; own keys/session; CSRF on authenticated mutations | `auth.me` |
| `POST /api/auth/logout` | Signed-in current user; own keys/session; CSRF on authenticated mutations | `auth.logout` |
| `POST /api/keys` | Signed-in current user; own keys/session; CSRF on authenticated mutations | `auth.create_key` |
| `GET /api/keys` | Signed-in current user; own keys/session; CSRF on authenticated mutations | `auth.list_keys` |
| `DELETE /api/keys/{key_id}` | Signed-in current user; own keys/session; CSRF on authenticated mutations | `auth.revoke_key` |
| `GET /api/key-check` | Personal API key validated manually | `auth.key_check` |
| `GET /api/auth/session` | Signed-in current user; own keys/session; CSRF on authenticated mutations | `auth.session_status` |
| `POST /api/auth/renew` | Signed-in current user; own keys/session; CSRF on authenticated mutations | `auth.renew_session` |
| `POST /api/chat` | Signed-in user; own conversations/turns; shared core rechecks tool grants | `chat.chat` |
| `POST /api/chats` | Signed-in user; own conversations/turns; shared core rechecks tool grants | `chats.create_chat` |
| `GET /api/chats` | Signed-in user; own conversations/turns; shared core rechecks tool grants | `chats.list_chats` |
| `GET /api/chats/{conversation_id}` | Signed-in user; own conversations/turns; shared core rechecks tool grants | `chats.get_chat` |
| `DELETE /api/chats/{conversation_id}` | Signed-in user; own conversations/turns; shared core rechecks tool grants | `chats.delete_chat` |
| `GET /healthz` | Unauthenticated probe; safe health result | `main.healthz` |
| `GET /readyz` | Unauthenticated probe; safe health result | `main.readyz` |
| `POST /api/media/upload` | Signed-in user; per-tool/selected-store grants and rate limits | `media.upload_image` |
| `GET /api/admin/monitoring` | Platform administrator; CSRF for authenticated mutations | `monitoring.read_monitoring` |
| `GET /api/users/{user_id}/permissions` | Platform administrator; CSRF for authenticated mutations | `permissions.get_permissions` |
| `POST /api/users/{user_id}/permissions` | Platform administrator; CSRF for authenticated mutations | `permissions.grant_permission` |
| `DELETE /api/users/{user_id}/permissions` | Platform administrator; CSRF for authenticated mutations | `permissions.revoke_permission` |
| `GET /api/servers` | Platform administrator; CSRF for authenticated mutations | `servers.get_servers` |
| `POST /api/servers` | Platform administrator; CSRF for authenticated mutations | `servers.create_server` |
| `PUT /api/servers/{server_id}` | Platform administrator; CSRF for authenticated mutations | `servers.edit_server` |
| `DELETE /api/servers/{server_id}` | Platform administrator; CSRF for authenticated mutations | `servers.delete_server` |
| `POST /api/servers/{server_id}/check` | Platform administrator; CSRF for authenticated mutations | `servers.check_server` |
| `GET /api/store-access/{store_id}` | Public ForwardAuth policy decision; no private store/account data | `store_access.store_access_gate` |
| `DELETE /api/stores/{store_id}` | Owner or platform administrator; lifecycle/idempotency/state checks | `store_lifecycle.delete_store` |
| `PUT /api/stores/{store_id}/access` | Platform administrator, enforced inside lifecycle handler; revision/state checks | `store_lifecycle.set_store_access` |
| `POST /api/stores` | Signed-in user; creation enabled, quotas and request identity | `store_requests.request_store_creation` |
| `GET /api/admin/store-settings` | Platform administrator; CSRF for authenticated mutations | `store_settings_api.read_settings` |
| `PUT /api/admin/store-settings` | Platform administrator; CSRF for authenticated mutations | `store_settings_api.update_settings` |
| `POST /api/stores/{store_id}/wordpress-setup` | Signed-in exact store owner only; Ready/enabled/current identities and CSRF | `store_wordpress_setup.issue_setup` |
| `POST /api/store-wordpress-setup/redeem` | One-use owner/session/store-bound capability, validated manually | `store_wordpress_setup.redeem_setup` |
| `GET /api/stores` | Signed-in owner scope, or administrator visibility | `stores.list_stores` |
| `GET /api/stores/{store_id}` | Signed-in owner scope, or administrator visibility | `stores.get_store` |
| `GET /api/tools` | Signed-in user; per-tool/selected-store grants and rate limits | `tools.get_tools` |
| `POST /api/tools/call` | Signed-in user; per-tool/selected-store grants and rate limits | `tools.execute_tool` |
| `GET /api/admin/usage-limits` | Platform administrator; CSRF for authenticated mutations | `usage_limits.get_limits` |
| `PUT /api/admin/usage-limits` | Platform administrator; CSRF for authenticated mutations | `usage_limits.update_limits` |
| `GET /api/users` | Platform administrator; CSRF for authenticated mutations | `users.list_users` |
| `POST /api/users/invite` | Platform administrator; CSRF for authenticated mutations | `users.invite_member` |
| `POST /api/auth/accept-invite` | Public redemption of a valid invitation; validated password/input | `users.accept_invite` |

| Additional endpoint | Behavior |
| --- | --- |
| `/mcp` | Stateless Streamable HTTP; personal gateway Bearer key; enabled/granted tools; shared core execution/audit |
| `/api/docs`, `/api/openapi.json` | Generated API documentation/schema; document public exposure accurately |

## I. Complete gateway data model

Fields below are SQLAlchemy model annotations, not real rows. Models inheriting `TimestampMixin` also have `created_at: Mapped[datetime]`, a non-null timezone-aware timestamp defaulting to database time. Tables below list fields declared in each model. Python types alone do not list all defaults, indexes, foreign keys or check constraints; [apps/gateway/app/models.py](../apps/gateway/app/models.py) and the migrations define them. Password/key plaintexts are not implied by a hash or encrypted field.

Ownership links are user → keys/sessions/chats/stores, conversation → turns, user+server+tool → grants, store → jobs/volume evidence/setup capability and store → its two registrations. Store/namespace foreign keys and unique PVC/PV evidence prevent cross-store adoption. Managed registration deletion removes its grants; historical audit identity remains useful afterwards.

### `users` — `User`

Team identity, role, activation, password hash, invitation and session version.

| Field | Model annotation |
| --- | --- |
| `id` | `Mapped[int]` |
| `email` | `Mapped[str]` |
| `password_hash` | `Mapped[str \| None]` |
| `role` | `Mapped[str]` |
| `active` | `Mapped[bool]` |
| `session_version` | `Mapped[int]` |
| `invite_token_hash` | `Mapped[str \| None]` |
| `invite_expires_at` | `Mapped[datetime \| None]` |

### `api_keys` — `APIKey`

User-owned key hashes/prefixes, active state and usage metadata.

| Field | Model annotation |
| --- | --- |
| `id` | `Mapped[int]` |
| `user_id` | `Mapped[int]` |
| `name` | `Mapped[str]` |
| `prefix` | `Mapped[str]` |
| `key_hash` | `Mapped[str]` |
| `revoked_at` | `Mapped[datetime \| None]` |
| `last_used_at` | `Mapped[datetime \| None]` |

### `mcp_servers` — `MCPServer`

Upstream endpoint/configuration, encrypted credential and last observation.

| Field | Model annotation |
| --- | --- |
| `id` | `Mapped[int]` |
| `slug` | `Mapped[str]` |
| `name` | `Mapped[str]` |
| `url` | `Mapped[str]` |
| `auth_header_name` | `Mapped[str \| None]` |
| `credential_encrypted` | `Mapped[str \| None]` |
| `enabled` | `Mapped[bool]` |
| `timeout_s` | `Mapped[float]` |
| `status` | `Mapped[str]` |
| `status_reason` | `Mapped[str \| None]` |
| `last_checked_at` | `Mapped[datetime \| None]` |

### `audit_log` — `AuditLog`

Tool attempt identity, source, server/tool, duration and outcome; retained independently of a live server.

| Field | Model annotation |
| --- | --- |
| `id` | `Mapped[int]` |
| `user_id` | `Mapped[int \| None]` |
| `server_id` | `Mapped[int \| None]` |
| `server_slug` | `Mapped[str \| None]` |
| `tool` | `Mapped[str]` |
| `duration_ms` | `Mapped[int]` |
| `outcome` | `Mapped[str]` |
| `source` | `Mapped[str]` |
| `error` | `Mapped[str \| None]` |

### `registry_bootstrap` — `RegistryBootstrap`

Idempotent marker for initial server registration.

| Field | Model annotation |
| --- | --- |
| `id` | `Mapped[int]` |

### `tool_grants` — `ToolGrant`

Explicit per-user/server/tool permissions; members start without grants.

| Field | Model annotation |
| --- | --- |
| `user_id` | `Mapped[int]` |
| `server_id` | `Mapped[int]` |
| `tool_name` | `Mapped[str]` |
| `granted_by_id` | `Mapped[int \| None]` |

### `chat_conversations` — `ChatConversation`

Owner-scoped conversation identity/title/timestamps.

| Field | Model annotation |
| --- | --- |
| `id` | `Mapped[UUID]` |
| `user_id` | `Mapped[int]` |
| `title` | `Mapped[str]` |
| `deleted_at` | `Mapped[datetime \| None]` |
| `updated_at` | `Mapped[datetime]` |

### `chat_turns` — `ChatTurn`

Idempotent accepted turns, progress, replay/final response and interruption state.

| Field | Model annotation |
| --- | --- |
| `id` | `Mapped[UUID]` |
| `conversation_id` | `Mapped[UUID]` |
| `message` | `Mapped[str]` |
| `image_id` | `Mapped[int \| None]` |
| `image_name` | `Mapped[str]` |
| `status` | `Mapped[str]` |
| `deadline_at` | `Mapped[datetime]` |
| `response` | `Mapped[dict]` |
| `http_status` | `Mapped[int \| None]` |

### `security_events` — `SecurityEvent`

Administrative/lifecycle/security changes with safe metadata.

| Field | Model annotation |
| --- | --- |
| `id` | `Mapped[int]` |
| `actor_id` | `Mapped[int \| None]` |
| `target_user_id` | `Mapped[int \| None]` |
| `action` | `Mapped[str]` |
| `details` | `Mapped[dict]` |

### `browser_sessions` — `BrowserSession`

Revocable browser sessions, nonce hash and idle/absolute expiry.

| Field | Model annotation |
| --- | --- |
| `id` | `Mapped[str]` |
| `user_id` | `Mapped[int]` |
| `session_version` | `Mapped[int]` |
| `last_activity_at` | `Mapped[datetime]` |
| `idle_expires_at` | `Mapped[datetime]` |
| `absolute_expires_at` | `Mapped[datetime]` |
| `revoked_at` | `Mapped[datetime \| None]` |

### `usage_limit_settings` — `UsageLimitSettings`

Shared administrator-controlled quota configuration and revision.

| Field | Model annotation |
| --- | --- |
| `id` | `Mapped[int]` |
| `revision` | `Mapped[int]` |
| `login_account_limit` | `Mapped[int]` |
| `login_ip_limit` | `Mapped[int]` |
| `login_window_seconds` | `Mapped[int]` |
| `chat_per_minute` | `Mapped[int]` |
| `chat_per_hour` | `Mapped[int]` |
| `chat_global_per_minute` | `Mapped[int]` |
| `tool_per_minute` | `Mapped[int]` |
| `updated_at` | `Mapped[datetime]` |
| `updated_by_id` | `Mapped[int \| None]` |

### `rate_limit_buckets` — `RateLimitBucket`

Transactional quota counters/windows shared across processes and keys.

| Field | Model annotation |
| --- | --- |
| `id` | `Mapped[str]` |
| `scope` | `Mapped[str]` |
| `used` | `Mapped[int]` |
| `reset_at` | `Mapped[datetime]` |

### `stores` — `Store`

Store owner/name/address, desired/current lifecycle, identities, access and MCP links.

| Field | Model annotation |
| --- | --- |
| `id` | `Mapped[UUID]` |
| `owner_id` | `Mapped[int]` |
| `name` | `Mapped[str]` |
| `slug` | `Mapped[str]` |
| `namespace` | `Mapped[str]` |
| `namespace_uid` | `Mapped[str \| None]` |
| `credentials_secret_uid` | `Mapped[str \| None]` |
| `release_name` | `Mapped[str]` |
| `url` | `Mapped[str]` |
| `status` | `Mapped[str]` |
| `access_enabled` | `Mapped[bool]` |
| `access_guarded` | `Mapped[bool]` |
| `access_revision` | `Mapped[int]` |
| `desired_state` | `Mapped[str]` |
| `generation` | `Mapped[int]` |
| `status_reason` | `Mapped[str \| None]` |
| `mcp_server_id` | `Mapped[int \| None]` |
| `analytics_mcp_server_id` | `Mapped[int \| None]` |
| `updated_at` | `Mapped[datetime]` |
| `deleted_at` | `Mapped[datetime \| None]` |

### `store_operations` — `StoreOperation`

Durable provision/delete work, availability, lease, attempts and original deadline.

| Field | Model annotation |
| --- | --- |
| `id` | `Mapped[UUID]` |
| `store_id` | `Mapped[UUID]` |
| `requested_by_id` | `Mapped[int]` |
| `request_key` | `Mapped[UUID]` |
| `request_fingerprint` | `Mapped[str]` |
| `action` | `Mapped[str]` |
| `store_generation` | `Mapped[int]` |
| `status` | `Mapped[str]` |
| `attempts` | `Mapped[int]` |
| `available_at` | `Mapped[datetime]` |
| `deadline_at` | `Mapped[datetime]` |
| `lease_token` | `Mapped[UUID \| None]` |
| `lease_expires_at` | `Mapped[datetime \| None]` |
| `started_at` | `Mapped[datetime \| None]` |
| `finished_at` | `Mapped[datetime \| None]` |
| `last_error` | `Mapped[str \| None]` |
| `updated_at` | `Mapped[datetime]` |

### `store_provisioning_settings` — `StoreProvisioningSettings`

Creation switch, store quotas, concurrency, timeout and settings revision.

| Field | Model annotation |
| --- | --- |
| `id` | `Mapped[int]` |
| `revision` | `Mapped[int]` |
| `enabled` | `Mapped[bool]` |
| `max_per_user` | `Mapped[int]` |
| `max_total` | `Mapped[int]` |
| `max_concurrent_creations` | `Mapped[int]` |
| `timeout_seconds` | `Mapped[int]` |
| `updated_at` | `Mapped[datetime]` |
| `updated_by_id` | `Mapped[int \| None]` |

### `store_volumes` — `StoreVolume`

Store/namespace-bound PVC and PV evidence, including partial progress/tombstones.

| Field | Model annotation |
| --- | --- |
| `store_id` | `Mapped[UUID]` |
| `claim_name` | `Mapped[str]` |
| `namespace_uid` | `Mapped[str]` |
| `claim_uid` | `Mapped[str]` |
| `volume_name` | `Mapped[str \| None]` |
| `volume_uid` | `Mapped[str \| None]` |
| `storage_class` | `Mapped[str]` |
| `updated_at` | `Mapped[datetime]` |

### `store_wordpress_setup` — `StoreWordPressSetup`

Hash of one-use setup capability bound to session/owner/store identity and expiry.

| Field | Model annotation |
| --- | --- |
| `store_id` | `Mapped[UUID]` |
| `token_hash` | `Mapped[str]` |
| `owner_id` | `Mapped[int]` |
| `actor_id` | `Mapped[int]` |
| `session_id` | `Mapped[str]` |
| `generation` | `Mapped[int]` |
| `access_revision` | `Mapped[int]` |
| `namespace_uid` | `Mapped[str]` |
| `expires_at` | `Mapped[datetime]` |
| `used_at` | `Mapped[datetime \| None]` |

The Notes service separately creates a `notes` table (id, title, body, created_at) in its database. Notes are shared team notes, not automatically isolated per user. WordPress/WooCommerce manage their own MySQL schema; exact order tables depend on Woo configuration/version, so this document does not invent a fixed complete plugin table list. Products, users/usermeta, settings, orders and media metadata remain in the store's database, with uploaded files on its content PVC.

Important model invariants include unique user/key/server identities, owner-scoped idempotency, retained store address identity, and storage uniqueness/namespace ownership constraints. PVC/PV identifiers retained after deletion prevent silently adopting a reused identity. Final completion locks and revalidates the records rather than trusting an earlier in-memory snapshot.

## J. All locally implemented MCP tools

These 19 tools are implemented in the three local custom MCP services. Tavily and other remote registrations have their own dynamic catalogues, which are not fabricated here. The gateway adds server-qualified names; the names below are upstream-local names.

| Service | Tool | Responsibility |
| --- | --- | --- |
| woo-mcp | `list_products` | Read a bounded page/filter of WooCommerce products. |
| woo-mcp | `create_product` | Create a product in this upstream's store; published state and supplied arguments determine storefront visibility. |
| woo-mcp | `get_product` | Read one product by ID in this store. |
| woo-mcp | `update_product` | Change allowed product fields in this store. |
| woo-mcp | `delete_product` | Trash or explicitly permanently delete the selected product; no cross-store identity implied. |
| woo-mcp | `list_orders` | Read this store's filtered/paginated WooCommerce orders. |
| woo-mcp | `get_order` | Read one order in this store. |
| woo-mcp | `update_order_status` | Change one order to an allowed WooCommerce status. |
| woo-mcp | `upload_media` | Upload validated image content through this store's restricted media account. |
| woo-mcp | `list_media` | Read media metadata from this store. |
| woo-mcp | `update_media` | Change selected media metadata in this store. |
| woo-mcp | `delete_media` | Delete selected media with required explicit confirmation argument. |
| mcp-analytics | `sales_summary` | Aggregate bounded order reads using the separate read-only credential. |
| mcp-analytics | `order_counts` | Count orders by status within validated date filters. |
| mcp-analytics | `top_products` | Rank products from bounded order-line reads. |
| mcp-notes | `add_note` | Create a shared team note in the Notes database. |
| mcp-notes | `list_notes` | List shared notes with bounded results. |
| mcp-notes | `get_note` | Read one shared note by ID. |
| mcp-notes | `info` | Describe the Notes service. |

Analytics dates use validated inclusive calendar dates in the store timezone. Sales summaries use processing/completed orders and gross order totals, including tax/shipping; partial refunds are not deducted. COD may still be unpaid. These figures should not be described as verified cash collected. Bounded pagination can report inability to complete rather than invent totals from a partial inventory.

### Analytics data flow and calculation rules

~~~mermaid
sequenceDiagram
    participant C as Chat or MCP client
    participant G as Gateway core
    participant A as Selected analytics MCP
    participant W as Same-store WooCommerce REST
    C->>G: Qualified analytics tool and dates
    G->>G: Account, grant, store and audit checks
    G->>A: MCP initialize and tool call
    A->>W: GET order pages with read-only Woo credentials
    W-->>A: Selected order and line-item fields
    A->>A: Count statuses / sum Decimal totals / rank quantities
    A-->>G: Complete bounded result or explicit error
    G-->>C: Audited result labelled with the selected store
~~~

| Detail | Implemented behavior |
|---|---|
| REST source | Same-store `/wp-json/wc/v3/orders`; never direct MySQL |
| Credential | Separate Woo read-only key/secret, not the Woo MCP RW credential |
| Fields | `id,status,currency,total,line_items`; ascending order ID, status=any; trash omitted |
| Dates | Strict inclusive YYYY-MM-DD in WordPress site timezone; order creation date |
| Empty dates | All available history, subject to scan limits |
| Bound | Ten pages × 100 orders; complete scan required; narrower range requested if it cannot finish |
| Deadline | 25 seconds overall; ten-second HTTP timeouts; outer gateway timeout can be shorter |
| `order_counts` | Non-trash order count and status breakdown |
| `sales_summary` | Processing/completed orders, Decimal gross sums grouped by currency |
| Gross amount meaning | Tax/shipping included; partial refunds not deducted; processing COD may be unpaid |
| `top_products` | Product/variation quantities from processing/completed orders; limit 1–20 with stable tie order |
| Freshness | Live REST reads; no result cache, copied order DB or warehouse |
| Concurrent changes | Paginated REST is not a transactional snapshot while orders change |
| Across stores | Separate authorized calls, no direct database join; a failed result is not zero |

`analytics_chat.py` handles one narrow all-time totals intent: select one permitted analytics pair, use empty dates, reject unexpected/repeated calls and format validated totals. If several destinations are possible, it asks which store. General two-store questions use normal tool selection and separate qualified names.

## K. Every gateway source module

The following 94-module index explains each component's responsibility. Helpers split pure validation, transport, ownership checks, orchestration and final database completion so none of those steps alone grants the others' authority. It is a component index, not a claim that every private function is separately described.

| Module | Responsibility |
| --- | --- |
| [apps/gateway/app/__init__.py](../apps/gateway/app/__init__.py) | Python package marker; no worker or service starts merely from this file. |
| [apps/gateway/app/admin_access.py](../apps/gateway/app/admin_access.py) | Administrator account activation, member-key management and security-event access. |
| [apps/gateway/app/analytics_chat.py](../apps/gateway/app/analytics_chat.py) | Helpers for unambiguous all-time order-count and sales questions. |
| [apps/gateway/app/audit.py](../apps/gateway/app/audit.py) | User-scoped audit reads, with administrator-wide visibility and bounded filtering/pagination. |
| [apps/gateway/app/auth.py](../apps/gateway/app/auth.py) | Login/logout, session checks/renewal, CSRF, own keys and personal API-key authentication. |
| [apps/gateway/app/browser_sessions.py](../apps/gateway/app/browser_sessions.py) | Server-enforced dashboard sessions. Ordinary requests never extend expiry. |
| [apps/gateway/app/catalog_cache.py](../apps/gateway/app/catalog_cache.py) | Bounded per-process cache of MCP tool definitions, never tool results. |
| [apps/gateway/app/chat.py](../apps/gateway/app/chat.py) | Bounded Groq tool loop under the logged-in user's identity; progress, recovery and safe failures. |
| [apps/gateway/app/chat_context.py](../apps/gateway/app/chat_context.py) | Small deterministic context; saved history remains in PostgreSQL. |
| [apps/gateway/app/chat_store.py](../apps/gateway/app/chat_store.py) | Durable idempotent chat turns, progress/final replay and interruption handling. |
| [apps/gateway/app/chat_tool_search.py](../apps/gateway/app/chat_tool_search.py) | Bounded tool definitions and on-demand search for one chat turn. |
| [apps/gateway/app/chats.py](../apps/gateway/app/chats.py) | Owner-scoped conversation list/create/read/delete endpoints. |
| [apps/gateway/app/config.py](../apps/gateway/app/config.py) | Database configuration from environment; refuses absent required credentials. |
| [apps/gateway/app/core.py](../apps/gateway/app/core.py) | Shared tool listing, enabled/grant checks, qualified routing, audit and failure isolation. |
| [apps/gateway/app/database.py](../apps/gateway/app/database.py) | Bounded async SQLAlchemy engine/session pool, timeouts and UTC behavior. |
| [apps/gateway/app/llm.py](../apps/gateway/app/llm.py) | Groq HTTP adapter, tool schema/name mapping, bounded output and safe provider errors. |
| [apps/gateway/app/llm_metrics.py](../apps/gateway/app/llm_metrics.py) | Non-sensitive provider diagnostics. Never return raw provider error text. |
| [apps/gateway/app/main.py](../apps/gateway/app/main.py) | FastAPI composition/lifespan, initial bootstrap, MCP route, probes and cleanup shutdown. |
| [apps/gateway/app/managed_servers.py](../apps/gateway/app/managed_servers.py) | Keep store-generated MCP identities under the store lifecycle. |
| [apps/gateway/app/managed_upstreams.py](../apps/gateway/app/managed_upstreams.py) | Authorize store-bound MCP connections without broadening the private URL policy. |
| [apps/gateway/app/mcp_api.py](../apps/gateway/app/mcp_api.py) | Inbound authenticated stateless Streamable HTTP MCP; request limits and rebinding defenses. |
| [apps/gateway/app/media.py](../apps/gateway/app/media.py) | Bounded image validation/upload via the same authorized and audited tool path. |
| [apps/gateway/app/media_scope.py](../apps/gateway/app/media_scope.py) | Select an authorized image destination and keep product writes on it. |
| [apps/gateway/app/model_budget.py](../apps/gateway/app/model_budget.py) | Per-message model budget; estimates are separate from provider quotas. |
| [apps/gateway/app/models.py](../apps/gateway/app/models.py) | Gateway's 17 persistent models, ownership constraints, uniqueness and indexes. |
| [apps/gateway/app/monitoring.py](../apps/gateway/app/monitoring.py) | Admin monitoring from durable audit/job rows. No upstream probes or writes. |
| [apps/gateway/app/observability.py](../apps/gateway/app/observability.py) | Bounded diagnostic fields; credentials, arguments and exception text stay out. |
| [apps/gateway/app/permissions.py](../apps/gateway/app/permissions.py) | Administrator tool-grant listing/add/remove operations; no implicit member access. |
| [apps/gateway/app/rate_limits.py](../apps/gateway/app/rate_limits.py) | Database-backed quotas shared by gateway processes and all user API keys. |
| [apps/gateway/app/registry.py](../apps/gateway/app/registry.py) | Initial upstream bootstrap and safe discovery/status handling. |
| [apps/gateway/app/security.py](../apps/gateway/app/security.py) | Argon2, signed token handling, random key generation/hash and Fernet credential encryption. |
| [apps/gateway/app/servers.py](../apps/gateway/app/servers.py) | Administrator generic upstream CRUD/check; encrypted optional static credentials. |
| [apps/gateway/app/store_access.py](../apps/gateway/app/store_access.py) | Public Traefik ForwardAuth gate. Never returns account or store data. |
| [apps/gateway/app/store_access_guard.py](../apps/gateway/app/store_access_guard.py) | Verify the exact public maintenance gate through a live provisioning claim. |
| [apps/gateway/app/store_claim_api.py](../apps/gateway/app/store_claim_api.py) | Read one fixed store PVC, including Pending claims, under a live job. |
| [apps/gateway/app/store_claim_stage.py](../apps/gateway/app/store_claim_stage.py) | Read, record and recheck one existing PVC, including Pending storage. |
| [apps/gateway/app/store_claims.py](../apps/gateway/app/store_claims.py) | Identify existing store claims, including partially provisioned storage. |
| [apps/gateway/app/store_cleanup.py](../apps/gateway/app/store_cleanup.py) | One guarded cleanup pass; terminal completion remains a separate transaction. |
| [apps/gateway/app/store_cluster_scope.py](../apps/gateway/app/store_cluster_scope.py) | Pure Kubernetes identities; callers must separately validate live job claims. |
| [apps/gateway/app/store_completion.py](../apps/gateway/app/store_completion.py) | Private worker completion transactions; external checks happen beforehand. |
| [apps/gateway/app/store_credentials.py](../apps/gateway/app/store_credentials.py) | Generate and validate private store credentials without I/O. |
| [apps/gateway/app/store_execution.py](../apps/gateway/app/store_execution.py) | Concrete worker handlers; construction starts no worker or external operation. |
| [apps/gateway/app/store_guarded_helm.py](../apps/gateway/app/store_guarded_helm.py) | Private Helm calls under a live store claim and verified resource identities. |
| [apps/gateway/app/store_guarded_kubernetes.py](../apps/gateway/app/store_guarded_kubernetes.py) | Bind a shared Kubernetes connection to one checked private worker job. |
| [apps/gateway/app/store_guarded_secrets.py](../apps/gateway/app/store_guarded_secrets.py) | Private, claim-checked access to one store's credentials Secret. |
| [apps/gateway/app/store_helm_config.py](../apps/gateway/app/store_helm_config.py) | Fixed Helm inputs for one store; no files, processes or network calls. |
| [apps/gateway/app/store_helm_plan.py](../apps/gateway/app/store_helm_plan.py) | Build per-store Helm overrides; no database, subprocess or Kubernetes calls. |
| [apps/gateway/app/store_helm_process.py](../apps/gateway/app/store_helm_process.py) | Private Linux subprocess lifecycle for Helm; this module grants no job access. |
| [apps/gateway/app/store_helm_release.py](../apps/gateway/app/store_helm_release.py) | Bounded release observations, saved identity/values, supported revision/state and original bootstrap configuration; never authorization or Ready by themselves. |
| [apps/gateway/app/store_helm_recovery.py](../apps/gateway/app/store_helm_recovery.py) | Verify and atomically fence an abandoned pending release before value-preserving Helm reconciliation on a replacement attempt. |
| [apps/gateway/app/store_helm_stage.py](../apps/gateway/app/store_helm_stage.py) | Install, inspect or reconcile an owned interrupted managed release under a live worker claim. |
| [apps/gateway/app/store_helm_workspace.py](../apps/gateway/app/store_helm_workspace.py) | Private temporary Helm files; projected credentials remain in their mount. |
| [apps/gateway/app/store_job_guard.py](../apps/gateway/app/store_job_guard.py) | Short-lived worker claim observations; no Kubernetes calls or lease renewal. |
| [apps/gateway/app/store_jobs.py](../apps/gateway/app/store_jobs.py) | Private PostgreSQL job claims; this module performs no Kubernetes work. |
| [apps/gateway/app/store_kube_connection.py](../apps/gateway/app/store_kube_connection.py) | Private, fixed-route Kubernetes transport; no worker starts on import. |
| [apps/gateway/app/store_lease_runner.py](../apps/gateway/app/store_lease_runner.py) | Run one private job stage while renewing its existing worker lease. |
| [apps/gateway/app/store_lifecycle.py](../apps/gateway/app/store_lifecycle.py) | Browser lifecycle requests; external cleanup stays in the private worker. |
| [apps/gateway/app/store_mcp_verification.py](../apps/gateway/app/store_mcp_verification.py) | Private discovery checks for a provisioned store's two disabled MCP entries. |
| [apps/gateway/app/store_namespace.py](../apps/gateway/app/store_namespace.py) | Pure namespace checks; callers must separately validate the live job claim. |
| [apps/gateway/app/store_namespace_api.py](../apps/gateway/app/store_namespace_api.py) | Owned namespace operations; callers must hold a live worker claim. |
| [apps/gateway/app/store_namespace_binding.py](../apps/gateway/app/store_namespace_binding.py) | Save an observed namespace UID under a live private worker claim. |
| [apps/gateway/app/store_namespace_stage.py](../apps/gateway/app/store_namespace_stage.py) | Private namespace stages; these do not install a store or mark it ready. |
| [apps/gateway/app/store_provisioning.py](../apps/gateway/app/store_provisioning.py) | Prepare one managed store; the worker commits Ready after this returns. |
| [apps/gateway/app/store_readiness.py](../apps/gateway/app/store_readiness.py) | Pure checks for the fixed store workloads; no service calls or state changes. |
| [apps/gateway/app/store_registration.py](../apps/gateway/app/store_registration.py) | Private worker registration of the two MCP services for a managed store. |
| [apps/gateway/app/store_requests.py](../apps/gateway/app/store_requests.py) | Queue authenticated store creation; Kubernetes work runs in the worker. |
| [apps/gateway/app/store_scheduling.py](../apps/gateway/app/store_scheduling.py) | Select store work fairly under the shared PostgreSQL admission lock. |
| [apps/gateway/app/store_secret.py](../apps/gateway/app/store_secret.py) | Pure definitions for the fixed credentials Secret of one managed store. |
| [apps/gateway/app/store_secret_api.py](../apps/gateway/app/store_secret_api.py) | Read or create an owned immutable Secret through a live job guard. |
| [apps/gateway/app/store_secret_binding.py](../apps/gateway/app/store_secret_binding.py) | Bind a validated credentials Secret UID before installing store workloads. |
| [apps/gateway/app/store_secret_stage.py](../apps/gateway/app/store_secret_stage.py) | Prepare and bind store credentials before any workload installation. |
| [apps/gateway/app/store_settings.py](../apps/gateway/app/store_settings.py) | Shared PostgreSQL settings for managed store provisioning. |
| [apps/gateway/app/store_settings_api.py](../apps/gateway/app/store_settings_api.py) | Admin controls for PostgreSQL-backed store provisioning settings. |
| [apps/gateway/app/store_storage_records.py](../apps/gateway/app/store_storage_records.py) | Read saved PVC/PV identities under one live private worker claim. |
| [apps/gateway/app/store_volume_api.py](../apps/gateway/app/store_volume_api.py) | Read one store's bound PVC/PV identifiers under a live private job claim. |
| [apps/gateway/app/store_volume_binding.py](../apps/gateway/app/store_volume_binding.py) | Persist verified store volume identifiers under a live private worker claim. |
| [apps/gateway/app/store_volume_inventory.py](../apps/gateway/app/store_volume_inventory.py) | Cleanup-only volume inventory; observations do not prove disk deletion. |
| [apps/gateway/app/store_volume_pages.py](../apps/gateway/app/store_volume_pages.py) | Bounded PV inventory pagination; no HTTP, database or deletion. |
| [apps/gateway/app/store_volume_recovery.py](../apps/gateway/app/store_volume_recovery.py) | Identify store PVs during recovery without requiring a surviving PVC. |
| [apps/gateway/app/store_volume_stage.py](../apps/gateway/app/store_volume_stage.py) | Read, save and recheck the two bound volumes of a provisioning job. |
| [apps/gateway/app/store_volumes.py](../apps/gateway/app/store_volumes.py) | Pure identity checks for the local-path volumes of one managed store. |
| [apps/gateway/app/store_wordpress_setup.py](../apps/gateway/app/store_wordpress_setup.py) | Owner-authorized password setup. No WordPress password enters the gateway. |
| [apps/gateway/app/store_wordpress_verification.py](../apps/gateway/app/store_wordpress_verification.py) | Private read-only WordPress probes for one provisioned managed store. |
| [apps/gateway/app/store_worker.py](../apps/gateway/app/store_worker.py) | Private concurrent job loop; real provisioning handlers are supplied separately. |
| [apps/gateway/app/store_worker_health.py](../apps/gateway/app/store_worker_health.py) | Local scheduler heartbeat. Probes perform no network or database requests. |
| [apps/gateway/app/store_worker_main.py](../apps/gateway/app/store_worker_main.py) | Explicit worker entry point; imports and package checks start no services. |
| [apps/gateway/app/store_workload_api.py](../apps/gateway/app/store_workload_api.py) | Collect one store's workload readiness through bounded, claim-checked reads. |
| [apps/gateway/app/stores.py](../apps/gateway/app/stores.py) | Authenticated reads for stores managed by the provisioner. |
| [apps/gateway/app/tool_selection.py](../apps/gateway/app/tool_selection.py) | Search the current user's permitted MCP tools without loading every schema. |
| [apps/gateway/app/tools.py](../apps/gateway/app/tools.py) | Browser tool list/call endpoints; release authentication DB work before long upstream I/O. |
| [apps/gateway/app/upstream_security.py](../apps/gateway/app/upstream_security.py) | Outbound MCP destination policy and DNS-pinned HTTP transport. |
| [apps/gateway/app/upstreams.py](../apps/gateway/app/upstreams.py) | Outbound MCP sessions, destination preparation, per-call bounds and discovery validation. |
| [apps/gateway/app/usage_limits.py](../apps/gateway/app/usage_limits.py) | Admin-managed quotas; reads are uncached and edits are audited. |
| [apps/gateway/app/users.py](../apps/gateway/app/users.py) | Administrator team listing/invitations and invitation acceptance. |

## L. Every dashboard source component

| Source file | Responsibility |
| --- | --- |
| [apps/dashboard/src/api.js](../apps/dashboard/src/api.js) | Same-origin API requests, CSRF/auth/error handling shared by dashboard features. |
| [apps/dashboard/src/App.jsx](../apps/dashboard/src/App.jsx) | Application auth state and login/workspace/invitation entry flow. |
| [apps/dashboard/src/components/AcceptInvite.jsx](../apps/dashboard/src/components/AcceptInvite.jsx) | Member invitation acceptance and own initial password form. |
| [apps/dashboard/src/components/ApiKeys.jsx](../apps/dashboard/src/components/ApiKeys.jsx) | Own key generation/list/revocation; plaintext shown only at issuance. |
| [apps/dashboard/src/components/Audit.jsx](../apps/dashboard/src/components/Audit.jsx) | Audit table/filtering and correct actor/source/outcome presentation. |
| [apps/dashboard/src/components/AuthFrame.jsx](../apps/dashboard/src/components/AuthFrame.jsx) | Shared sign-in/invitation page layout. |
| [apps/dashboard/src/components/Chat.jsx](../apps/dashboard/src/components/Chat.jsx) | Conversation UI, messages, uploads, request progress and durable turn recovery. |
| [apps/dashboard/src/components/ChatRunStatus.jsx](../apps/dashboard/src/components/ChatRunStatus.jsx) | Tool progress and active chat execution status. |
| [apps/dashboard/src/components/ConfirmDialog.jsx](../apps/dashboard/src/components/ConfirmDialog.jsx) | Accessible explicit confirmation UI for user actions. |
| [apps/dashboard/src/components/CreateStore.jsx](../apps/dashboard/src/components/CreateStore.jsx) | Store display name/address input, request submission and recovery feedback. |
| [apps/dashboard/src/components/Icon.jsx](../apps/dashboard/src/components/Icon.jsx) | Shared application icon renderer. |
| [apps/dashboard/src/components/ImageDestination.jsx](../apps/dashboard/src/components/ImageDestination.jsx) | Explicit authorized store/media destination selection. |
| [apps/dashboard/src/components/MemberKeys.jsx](../apps/dashboard/src/components/MemberKeys.jsx) | Administrator issue/revoke interface for a member's gateway keys. |
| [apps/dashboard/src/components/MemberPermissions.jsx](../apps/dashboard/src/components/MemberPermissions.jsx) | Administrator member tool permissions, grouped for management. |
| [apps/dashboard/src/components/Monitoring.jsx](../apps/dashboard/src/components/Monitoring.jsx) | Administrator metric cards, chart, upstream table, alerts and refresh controls. |
| [apps/dashboard/src/components/PermissionGroups.jsx](../apps/dashboard/src/components/PermissionGroups.jsx) | Grouped server/store tool-permission presentation. |
| [apps/dashboard/src/components/ServerForm.jsx](../apps/dashboard/src/components/ServerForm.jsx) | Administrator upstream URL, identity, timeout and credential form. |
| [apps/dashboard/src/components/Servers.jsx](../apps/dashboard/src/components/Servers.jsx) | Registered server list, state, discovery and enable/edit/remove controls. |
| [apps/dashboard/src/components/SessionStatus.jsx](../apps/dashboard/src/components/SessionStatus.jsx) | Idle-expiry status/renewal prompts and sign-out feedback. |
| [apps/dashboard/src/components/sessionTiming.js](../apps/dashboard/src/components/sessionTiming.js) | Client-side session timing calculations; server remains authoritative. |
| [apps/dashboard/src/components/StoreActions.jsx](../apps/dashboard/src/components/StoreActions.jsx) | Owner/admin delete and administrator enable/disable controls with confirmation. |
| [apps/dashboard/src/components/StoreProgress.jsx](../apps/dashboard/src/components/StoreProgress.jsx) | Visible provisioning/lifecycle progress while the backend job runs. |
| [apps/dashboard/src/components/Stores.jsx](../apps/dashboard/src/components/Stores.jsx) | User store inventory/detail, URL, status, operation and setup controls. |
| [apps/dashboard/src/components/StoreSettings.jsx](../apps/dashboard/src/components/StoreSettings.jsx) | Administrator creation switch, quota/concurrency/timeout settings. |
| [apps/dashboard/src/components/UsageLimits.jsx](../apps/dashboard/src/components/UsageLimits.jsx) | Administrator rate-limit configuration and revision handling. |
| [apps/dashboard/src/components/useConfirmation.js](../apps/dashboard/src/components/useConfirmation.js) | Reusable asynchronous confirmation state for user actions. |
| [apps/dashboard/src/components/useDashboardSession.js](../apps/dashboard/src/components/useDashboardSession.js) | Activity/session polling/renewal and navigation cleanup behavior. |
| [apps/dashboard/src/components/Users.jsx](../apps/dashboard/src/components/Users.jsx) | Administrator members, invitations, activation, keys and permissions. |
| [apps/dashboard/src/components/WordPressSetup.jsx](../apps/dashboard/src/components/WordPressSetup.jsx) | Owner-only setup launch and validated form destination; no password sent to gateway. |
| [apps/dashboard/src/components/Workspace.jsx](../apps/dashboard/src/components/Workspace.jsx) | Role-aware navigation and page composition, including scrollable small-screen sidebar. |
| [apps/dashboard/src/index.css](../apps/dashboard/src/index.css) | Shared visual styling and layout. |
| [apps/dashboard/src/main.jsx](../apps/dashboard/src/main.jsx) | React application mount and global CSS import. |
| [apps/dashboard/src/monitoring.js](../apps/dashboard/src/monitoring.js) | Strict bounded monitoring response validation and sequential polling lifecycle. |
| [apps/dashboard/src/permissionGroups.js](../apps/dashboard/src/permissionGroups.js) | Pure grouping of tool permissions by upstream/store. |
| [apps/dashboard/src/storeCreation.js](../apps/dashboard/src/storeCreation.js) | Validation, secure UUID and account-scoped recovery for lost creation replies. |
| [apps/dashboard/src/storeDeletion.js](../apps/dashboard/src/storeDeletion.js) | Validated deletion requests and safe lifecycle acknowledgement handling. |
| [apps/dashboard/src/storeMonitor.js](../apps/dashboard/src/storeMonitor.js) | Store polling/progress handling with timeout/cancellation/stale-result protection. |
| [apps/dashboard/src/uuid.js](../apps/dashboard/src/uuid.js) | Native secure UUID or secure random fallback; no weak random idempotency keys. |
| [apps/dashboard/src/wordpressSetup.js](../apps/dashboard/src/wordpressSetup.js) | Owner eligibility, capability/URL validation and safe password-setup handoff. |

## M. Every Helm/chart/configuration file

| Chart file | Resource kinds / type | Responsibility |
| --- | --- | --- |
| [charts/platform/templates/ingress-default-certificate.yaml](../charts/platform/templates/ingress-default-certificate.yaml) | TLSStore | Optional default certificate reference to an existing platform TLS Secret. |
| [charts/platform/templates/production-validation.yaml](../charts/platform/templates/production-validation.yaml) | Validation | Reject unsafe production hosts, cookies/TLS, image references, quotas and enabled-worker endpoint placeholders. |
| [charts/platform/values.schema.json](../charts/platform/values.schema.json) | JSON Schema | Validate platform value types and production combinations. |
| [charts/store/templates/production-validation.yaml](../charts/store/templates/production-validation.yaml) | Validation | Check production store origin, TLS, images, storage and managed settings. |
| [charts/store/values.schema.json](../charts/store/values.schema.json) | JSON Schema | Validate store configuration and production settings. |
| [charts/platform/.helmignore](../charts/platform/.helmignore) | Configuration/helper | Exclude non-chart files from Helm packaging. |
| [charts/platform/Chart.yaml](../charts/platform/Chart.yaml) | Configuration/helper | Helm chart identity and version metadata; application image versions are configured separately. |
| [charts/platform/files/postgres-init.sh](../charts/platform/files/postgres-init.sh) | Configuration/helper | Initialize separate gateway/Notes DBs and roles; restrict PUBLIC access. |
| [charts/platform/templates/_store-provisioner.tpl](../charts/platform/templates/_store-provisioner.tpl) | Configuration/helper | Release/platform-derived names and reusable provisioner helpers. |
| [charts/platform/templates/apps-ingress.yaml](../charts/platform/templates/apps-ingress.yaml) | Ingress | App host: /api and /mcp to gateway, / to dashboard. |
| [charts/platform/templates/apps-network-policy.yaml](../charts/platform/templates/apps-network-policy.yaml) | NetworkPolicy | Application ingress/egress, gateway database/Notes/approved upstream paths. |
| [charts/platform/templates/dashboard.yaml](../charts/platform/templates/dashboard.yaml) | Service, Deployment | Static frontend Service/Deployment, probes/resources/security context. |
| [charts/platform/templates/default-network-policies.yaml](../charts/platform/templates/default-network-policies.yaml) | NetworkPolicy, NetworkPolicy, NetworkPolicy, NetworkPolicy | Platform default-deny, DNS and required platform network paths. |
| [charts/platform/templates/gateway.yaml](../charts/platform/templates/gateway.yaml) | ConfigMap, Service, Deployment | Gateway configuration including managed domain/scheme, Secret references, migration init, probes and resources. |
| [charts/platform/templates/managed-store-network-policy.yaml](../charts/platform/templates/managed-store-network-policy.yaml) | NetworkPolicy | Gateway connectivity to legitimate managed-store MCP endpoints. |
| [charts/platform/templates/mcp-notes.yaml](../charts/platform/templates/mcp-notes.yaml) | Service, Deployment, NetworkPolicy | Notes Service/Deployment and database network access. |
| [charts/platform/templates/postgres.yaml](../charts/platform/templates/postgres.yaml) | ConfigMap, Service, StatefulSet, NetworkPolicy | Initialization ConfigMap, headless Service, StatefulSet/PVC and network access. |
| [charts/platform/templates/service-accounts.yaml](../charts/platform/templates/service-accounts.yaml) | ServiceAccount | Application service accounts without automatic API token mounting. |
| [charts/platform/templates/store-provisioner-access.yaml](../charts/platform/templates/store-provisioner-access.yaml) | ClusterRoleBinding | Opt-in namespace-management role binding for the private provisioner. |
| [charts/platform/templates/store-provisioner-namespace-policy.yaml](../charts/platform/templates/store-provisioner-namespace-policy.yaml) | ValidatingAdmissionPolicy, ValidatingAdmissionPolicyBinding | Admission checks for provisioner-created/modified namespace ownership. |
| [charts/platform/templates/store-provisioner-network-policy.yaml](../charts/platform/templates/store-provisioner-network-policy.yaml) | NetworkPolicy, NetworkPolicy | Worker API/service verification/DB network routes with exact configured API ranges. |
| [charts/platform/templates/store-provisioner-rbac.yaml](../charts/platform/templates/store-provisioner-rbac.yaml) | ServiceAccount, ClusterRole, ClusterRole | Worker SA and restricted namespace/workload ClusterRoles. |
| [charts/platform/templates/store-provisioner-rolebinding-policy.yaml](../charts/platform/templates/store-provisioner-rolebinding-policy.yaml) | ValidatingAdmissionPolicy, ValidatingAdmissionPolicyBinding | Admission checks limiting scoped RoleBindings to intended managed ownership/role/subject. |
| [charts/platform/templates/store-provisioner-storage.yaml](../charts/platform/templates/store-provisioner-storage.yaml) | ClusterRole, ClusterRoleBinding | PV get/list-only ClusterRole and opt-in binding; no worker PV delete. |
| [charts/platform/templates/store-provisioner.yaml](../charts/platform/templates/store-provisioner.yaml) | ConfigMap, Deployment | Private worker Deployment, settings/profile mounts, projected token/CA and heartbeat probes. |
| [charts/platform/templates/wordpress-setup-network-policy.yaml](../charts/platform/templates/wordpress-setup-network-policy.yaml) | NetworkPolicy | Allow managed WordPress setup return path to gateway. |
| [charts/platform/values-apps-local.yaml](../charts/platform/values-apps-local.yaml) | Configuration/helper | Local gateway/dashboard/host/provider/cache references and application budgets. |
| [charts/platform/values-local.yaml](../charts/platform/values-local.yaml) | Configuration/helper | Local PostgreSQL storage profile. |
| [charts/platform/values-prod.yaml](../charts/platform/values-prod.yaml) | Configuration/helper | Independent production example: domains, TLS, secure cookies, images, storage, network ranges, existing Secrets and staged worker profile. |
| [charts/platform/values-provisioner-local.yaml](../charts/platform/values-provisioner-local.yaml) | Configuration/helper | Explicit local worker enablement, image, API endpoint ranges and RBAC switches. |
| [charts/platform/values.yaml](../charts/platform/values.yaml) | Configuration/helper | Base platform DB, Notes, network, cache and disabled-worker defaults. |
| [charts/store/.helmignore](../charts/store/.helmignore) | Configuration/helper | Exclude non-chart files from Helm packaging. |
| [charts/store/Chart.yaml](../charts/store/Chart.yaml) | Configuration/helper | Helm chart identity and version metadata; application image versions are configured separately. |
| [charts/store/files/bootstrap.sh](../charts/store/files/bootstrap.sh) | Configuration/helper | Bounded initial WordPress/Woo setup and ordered key/media/seed execution. |
| [charts/store/files/keys.php](../charts/store/files/keys.php) | Configuration/helper | Verify/create independent Woo keys with safe serialization and replay/conflict checks. |
| [charts/store/files/media.php](../charts/store/files/media.php) | Configuration/helper | Verify/create restricted media account/application password without rotating committed credentials. |
| [charts/store/files/seed.sh](../charts/store/files/seed.sh) | Configuration/helper | Site and customer-account settings after credentials are verified; no product seeding. |
| [charts/store/files/wordpress-setup.php](../charts/store/files/wordpress-setup.php) | Configuration/helper | Owner capability form/redemption and password/title update inside WordPress. |
| [charts/store/templates/bootstrap-configmap.yaml](../charts/store/templates/bootstrap-configmap.yaml) | ConfigMap | Mount bootstrap, seed and PHP helper source. |
| [charts/store/templates/bootstrap-job.yaml](../charts/store/templates/bootstrap-job.yaml) | Job | Durable managed bootstrap or static-store hook, credentials, content volume, original deadline and bounded retries. |
| [charts/store/templates/default-network-policies.yaml](../charts/store/templates/default-network-policies.yaml) | NetworkPolicy, NetworkPolicy, NetworkPolicy, NetworkPolicy, NetworkPolicy, NetworkPolicy, NetworkPolicy, NetworkPolicy | Default-deny and required same-store WordPress/MySQL/HTTP/DNS paths. |
| [charts/store/templates/mcp-analytics.yaml](../charts/store/templates/mcp-analytics.yaml) | Service, Deployment, NetworkPolicy | Read-only analytics Service/Deployment and gateway ingress allowance. |
| [charts/store/templates/mysql.yaml](../charts/store/templates/mysql.yaml) | Service, StatefulSet | Per-store MySQL ClusterIP Service, StatefulSet and claim template. |
| [charts/store/templates/provisioner-network-policy.yaml](../charts/store/templates/provisioner-network-policy.yaml) | NetworkPolicy, NetworkPolicy | Private worker inspection/discovery paths to owned store services. |
| [charts/store/templates/service-accounts.yaml](../charts/store/templates/service-accounts.yaml) | ServiceAccount | Separate tokenless store workload service accounts. |
| [charts/store/templates/store-access.yaml](../charts/store/templates/store-access.yaml) | Middleware | Exact store-scoped Traefik ForwardAuth Middleware. |
| [charts/store/templates/store-guardrails.yaml](../charts/store/templates/store-guardrails.yaml) | ResourceQuota, LimitRange | Per-store ResourceQuota and container/PVC LimitRanges. |
| [charts/store/templates/woo-mcp-network-policy.yaml](../charts/store/templates/woo-mcp-network-policy.yaml) | NetworkPolicy | Gateway access to store's Woo MCP without public exposure. |
| [charts/store/templates/woo-mcp.yaml](../charts/store/templates/woo-mcp.yaml) | Service, Deployment | Custom Woo MCP Service/Deployment and server-side credential references. |
| [charts/store/templates/wordpress-setup.yaml](../charts/store/templates/wordpress-setup.yaml) | ConfigMap, NetworkPolicy | Read-only setup plugin ConfigMap and required network path. |
| [charts/store/templates/wordpress.yaml](../charts/store/templates/wordpress.yaml) | PersistentVolumeClaim, Service, Deployment, Ingress | Content PVC, WordPress Service/Deployment/Ingress and external setup-plugin mount. |
| [charts/store/values-local.yaml](../charts/store/values-local.yaml) | Configuration/helper | Original local storefront host. |
| [charts/store/values-managed.yaml](../charts/store/values-managed.yaml) | Configuration/helper | Per-store quotas and resource/PVC ranges; combined with generated owned store overrides. |
| [charts/store/values-prod.yaml](../charts/store/values-prod.yaml) | Configuration/helper | Independent HTTPS example: public host/images, 10Gi volumes and matching managed quotas. |
| [charts/store/values.yaml](../charts/store/values.yaml) | Configuration/helper | Store images, plugin pin, services, resource defaults and optional setup/access flags. |

Conditional templates do not mean every resource exists in every release. The worker creates managed namespaces/Secret/scoped RoleBinding through guarded API operations, then installs the store chart. PersistentVolumes come from the storage controller, not from a handwritten PV template. Traefik CRDs, CoreDNS, the API server and local-path infrastructure are cluster prerequisites, not applications built by these two charts.

## N. Operator scripts

| Script | Role |
| --- | --- |
| [scripts/gateway-local-env.sh](../scripts/gateway-local-env.sh) | Load local gateway development environment for user-run tests; never include its real secret values in documentation. |
| [scripts/gateway-secrets.sh](../scripts/gateway-secrets.sh) | Prepare gateway credential Secrets for operator use; inspect environment and scope before running. |
| [scripts/install-store-access.py](../scripts/install-store-access.py) | Preview/apply an existing store's scoped maintenance gate with identity fencing. |
| [scripts/install-store-wordpress-setup.py](../scripts/install-store-wordpress-setup.py) | Preview/apply compatible setup plugin/workload configuration for an existing store; passwords unchanged on installation. |
| [scripts/platform-secrets.sh](../scripts/platform-secrets.sh) | Prepare platform database credential Secrets and related initialization inputs. |
| [scripts/secrets.sh](../scripts/secrets.sh) | Prepare the original store's credential Secret references/material. |
| [scripts/verify-chart-security.py](../scripts/verify-chart-security.py) | Render charts and assert required/denied routes, isolation and runtime security settings. |
| [scripts/verify-cluster-security.py](../scripts/verify-cluster-security.py) | User-run cluster verification of security expectations; inspect its flags before live use. |
| [scripts/verify-provisioner-admission.py](../scripts/verify-provisioner-admission.py) | Verify worker admission restrictions; cluster-policy testing is separate from pure rendering. |
| [scripts/verify-provisioner-chart.py](../scripts/verify-provisioner-chart.py) | Verify provisioner chart settings/resources and constrained configuration. |
| [scripts/verify-provisioner-worker-rbac.py](../scripts/verify-provisioner-worker-rbac.py) | Verify intended and denied worker Kubernetes API permissions. |

These scripts are indexed for understanding; writing this design did not run them or change the cluster, database, stores or running containers. Preview/apply installers are deliberately distinct from provisioning new stores.

## O. Build and dependency boundaries

| Build file | Role |
| --- | --- |
| [apps/dashboard/Dockerfile](../apps/dashboard/Dockerfile) | Node 22 npm-ci build runs lint/Vite; final image serves static assets with unprivileged Nginx. |
| [apps/gateway/Dockerfile](../apps/gateway/Dockerfile) | Python gateway image, frozen dependencies, application and Alembic sources; non-root runtime. |
| [apps/gateway/Dockerfile.provisioner](../apps/gateway/Dockerfile.provisioner) | Python worker plus checksum-verified Helm 3.22.0 and bundled store chart; no Docker daemon/socket needed. |
| [apps/gateway/Dockerfile.provisioner.dockerignore](../apps/gateway/Dockerfile.provisioner.dockerignore) | Restrict build context; excludes unrelated/secret/local artifacts from worker image context. |
| [apps/mcp-analytics/Dockerfile](../apps/mcp-analytics/Dockerfile) | Containerize custom read-only analytics MCP service. |
| [apps/mcp-notes/Dockerfile](../apps/mcp-notes/Dockerfile) | Containerize custom Notes MCP service. |
| [apps/woo-mcp/Dockerfile](../apps/woo-mcp/Dockerfile) | Containerize custom WooCommerce REST/media MCP service. |

| Additional implementation/configuration | Responsibility |
| --- | --- |
| [apps/woo-mcp/main.py](../apps/woo-mcp/main.py) | All 12 locally authored Woo product/order/media tools and HTTP service lifecycle. |
| [apps/mcp-analytics/main.py](../apps/mcp-analytics/main.py) | Three locally authored read-only analytics tools, timezone/date handling and bounded REST pagination. |
| [apps/mcp-notes/main.py](../apps/mcp-notes/main.py) | Four locally authored Notes tools, SQL pool/schema initialization and health/readiness. |
| `apps/mcp-analytics/src/mcp_analytics/__init__.py`, `apps/mcp-notes/src/mcp_notes/__init__.py` | Package markers for the custom service distributions. |
| `apps/*/pyproject.toml`, `apps/*/uv.lock` | Python dependency declarations and reproducible resolved versions for each Python application. |
| [apps/dashboard/package.json](../apps/dashboard/package.json), [apps/dashboard/package-lock.json](../apps/dashboard/package-lock.json) | Frontend scripts/dependencies and npm-ci lockfile. |
| [apps/dashboard/vite.config.js](../apps/dashboard/vite.config.js), [apps/dashboard/index.html](../apps/dashboard/index.html) | Build entry/configuration; compiled assets become the static frontend. |
| [apps/dashboard/nginx.conf](../apps/dashboard/nginx.conf) | Static serving, health route, asset cache headers and SPA fallback; API/MCP remain ingress-routed. |
| [apps/dashboard/.dockerignore](../apps/dashboard/.dockerignore) and application ignore files | Keep dependency caches/unrelated files out of image contexts. |

Store source pins WordPress 7.1.3/PHP 8.3, WooCommerce 11.2.1, WP-CLI 2.12.0/PHP 8.3 and MySQL 8.4. The provisioner 0.1.9 rollout at platform revision 37 was followed by the successful parallel restart test. Image tags in production values are build/deployment examples.

## P. Alembic migration inventory

| Revision ← parent | Migration source | Purpose |
| --- | --- | --- |
| `8bd39ee59e3f` ← `None` | [apps/gateway/migrations/versions/8bd39ee59e3f_initial_gateway_schema.py](../apps/gateway/migrations/versions/8bd39ee59e3f_initial_gateway_schema.py) | initial gateway schema |
| `4c9b6f10a2d1` ← `8bd39ee59e3f` | [apps/gateway/migrations/versions/4c9b6f10a2d1_registry_bootstrap.py](../apps/gateway/migrations/versions/4c9b6f10a2d1_registry_bootstrap.py) | Persist registry bootstrap state. |
| `61d5e40b8a72` ← `4c9b6f10a2d1` | [apps/gateway/migrations/versions/61d5e40b8a72_member_tool_grants.py](../apps/gateway/migrations/versions/61d5e40b8a72_member_tool_grants.py) | Add member tool grants, denied by default. |
| `a7f24e6910cb` ← `61d5e40b8a72` | [apps/gateway/migrations/versions/a7f24e6910cb_chat_history.py](../apps/gateway/migrations/versions/a7f24e6910cb_chat_history.py) | Persist private chat conversations and execution results. |
| `b8d16a29e504` ← `a7f24e6910cb` | [apps/gateway/migrations/versions/b8d16a29e504_admin_account_access.py](../apps/gateway/migrations/versions/b8d16a29e504_admin_account_access.py) | Invalidate disabled-account sessions and record administrative access changes. |
| `c2e90f7348ad` ← `b8d16a29e504` | [apps/gateway/migrations/versions/c2e90f7348ad_browser_sessions.py](../apps/gateway/migrations/versions/c2e90f7348ad_browser_sessions.py) | Add persistent dashboard sessions with idle and absolute expiry. |
| `d4e91b70a6f2` ← `c2e90f7348ad` | [apps/gateway/migrations/versions/d4e91b70a6f2_usage_limits.py](../apps/gateway/migrations/versions/d4e91b70a6f2_usage_limits.py) | Add admin-managed request limits and shared quota counters. |
| `e8c31d52a904` ← `d4e91b70a6f2` | [apps/gateway/migrations/versions/e8c31d52a904_chat_deletion.py](../apps/gateway/migrations/versions/e8c31d52a904_chat_deletion.py) | Delete personal chat content while retaining minimal replay markers. |
| `bd6709d1e89d` ← `e8c31d52a904` | [apps/gateway/migrations/versions/bd6709d1e89d_add_multi_store_provisioning.py](../apps/gateway/migrations/versions/bd6709d1e89d_add_multi_store_provisioning.py) | add multi-store provisioning |
| `576f196bae1f` ← `bd6709d1e89d` | [apps/gateway/migrations/versions/576f196bae1f_add_store_provisioning_settings.py](../apps/gateway/migrations/versions/576f196bae1f_add_store_provisioning_settings.py) | Add admin-managed store provisioning settings. |
| `4d2a8f19c6b0` ← `576f196bae1f` | [apps/gateway/migrations/versions/4d2a8f19c6b0_store_creation_concurrency.py](../apps/gateway/migrations/versions/4d2a8f19c6b0_store_creation_concurrency.py) | Add admin-managed parallel store creation capacity. |
| `6a18dc43f2b7` ← `4d2a8f19c6b0` | [apps/gateway/migrations/versions/6a18dc43f2b7_store_analytics_registration.py](../apps/gateway/migrations/versions/6a18dc43f2b7_store_analytics_registration.py) | Track the analytics MCP registration for each managed store. |
| `7b42e91fd0c6` ← `6a18dc43f2b7` | [apps/gateway/migrations/versions/7b42e91fd0c6_store_namespace_uid.py](../apps/gateway/migrations/versions/7b42e91fd0c6_store_namespace_uid.py) | Remember the Kubernetes namespace UID for each managed store. |
| `9c71a2d4e8f0` ← `7b42e91fd0c6` | [apps/gateway/migrations/versions/9c71a2d4e8f0_store_credentials_secret_uid.py](../apps/gateway/migrations/versions/9c71a2d4e8f0_store_credentials_secret_uid.py) | Track each managed store's credentials Secret identity. |
| `c82d6a41f093` ← `9c71a2d4e8f0` | [apps/gateway/migrations/versions/c82d6a41f093_store_volume_bindings.py](../apps/gateway/migrations/versions/c82d6a41f093_store_volume_bindings.py) | Track the PVC and PV identities belonging to each managed store. |
| `d18fa734c902` ← `c82d6a41f093` | [apps/gateway/migrations/versions/d18fa734c902_store_access.py](../apps/gateway/migrations/versions/d18fa734c902_store_access.py) | Store maintenance access, independent of ownership and lifecycle jobs. |
| `e53cb92a071d` ← `d18fa734c902` | [apps/gateway/migrations/versions/e53cb92a071d_wordpress_setup.py](../apps/gateway/migrations/versions/e53cb92a071d_wordpress_setup.py) | One-use WordPress password setup capabilities; passwords stay in WordPress. |

Source migration head(s): `e53cb92a071d`. The user confirmed `e53cb92a071d (head)` and no schema drift in the pasted cluster check. A later check is required to call that current live state. Alembic/Helm rollback compatibility is not inferred merely from having a migration history.

## Q. Verification and feature locations

| Check | Recorded result |
|---|---|
| Two-user flow | Checkout appeared in Woo admin, chat created/read store data, and the second user queried orders through Inspector |
| Shared gateway | One prompt used two upstreams, and calls appeared under the correct users |
| Access controls | A revoked key failed on its next request, disabled tools disappeared, and an unavailable upstream left others working |
| Store destination | Product and image operations reached the store selected by the member's grants |
| Two-store analytics | Both qualified analytics tools returned separately labelled order counts |
| Isolation | Own-store WordPress/MySQL Service and pod-IP checks worked, while cross-store checks failed in both directions |
| Deletion | The chosen store's namespace and recorded PVs disappeared, while the other store continued working |
| Parallel restart | Two stores reached Ready/Succeeded on attempt 2 after worker restart with saved identities and original deadlines preserved |
| Owner setup | PHP, installer, filesystem and PostgreSQL tests checked password handling, owner-only setup and session/expiry rules |
| Monitoring | Backend/PostgreSQL and frontend checks passed, followed by the working Monitoring page |
| Worker API access | PV get/list were allowed, while PV delete and platform Secret reads were denied |
| Production values | Render tests checked HTTPS settings, two-store profiles, quotas, unsafe values and saved-release compatibility |

Pure tests use fake upstreams. PostgreSQL suites use temporary schemas and real transactions/row-lock waits. Chart tests inspect rendered resources. The live checks above exercised the deployed local system.

### Mandatory, bonus and add-on locations

Gateway filenames below live in `apps/gateway/app/`; gateway tests live in `apps/gateway/tests/`.

| Group / PDF item | Implementation and representative checks |
|---|---|
| Mandatory four layers | `charts/store`; [apps/woo-mcp/main.py](../apps/woo-mcp/main.py); `core.py` / `mcp_api.py`; `apps/dashboard/src`; `chat.py` |
| Mandatory three upstreams | `apps/woo-mcp`, `apps/mcp-analytics`, `apps/mcp-notes`; `registry.py` bootstrap |
| Mandatory routing, failures, names and audit | `core.py`, `upstreams.py`, `audit.py`; `verify_upstream_mcp.py` and completed live demonstration |
| Mandatory chart/config-only production changes | Both chart values/schema/validation templates; `verify_store_production_values.py` and `verify_store_helm_config.py` |
| Bonus create/delete and automatic registration | `store_requests.py`, `store_provisioning.py`, `store_registration.py`, `store_cleanup.py`, `store_execution.py` |
| Bonus concurrent work, quota and restart recovery | `store_scheduling.py`, `store_jobs.py`, `store_lease_runner.py`, `store_helm_stage.py`, `store_helm_recovery.py`, `store_completion.py`; live parallel restart |
| Bonus saved storage and clean teardown | Claim/volume API, binding and stage modules; `store_storage_records.py`; storage/cleanup/completion suites and live deletion |
| Add-on tool permissions | `permissions.py` / `core.py`; grouped dashboard permissions; hide and direct-call denial |
| Add-on many tools | `chat_tool_search.py` / `tool_selection.py`; `verify_chat_tool_search.py` / `verify_tool_selection.py` |
| Add-on security hardening | `upstream_security.py`, `managed_upstreams.py`, `security.py`; chart RBAC/admission/policies and security scripts |
| Add-on observability | `observability.py`, `monitoring.py`, `store_worker_health.py`, `Monitoring.jsx`; `verify_observability.py` / `verify_monitoring_postgres.py` |
| Shared request limits | `rate_limits.py` / `usage_limits.py` store shared per-user limits in PostgreSQL |
| Helm image updates | Application image updates use Helm release configuration, with migrations run by the gateway init container |
| Additional controls | `store_access.py` / `store_lifecycle.py` maintenance; `store_wordpress_setup.py` / PHP plugin owner setup; `media_scope.py` selected image destination |


Other representative tests are `verify_store_installation_recovery.py`, `verify_store_recovery_chart.py`, `verify_store_provisioning.py`, `verify_store_completion.py`, `verify_store_helm_process.py`, `verify_store_volume_binding.py`, `verify_store_cleanup.py`, `verify_store_wordpress_setup.py` and frontend `verify_monitoring.mjs` / `verify_wordpress_setup.mjs` / `verify_store_creation.mjs`. Read each suite's setup/cleanup before running it; temporary-schema tests differ from pure read-only checks.

## R. Decisions and alternatives

| Decision | Why | Cost / next boundary |
| --- | --- | --- |
| PostgreSQL durable queue instead of a broker | Transactional admission, ownership and completion reuse the application DB | Polling/locks and DB availability become scaling dependencies; no Celery/RabbitMQ claimed |
| In-process schema cache instead of Redis | Small metadata, bounded memory, simple invalidation, no extra stateful system | Per-replica cache/staleness; results and permissions deliberately remain uncached |
| Separate WP/MySQL per store | Clear credentials/data/storage isolation and familiar Woo application stack | Higher fleet memory/storage cost than shared database multitenancy |
| Store UUID namespaces and saved UIDs | Recover accepted operations while rejecting replacement objects | More validation code, tombstones and explicit conflict handling |
| Separate preparation and terminal DB commit | No network call itself can mark Ready; final ownership/deadline remains checked | Evidence can change between probes, so final DB guards plus recovery are required |
| No blind external write retry | Avoid duplicate products/installations after lost acknowledgement | Caller may need an inspect/recovery action; cannot guarantee exactly-once external effects |
| Traefik maintenance gate | Immediate non-destructive store disablement and fail-closed access | Gateway/PG outage can affect public managed-store availability |
| REST business tools; WP-CLI bootstrap | Business API for runtime, application-native functions for initial setup | REST pagination and plugin/version compatibility need explicit bounds/testing |
| Version pins | Reproducible compatibility instead of automatically installing a newer incompatible Woo | Requires an intentional security/compatibility update process |
| Local-path storage | Simple local Kubernetes persistence and automatic reclaim | Single-node durability; no backup, replication or verified physical erase guarantee |


## S. Helm configuration and source walkthrough

The repository contains local and production values for both charts. The architecture diagrams describe the local k3d system. The production files describe the same resources with HTTPS, registry images and larger volumes.

| Setting | Local configuration | Production configuration |
|---|---|---|
| Dashboard host | `app.localtest.me` | `app.urumi.example.com` |
| Original store | `store.localtest.me` | `store.urumi.example.com` |
| Managed base domain | `localtest.me` | `shops.urumi.example.com` |
| URL scheme | HTTP | HTTPS |
| Custom images | Local imported tags | Example `ghcr.io/urumi-example` references |
| PostgreSQL PVC | 1Gi | 10Gi |
| Each managed store | Two 2Gi claims with a 4Gi quota | Two 10Gi claims with a 20Gi quota |
| Storage | Local-path with Delete reclaim | Local-path with Delete reclaim |
| Secrets | Existing application Secrets | Existing Secret references plus the certificate reference |
| TLS | Local routes use HTTP | Default Traefik TLSStore referencing `urumi-public-tls` |
| Worker profile | Local MCP images/settings | Production MCP images, HTTPS ingress and matching storage limits |

The example domain and registry names are values in the repository. The gateway template maps the managed domain/scheme to `STORE_BASE_DOMAIN` and `STORE_URL_SCHEME`. The worker loads managed defaults, then its environment profile, then the fixed per-store plan.

The TLSStore template keeps the wildcard private key in the platform namespace. Managed store Ingresses use that controller certificate. Validation checks include the domain format, secure cookie/TLS settings, image references, quotas, annotation types and enabled-worker API destinations.

Supported interrupted-release recovery uses saved release values. Updating the worker's profile does not rewrite existing Ready stores. The setup/access installers make explicit, store-scoped compatibility updates.

Helm updates replace manifests and images. Gateway startup runs Alembic migrations from the same application image. The databases and WordPress content remain separate persistent state, so selecting an earlier Helm release does not reverse database writes.

See [README VPS/k3s setup](../README.md#vps--k3s-setup) for installation commands and [README usage](../README.md#using-urumi) for the two-user flow.

### Follow a real request through the source

| To understand | Read in this order |
|---|---|
| External MCP orders query | `mcp_api.py` → `auth.py` → `core.py` → `upstreams.py` → Woo `main.py` → same-store Woo REST |
| Sales across two stores | `chat.py` / tool search → two qualified calls in `core.py` → each analytics `main.py` → each own Woo REST |
| Image then product | Dashboard `ImageDestination.jsx` / `Chat.jsx` → `media.py` / `media_scope.py` → `core.py` → Woo upload/product tools |
| Initial store creation | `store_requests.py` → `store_scheduling.py` / `store_jobs.py` → `store_lease_runner.py` → `store_provisioning.py` → `store_completion.py` |
| Restart during install | `store_helm_stage.py` → `store_helm_release.py` → `store_helm_recovery.py` → guarded Helm → readiness/completion |
| Store deletion | `store_lifecycle.py` → worker cleanup → claim capture / PV inventory → `store_execution.py` terminal evidence transaction |
| Owner password | Dashboard setup helper → `store_wordpress_setup.py` issuance → store PHP form → capability redemption → WordPress password write |
| Monitoring | Tool audits / operation rows → `monitoring.py` snapshot → frontend validator → `Monitoring.jsx` |

### Terms

| Term | Meaning here |
|---|---|
| Upstream | A registered MCP server behind the gateway |
| Tool schema | Name/description/input definition, not a tool result |
| Grant | One user's permission for one tool on one server |
| UID | Kubernetes object identity; names alone can be reused |
| Lease | Temporary DB ownership of one worker operation |
| Generation | Store lifecycle version rejecting stale work |
| PVC / PV | Storage claim / its backing Kubernetes volume |
| Readiness / liveness | Can receive traffic / process is making progress |
| Idempotent retry | Reuse the accepted request identity instead of creating a second operation |
| Uncertain write | Operation may have happened but the reply/outcome cannot be confirmed |
| ForwardAuth | Traefik asks the gateway whether a managed store request may proceed |
| MCP annotation | An upstream hint, never permission |

The short design choices are at the start of `system-design.md`. To trace a request, follow its row above and then read the checks for a revoked grant, unavailable upstream, lost worker lease or replaced resource UID.
