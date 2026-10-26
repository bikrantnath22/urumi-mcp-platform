# Urumi — system design and tradeoffs



Urumi runs a WooCommerce store and a team MCP gateway on Kubernetes. An administrator connects an upstream once. A team member then uses its permitted tools from the dashboard chat or an external MCP client, using only their own Urumi account or API key.

This document explains the architecture and the main decisions. The [detailed reference](system-design-reference.md) contains the complete component index, HTTP routes, database fields, tools, Kubernetes files and operational limits. Read this document first; use the reference when you want to find the implementation.

## 1. Design choices in brief

- One gateway handles discovery, permissions, routing and auditing for both chat and external clients. The chat does not get a second route around permissions.
- WooCommerce business operations use its REST API. WP-CLI and PHP helpers handle installation and application-specific setup.
- Each managed store has its own WordPress, MySQL, WooCommerce MCP, analytics MCP, credentials, namespace, storage and hostname.
- PostgreSQL stores accounts, grants, audits and chats. It also holds the durable provisioning queue, leases and resource identities.
- Small tool-schema and monitoring caches live in gateway memory. There is no Redis, message broker or cache of order answers.
- Kubernetes provides scheduling, Services, DNS, storage controllers and policy enforcement. Traefik handles public ingress and the managed-store maintenance check.
- Two Helm charts define the platform and store workloads. Values files hold environment settings, and the provisioner adds each store's identity and address.

Tool names use the server slug as a prefix to avoid collisions. Discovery and calls have separate bounds, so one unavailable upstream does not invalidate the others.

The tradeoff is a small number of infrastructure services, with more explicit coordination in PostgreSQL and the worker. This gives recoverable operations without adding a separate queue service. It also makes PostgreSQL a central dependency.

## 2. What runs where



~~~mermaid
flowchart TB
    Browser["Browser: runs React dashboard and chat"]
    Client["External MCP client: personal API key"]
    Shopper["Shopper: storefront and checkout"]
    LocalEdge["Local k3d host-port mapping / server load balancer"]
    Ingress["Traefik: public host and path routing"]
    Browser --> LocalEdge
    Client --> LocalEdge
    Shopper --> LocalEdge
    LocalEdge --> Ingress

    subgraph Platform["platform namespace"]
        UI["Dashboard: Nginx on 8080"]
        Gateway["Gateway: FastAPI and MCP on 8000"]
        Cache["Process memory: schema cache, monitoring cache, HTTP counters"]
        DB[("PostgreSQL: gateway DB and separate notes DB")]
        Notes["Team Notes MCP on 8000"]
        Worker["Private store provisioner: Python and Helm"]
        Gateway --> Cache
        Gateway --> DB
        Gateway --> Notes
        Notes --> DB
        Worker --> DB
    end

    subgraph Store["store namespace, or one urumi-store-UUID namespace"]
        Woo["WooCommerce MCP on 8000"]
        Analytics["Sales analytics MCP on 8000"]
        WP["WordPress / WooCommerce on 80"]
        MySQL[("MySQL on 3306 and data PVC")]
        Content[("WordPress content PVC")]
        Job["Bootstrap Job: WP-CLI and PHP helpers"]
        Woo -->|"RW Woo key / restricted media account"| WP
        Analytics -->|"Separate RO Woo key"| WP
        WP --> MySQL
        WP --> Content
        Job --> MySQL
        Job --> Content
    end

    Ingress -->|"App /"| UI
    UI -.->|"HTML CSS and JavaScript returned to browser"| Browser
    Ingress -->|"App /api and /mcp"| Gateway
    Ingress -->|"Store host, after managed access check"| WP
    Ingress -.->|"Managed-store ForwardAuth"| Gateway
    Gateway --> Woo
    Gateway --> Analytics
    WP -.->|"Owner setup capability redemption"| Gateway
    Gateway --> LLM["Groq: model inference over HTTPS"]
    Gateway --> Remote["Other approved MCP upstreams, including Tavily"]
    Worker --> API["Kubernetes API: TLS, RBAC and admission policies"]
    API -.->|"Creates and observes store resources"| Store
    Worker -.->|"Private readiness/discovery only"| WP
    Worker -.-> Woo
    Worker -.-> Analytics
~~~

Nginx returns the dashboard files to the browser. React then sends login, chat and store requests through Traefik's `/api` route to the gateway. This is a browser connection, so there is no direct Nginx-to-gateway link.

The store box is repeated, not shared, for managed stores. Team Notes remains one shared service in the platform namespace. The worker's direct WordPress/MCP calls are private readiness checks under a live provisioning claim; user business calls still go through the gateway.

| Component | Responsibility | State |
|---|---|---|
| React dashboard | Login, chat, tool activity, personal keys, stores and administration | Durable records are on the server; browser recovery data holds request identities |
| Dashboard Nginx | Serve the compiled application, health route and static assets | No business database or API proxy |
| Gateway | Browser API, inbound MCP server, outbound MCP client, authorization, audit and model loop | Gateway PostgreSQL database; bounded local caches |
| WooCommerce MCP | Translate product, order and media tools into REST requests to its store | No separate business database |
| Analytics MCP | Read orders with a separate read-only key and calculate bounded summaries | No warehouse, copied order database or analytics result cache |
| Notes MCP | Add and read shared team notes | Separate notes database and login on the PostgreSQL instance |
| Provisioner | Claim creation/deletion jobs, run guarded Helm operations, verify and clean up | Store/job/UID records in the gateway database |
| WordPress and WooCommerce | Storefront, cart, checkout, admin and business REST APIs | Store-specific MySQL and content PVC |
| Bootstrap Job | Install and configure the store, credentials, media account and checkout settings | Durable Job completion for managed stores |
| Kubernetes infrastructure | Schedule workloads, maintain endpoints, enforce supported policies and reclaim storage | API state and node-local persistent volumes |
| Groq / remote MCP services | External inference and registered external tools | External dependencies; their credentials stay server-side |

The original store in namespace `store` is separate from dashboard-created stores. Its default registrations are `woo` and `analytics`. A new store receives its own pair. Creating a product through the original WooCommerce registration still creates it in the original store.

## 3. Public routing, load balancing and storage

### Addresses and entry points

| Address/path | Destination |
|---|---|
| `http://app.localtest.me/` | React assets from dashboard Nginx |
| Same host, `/api` | Gateway browser API, including chat and administration |
| Same host, `/mcp` | Gateway Streamable HTTP endpoint; personal Bearer key |
| `http://store.localtest.me` | Original WordPress store |
| `http://<slug>.localtest.me` | One managed WordPress store; ForwardAuth check first |
| Internal `*.svc.cluster.local` names | Cluster Services; no public MCP or database Ingress |

The README creates a local k3d cluster with host ports 80/443 forwarded through its load-balancer container. Traefik then selects the route by host and path. Kubernetes Services select ready backend pods. A Service is a stable endpoint abstraction; it is not necessarily an extra proxy hop after Traefik. The controller can use pod endpoints or native Service balancing, depending on its configuration.

Each store runs one WordPress pod and one MySQL pod. Traefik routes incoming requests, while Services provide stable addresses for the selected backend pods.

`localtest.me` names resolve to loopback. A browser must reach the machine exposing the local cluster. They are local development addresses, not a public hosting service. Store display names and DNS slugs are different: `Assam Tea` can use either `assamtea` or `assam-tea`. Spaces are allowed in the display name, not in a hostname.

### Persistence

| Data | Location |
|---|---|
| Users, key hashes, sessions, upstream definitions, grants, tool audits, chat history, store jobs and resource identities | Gateway PostgreSQL database |
| Team notes | Separate notes database on the same PostgreSQL server |
| Products, orders, WordPress accounts/options | MySQL in that store's namespace |
| Plugins, themes and uploaded media | That store's WordPress content PVC |
| Namespace/Secret/PVC/PV UIDs, release records and resource definitions | Kubernetes API state; selected identities also saved in PostgreSQL |
| Tool schemas, monitoring snapshots, HTTP counters, temporary Helm workspace | Process memory or temporary files; not the durable source of truth |

The default local storage class is `local-path`. PostgreSQL has its own PVC. Each store has `data-mysql-0` and `wordpress-data`. Persistence survives pod replacement on the storage node; it does not provide replication, backups or recovery from loss of that node.

## 4. Identity, permissions and credential movement

Dashboard login and external MCP authentication are separate entry paths.

**Dashboard:** an Argon2 password hash identifies the account. Login creates a signed HttpOnly session cookie backed by a revocable database session. The session has a 30-minute idle limit and an eight-hour absolute limit. Ordinary requests do not renew it. Authenticated mutations require the origin/CSRF checks.

**External MCP:** a random `urm_` API key is shown when issued. PostgreSQL stores its SHA-256 hash and a display prefix. Each MCP HTTP request validates the key, revocation and current account state. Revocation rejects the next request; it does not undo an operation already accepted upstream.

| Action | Member | Store owner | Platform administrator |
|---|---|---|---|
| Use a business tool | Exact server/tool grant required | Same grant requirement | Broad tool access, still subject to store state and usage limits |
| Read personal chat/history and manage own API keys | Own records | Own records | Own records; separate admin APIs manage member keys |
| Create stores | When creation is enabled and quotas allow | Same | Same creation guardrails; can change settings |
| View/delete a managed store | No access to someone else's store | Own store | Administrative visibility and deletion |
| Disable/enable a store | No | No additional right from ownership | Yes |
| Set WordPress password | No access to someone else's store | Yes, for an enabled Ready store | Only when also the store owner |
| Register upstreams, grant tools, invite/manage members, view Monitoring | No | No additional right from ownership | Yes |

Members start with no tool access. Store creation does not automatically grant business tools. An administrator can grant member 2 a tool on member 3's store; member 2 can then perform that tool's business operation, but does not become the owner or gain WordPress-password setup.

### Credentials stay at their intended boundary



~~~mermaid
flowchart LR
    Client["Client: personal Urumi API key"] -->|"Authenticate only"| Gateway["Gateway"]
    Gateway -->|"Database lookup: key hash and grants"| PG[("Gateway DB")]
    PG -->|"Fernet-encrypted static credential"| Gateway
    Gateway -->|"Decrypt only for approved destination"| Upstream["Registered upstream MCP"]
    Upstream -->|"Own Woo / media credential"| WP["That store's WordPress"]
    Secrets["Kubernetes Secrets"] -.-> Gateway
    Secrets -.-> Upstream
~~~

The client's API key or browser cookie is never forwarded to an upstream. A configured upstream Bearer/header credential is different and is decrypted only for that destination. Public registry responses return `has_credential`, not its value.

Local custom Woo/analytics/Notes MCP endpoints do not require a separate Urumi user key. Their access boundary is internal Services and NetworkPolicy; the gateway performs user authorization. Woo/analytics then authenticate separately to WordPress. This is shared integration access with per-user gateway enforcement.

The static Secret `gateway-auth-secrets` contains bootstrap dashboard credentials and signing/encryption keys. Each store's `store-secrets` contains its own database, initial WP admin, Woo RW/RO and media credentials. New owners choose a later WP password separately. Workloads receive only the Secret fields they use. Kubernetes Secret data uses base64 encoding, which does not itself encrypt the values.

## 5. MCP discovery, names, routing and failures

An upstream speaks Streamable HTTP. For discovery or execution, the gateway creates a bounded HTTP/MCP session, initializes it and sends `tools/list` or `tools/call`. It closes the session and transport afterwards. The inbound gateway MCP server is stateless and returns JSON responses; it does not require sticky browser sessions or an in-memory per-user MCP connection.

### Listing tools

1. Reload the active user's account and permitted server/tool grants.
2. Select enabled servers relevant to that user.
3. Discover with at most eight simultaneous discovery operations. Ordinary aggregate discovery gives each upstream at most three seconds.
4. Prefix names with the server slug, preserve the schema and add the server name to the description.
5. Catch individual upstream failures, update their last observed status and continue collecting other results.
6. Reload enabled/configuration/grant state after discovery, then return only tools that still qualify.

The returned list is permission-filtered. An external client may retain an old list in its UI until refreshed; calling an old name still goes through fresh server-side checks.

### Names and collisions

The public name is `<server-slug>__<tool-name>`. Two servers exposing `list_orders` remain distinct:

~~~text
woo__list_orders
woo-cloth-as-8ab1bb9267dd__list_orders
analytics-cloth-as-8ab1bb9267dd__order_counts
~~~

Managed prefixes contain `woo` or `analytics`, up to eight characters of the store slug and twelve hex characters from its UUID. The registry enforces unique slugs; collision/conflict handling never silently adopts someone else's registration. Gateway-qualified names are capped at 128 characters.

The model provider has a different name constraint. Long or otherwise incompatible names get deterministic hashed aliases for that model request. The alias maps back to the qualified name before execution; it grants no new access.

### Calling a tool



~~~mermaid
sequenceDiagram
    participant C as Chat or external client
    participant G as Gateway core
    participant D as PostgreSQL
    participant U as Selected upstream MCP
    participant W as Store REST API or Notes DB
    C->>G: Qualified tool name and arguments
    G->>D: Check account, grant, server, store state and quota
    G->>D: Commit audit attempt before upstream execution
    G->>U: Initialize MCP and call original upstream tool name
    U->>W: Run the selected business operation
    W-->>U: Result
    U-->>G: MCP result
    G->>D: Record duration and confirmed outcome
    G-->>C: Result or safe error with uncertainty when needed
~~~

Chat, `POST /api/tools/call` and external `/mcp` use `execute_gateway_tool` in `core.py`. The core resolves the server prefix, checks current grants/enabled state, records the attempt and calls only that server. It does not discover every upstream before each execution.

Upstream call deadlines are configurable from 1–60 seconds; the default registry value is 15 seconds. Discovery follows pagination but stops at 20 pages or 1,000 tools, rejects repeated cursors and duplicate names, and returns no misleading partial catalogue.

A connection failure marks that server Unreachable, invalidates its schema cache and returns a safe error. Other servers remain available. A business error returned by a reachable tool does not mean the server itself is unreachable. A lost reply can mean a write already happened. The gateway reports that uncertainty rather than automatically repeating the write.

## 6. The three custom upstream services

### WooCommerce MCP: runtime store operations

Source: [apps/woo-mcp/main.py](../apps/woo-mcp/main.py).

This service is written in Python using FastMCP. It exposes twelve tools:

| Area | Tools |
|---|---|
| Products | `list_products`, `get_product`, `create_product`, `update_product`, `delete_product` |
| Orders | `list_orders`, `get_order`, `update_order_status` |
| Media | `upload_media`, `list_media`, `update_media`, `delete_media` |

Product/order tools use WooCommerce `/wp-json/wc/v3` with the store's read/write consumer key and secret. Media tools use WordPress `/wp-json/wp/v2/media` with a separate restricted account and application password. The service returns selected fields instead of dumping the complete REST response.

REST was chosen because WooCommerce already validates business operations and provides pagination/status filters. Runtime tools do not shell out to WP-CLI or write directly to MySQL. WP-CLI remains an installation tool.

The adapter preserves the configured store Host and sets its forwarded protocol header for REST requests. Public links are normalized to that store's configured public origin. This is separate from end-user authorization and does not encrypt the internal HTTP connection.

Uploads accept JPEG, PNG, WebP or GIF, at most 2 MiB, validate base64 and inspect file signatures. Product creation can use a media ID or image URL. Network restrictions limit store egress, but an authorized public image URL can still cause WordPress to fetch external content; the MCP destination validator is not a general-purpose image-content sanitizer.

### Sales analytics MCP: calculations and their meaning

Source: [apps/mcp-analytics/main.py](../apps/mcp-analytics/main.py).

Every store, including the original one, has this separate upstream. It uses the store's **read-only** Woo consumer key. It reads `/wp-json/wc/v3/orders` and calculates results in memory. It does not read MySQL directly, change an order, or maintain a second order database.

| Tool | What it returns | Important meaning |
|---|---|---|
| `order_counts` | Total non-trashed orders and counts grouped by status | Includes unpaid/pending states; a count is not revenue |
| `sales_summary` | Number and gross total of processing/completed orders, separately by currency | Includes tax/shipping; partial refunds are not deducted; COD can be unpaid |
| `top_products` | Products/variations ranked by units in processing/completed orders | Ordered units, not stock levels or refund-adjusted net sales |

The implementation uses Decimal for monetary aggregation and never adds different currencies into one number. Product rankings use product/variation identity and deterministic tie ordering.

Dates must be `YYYY-MM-DD`, with inclusive day boundaries in the **WordPress site timezone**. They filter order creation date, not payment/completion date. Empty dates mean all available history. The dashboard's IST display formatting does not change WordPress's timezone. When comparing “today” across stores, the chosen dates and store timezone must be understood.

Each request reads at most ten pages of 100 orders, using ascending order IDs and a restricted set of fields. It has a 25-second overall bound and ten-second HTTP timeouts. More matching pages, invalid responses, redirects or a timeout produce an error asking for a narrower date range. The service does not call an incomplete scan a full total. The gateway's shorter configured call timeout can end the request sooner.

For a two-store comparison, chat calls the two qualified analytics tools separately and labels each result. There is no cross-store database join or permission bypass. During the live test, both tools returned a valid zero count; missing/failed data would not justify reporting zero.

`analytics_chat.py` also handles an unambiguous “all-time order count and sales” request: it uses the one available analytics pair with empty date filters and formats verified totals. When several analytics destinations are possible, it asks which store. General cross-store questions continue through normal tool selection.

### Team Notes MCP: a different data source

Source: [apps/mcp-notes/main.py](../apps/mcp-notes/main.py).

This service exposes `add_note`, `list_notes`, `get_note` and `info`. It uses parameterized SQL, a bounded async PostgreSQL pool and a separate database/login. Listing uses bounded results and an ID cursor. Notes are shared team data for people granted the relevant tools; they are not private per-member records. Stored note text is untrusted data when returned to the model.

WooCommerce, analytics and Notes satisfy the three-upstream requirement. Tavily is an additional remote MCP registration, not a fourth custom server written in this repository.

## 7. Chat, tool search, images and replay

The browser sends an authenticated `POST /api/chat` with a conversation ID and idempotent turn ID. The gateway saves the accepted turn, loads only that account's recent context and runs the model loop. Groq receives the model prompt, selected schemas and any business data needed in the tool results. It receives no gateway API key or upstream credential. Relevant order/note data can leave the cluster as model context; keeping credentials private does not mean inference is local.

The configured default model is `openai/gpt-oss-20b` through Groq's compatible chat-completions API.



~~~mermaid
flowchart LR
    Request["Own conversation and accepted turn"] --> Context["Compact saved context"]
    Context --> Catalogue["Current permitted tool catalogue"]
    Catalogue --> Select["Small active set; search more definitions if needed"]
    Select --> Model["Groq completion"]
    Model --> Decision{"Tool call?"}
    Decision -->|"Yes"| Core["Same authorized and audited gateway core"]
    Core --> Save["Save progress and return result to model"]
    Save --> Model
    Decision -->|"No"| Finish["Persist final answer and trace"]
~~~

The browser shows the tools, arguments and returned results. `ChatRunStatus` shows waiting/running progress; this is not token streaming from the model. The main chat response is JSON, with saved turn progress available through history.

| Bound | Current source default |
|---|---|
| Whole chat turn | 90 seconds |
| Upstream business actions per turn | 3 |
| Model output per completion | 512 tokens |
| Estimated model budget per turn | 12,000 tokens |
| Compact history context | 2,400 characters |
| Active business tool definitions | At most 4, within about 6,000 schema characters |

For compound requests, the initial selection draws matches from each part of the message. Later searches blend new definitions with the previous active set, so looking up Notes can retain an order tool needed by the same request. Both limits still apply; selection itself executes no upstream action.

Tool search uses `urumi_search_tools` to load matching definitions from the permitted directory. It does not execute a store operation and cannot reveal tools that were denied. Searches are bounded by time, model budget and progress checks. Model parallel tool calls are disabled.

These limits explain why a large request may need several turns. Token estimates are not exact tokenizer accounting or a promise that Groq's own quota is available.

An image upload goes through the same granted media tool, records its audit and returns a store-specific media ID. The selected destination is carried into the product operation. With several store grants, the member must choose the intended store. An image ID from one store is not a portable product image ID for another.

A repeated accepted turn can return its saved progress/final result. This is durable request recovery, not a cache of live orders. The system does not promise exactly-once execution of arbitrary external writes. Cancellation, provider errors or a lost reply can leave an uncertain action for the user to inspect.

## 8. Store creation, parallel work and restart recovery

### Accepting and scheduling a request

The dashboard validates the store name and slug and generates a secure request UUID. Account-scoped browser recovery data keeps the same request identity if the reply is lost. The server binds that identity to a request fingerprint. An identical retry returns the accepted operation; different content under the same identity conflicts.

Acceptance is automatic after validation and admission checks. There is no separate manual “accept” button for an administrator. A database transaction creates a Provisioning store and Pending operation. The admission lock protects the creation switch, per-user/global store counts and unique address. The scheduler separately enforces the configured number of simultaneous creations under the same shared gate. Pending jobs keep their original acceptance-time deadline, so queue time uses part of that allowance. Failed stores keep consuming store quota until deleted.

The worker polls PostgreSQL. Row locking, lease tokens and operation generations prevent competing attempts from owning the same job. Selection prioritizes deletion and overdue cleanup, then provision jobs with owner fairness. The configured global creation limit applies across workers. The local `max_jobs=100` task ceiling is not permission to create 100 stores in parallel.

### Provisioning sequence


~~~mermaid
flowchart TD
    A["Accepted store and Pending operation"] --> B["Claim job; renew 60-second lease"]
    B --> C["Create/recover owned namespace; persist namespace UID"]
    C --> D["Bind scoped access; create/recover immutable credentials Secret; persist UID"]
    D --> E["Inspect release; install once or reconcile an owned interrupted release"]
    E --> F["Wait for current workloads and durable bootstrap completion"]
    F --> G["Read/save/read back both PVC and PV identities"]
    G --> H["Read-only WordPress and credential checks"]
    H --> I["Register Woo and analytics disabled; discover their required tools"]
    I --> J["Recheck workloads, volumes, maintenance gate and claim"]
    J --> K["Atomic DB completion: Ready, Succeeded and enabled registrations"]
~~~

The lease normally renews every 20 seconds. The original provisioning deadline comes from the accepted operation and is never extended by a retry or restart. The default timeout is 1,200 seconds; current database settings can differ.

The namespace is `urumi-store-<UUID hex>` and the release name is `store`. The worker persists the actual namespace UID, immutable credential-Secret UID, both PVC UIDs and their PV identities. A familiar resource name is not enough: a replacement object with a different UID is rejected.

Helm installs with `--wait=false`. The renewable worker, rather than a long Helm wait, checks readiness. The managed bootstrap is an ordinary durable Job, so stopping the worker does not remove or rerun that Job. The worker polls its workloads while retaining the claim and creation slot.

On replacement attempts, the worker inspects the stored Helm release before writing. For a supported owned interrupted release, it can:
1. Verify namespace/Secret ownership and the release's saved chart, values and deadline-related configuration.
2. Atomically change an abandoned pending Helm record to failed, using tests on UID, resourceVersion, old payload and old status.
3. Run a normal guarded Helm upgrade with `--reuse-values`.
4. Independently inspect the persisted result and resume verification.

This is specific to the pinned Helm 3 storage format. It never marks the store Ready by patching Helm, adopts unrelated releases, resets values or recreates the bootstrap Job. Recovery history is bounded at eight revisions. Unsupported or changed evidence stops recovery.

### The condition for Ready

Helm success alone is insufficient. Verification requires:
- MySQL StatefulSet, WordPress/Woo/analytics Deployments and bootstrap Job with current identity/generation/replica/completion state.
- Both fixed PVCs fully bound to the saved PV identities.
- Storefront response, read-only probes with each Woo RW/RO credential, and the restricted media account.
- Both disabled MCP registrations with correct store identity and required discovered tools.
- The exact Traefik maintenance Middleware and store Ingress.
- A live claim, original deadline and unchanged saved records in the final completion transaction.

No verification probe creates products or orders. Only the final transaction enables registrations and commits Ready/Succeeded. It rechecks time after real row-lock waits and flushes, so an expired lease cannot become valid by waiting.

In the 23 October restart test, two stores were created at the same time. After the worker restarted, both reached Ready/Succeeded on attempt 2. Their namespace UIDs, credential-Secret UIDs, operation IDs and original deadlines stayed the same.

## 9. Bootstrap, WordPress setup and store controls

### Installation

`bootstrap.sh` waits for WordPress configuration/database, installs WordPress using the chosen store name, sets permalinks, installs the pinned WooCommerce version, configures pages/COD/shipping, verifies API keys/media access and runs the seed script. Existing matching products/settings are reused where the helper supports replay; conflicting credentials or plugin versions stop setup.

The source pins WordPress `7.1.3-php8.3-apache`, WP-CLI `cli-2.12.0-php8.3` and WooCommerce `11.2.1`.

PHP files are used because WordPress itself is PHP. `keys.php` and `media.php` use WordPress/Woo storage conventions and verify recoverable credentials. `wordpress-setup.php` implements the later owner password form. They are application helpers, not independent public services.

The protected setup plugin is mounted at `/opt/urumi/mu-plugins`, outside the content PVC. Mounting it under an uninitialized `wp-content` previously caused root-owned parent directories and prevented plugin installation. The current layout lets non-root WordPress startup create writable content directories while keeping the setup plugin read-only.

### Owner chooses the WordPress password

The owner starts setup from **Stores → Set WordPress password**. The one-use setup token lasts five minutes and is stored as a hash. It is bound to the owner, browser session and store.




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

The chosen password policy is 6–16 characters. New managed stores use username `admin`; compatible older usernames remain supported. The original random password in the immutable provisioning Secret does not become the owner's new password. The gateway never receives the chosen password.

There is no distributed transaction between capability redemption and the WordPress password write. If redemption succeeds and the password write fails, the owner must start setup again. A non-owner platform administrator cannot use the normal setup API to reset this password; cluster/database operators remain a higher-trust boundary.

### Disable versus delete

**Disable** is an administrator action. It preserves store data, resource identities and grants, disables the managed MCP registrations, and makes Traefik's ForwardAuth return a maintenance response. The gate checks store state, not shopper login. It permits enabled, guarded Ready stores and otherwise returns 503. Gateway/database failure also closes the gate. This intentionally makes gateway availability part of managed storefront availability.

**Delete** is available to the owner or administrator. It disables access and queues a destructive lifecycle operation. Deletion does not reuse the non-destructive disable switch as a substitute for teardown.

## 10. Deletion and storage recovery

Deletion records known storage before removing the owned namespace. It can save a PVC identity while the claim is still Pending, then add a matching PV name/UID later. Saved evidence can become more complete; it cannot be overwritten with another store's or replacement resource's identity.

After namespace deletion, the storage controller handles PVC/PV reclamation. The worker has PV get/list permission, **not PV delete**. It observes the exact saved volumes and can scan bounded PV pages if a PVC disappeared before its PV identity was fully saved.

A scan follows all opaque continuation tokens, keeps one resource version and rejects repeated tokens, duplicates, replacement identities and incomplete responses. Limits are 100 items per page, 100 pages and 4,096 characters per token. Reaching a limit or losing the cursor does not count as cleanup success.

Only verified namespace absence and complete storage evidence permit terminal completion. For a user deletion, the database commits Deleted/Succeeded and removes both MCP registrations and their tool grants. For timed-out creation, verified cleanup permits Failed with the fixed timeout reason. Historical audits and storage ownership records remain; unrelated stores and Team Notes remain untouched.

Cleanup can continue after the creation deadline, but always needs a live cleanup lease. Kubernetes resource disappearance is the evidence used here. It is not a claim of forensic disk erasure or a substitute for a storage backup policy.

The live deletion test removed the selected Food store's namespace and both recorded PVs while the Cloth store's pods/PVCs and tool access continued working. Separate live checks denied cross-store WordPress/MySQL Service and pod-IP access in both directions, with own-store positive controls.

## 11. Caching, usage limits and monitoring

| Mechanism | Stored where | What it holds / invalidation |
|---|---|---|
| Tool catalogue cache | Gateway process memory | Schemas only; default 60 seconds, 32 entries, 2 MiB; default cached slugs are `woo,analytics,notes` |
| Monitoring cache | Gateway process memory | Read-only aggregate snapshots; 20 seconds; only 1/6/24-hour windows |
| Rate-limit buckets | PostgreSQL | Shared counters across processes and all keys of one account |
| Chat/store request replay | PostgreSQL | Accepted request/progress/result identity; durable recovery, not a live-data cache |
| Built frontend assets | Browser HTTP cache | Content-hashed assets get a long immutable cache; index.html requires revalidation |

Tool cache misses are coalesced, results are deep-copied and late invalidated loads cannot repopulate an entry. Cache identity includes server configuration/credential changes. Dynamic managed-store slugs bypass schema caching unless explicitly configured. A schema hit is not a fresh health check. Grants, revocation and store authorization are still checked.

Orders, sales results and Notes responses come from their upstreams. Schema and monitoring caches and HTTP counters belong to the gateway process. PostgreSQL holds permissions, rate-limit buckets and accepted request records. The project does not use Redis.

The source usage defaults are five account login attempts and 30 IP attempts per ten minutes, ten chat requests per user/minute and 100/hour, 30 global chats/minute, and 60 tools per user/minute. Administrators can change them. Proxy forwarding headers affect IP limits only when the TCP peer is in the configured trusted proxy ranges.

### Observability

Monitoring is an administrator-only page backed by bounded read-only PostgreSQL queries. It shows:
- Tool calls, confirmed failures, denied/uncertain calls, error rate, average and P95 latency.
- Per-server counts/latency and last observed Connected/Unreachable/Disabled state.
- Twelve activity buckets over the chosen 1/6/24-hour window.
- Store inventory, active/queued/overdue jobs and completed/failed creation durations.
- Gateway-process HTTP requests, errors, rejections, cancellations and average duration.

Denied and uncertain calls do not become confirmed upstream failures in the error-rate denominator. Upstream status means the last recorded discovery/call, not a continuous synthetic probe. HTTP counters reset on process restart; database audit/job metrics survive.

The page polls sequentially every 30 seconds while visible and handles aborts/late replies. Alerts appear in the refreshed page for unhealthy observed upstreams and relevant job/error conditions. There is no email/SMS delivery, Prometheus, Grafana or background alert service.

Structured JSON logs use generated request IDs and safe fields; the response exposes the correlation ID. Tool logs connect request IDs to audit IDs, and worker logs use the operation identity. Logs omit request bodies, passwords, keys and raw exception diagnostics. User-visible tool traces/history are separate and can contain business results. This is correlation logging, not a full distributed tracing system.

Worker probes read a local scheduler heartbeat. Liveness needs a recent heartbeat; readiness also needs a recent successful queue observation. The probes do not make their own database/API requests. SIGTERM/cancellation stops active work and awaits HTTP/subprocess cleanup before resources and claims are released.

## 12. Kubernetes controls, Helm values and tradeoffs

### Network and API boundaries

Default-deny ingress/egress is combined with explicit allowances for DNS, ingress, own-store REST/SQL, gateway MCP traffic, worker verification and required public HTTPS. Policies use namespace/pod selectors and ports; they do not validate an HTTP tool name or distinguish URL paths.

Store MCPs can reach their own WordPress, not another store's WordPress/MySQL. MySQL accepts WordPress/bootstrap traffic, not direct MCP SQL. Gateway can reach approved original and managed MCP services, Notes, PostgreSQL and public HTTPS. The owner setup callback has a specific network allowance. Application URL validation and grants provide controls beyond packet filtering.

Generic external MCP URLs require HTTPS on port 443 and exclusively permitted public DNS answers. Exact operator-approved internal endpoints are separate exceptions. The transport pins the validated numeric destination while preserving Host/TLS server name, rejects redirects/endpoint changes and ignores environment proxy configuration. Managed endpoints additionally require current store/registration/namespace ownership. Cluster CIDRs alone do not authorize an arbitrary internal URL.

Ordinary application ServiceAccounts do not mount Kubernetes API tokens. The provisioner explicitly mounts a projected token and CA, uses verified API TLS and refreshes token reads. Cluster RBAC permits namespace operations, a constrained workload RoleBinding and read-only PV discovery. A RoleBinding grants workload powers only in the managed namespace. Admission policies restrict the namespace and RoleBinding identity/ownership that RBAC alone cannot express.

The worker cannot delete PVs or read platform Secrets through these roles. It can manage significant workload/Secret resources inside authorized store namespaces. That power and the need to trust cluster administrators should be stated plainly.

Helm runs as fixed argv without a shell, with isolated environment and 0700 directories/0600 files. Stdin is capped at 64 KiB and combined stdout/stderr at 1 MiB. Timeout or lost ownership stops the whole process group, allows three seconds for TERM, then forces shutdown and waits for reaping before removing the workspace. Its kubeconfig references mounted token/CA files.

Long-running containers have resource requests/limits, non-root identities and probes; sensitive runtime writes use explicit writable mounts. A completed bootstrap Job uses completion/backoff/deadline semantics rather than a long-running HTTP probe. The [reference resource tables](system-design-reference.md#g-monitoring-caching-and-bounded-resources) give the exact defaults.

### Local and production values

The running system uses local k3d values. The repository also contains `values-prod.yaml` files for the same charts. This table compares the checked-in settings required by the assignment.

| Setting | Local values | Production values |
|---|---|---|
| Dashboard | `http://app.localtest.me` | `https://app.urumi.example.com` |
| Original store | `http://store.localtest.me` | `https://store.urumi.example.com` |
| Managed store | `http://<slug>.localtest.me` | `https://<slug>.shops.urumi.example.com` |
| Images | Local tags imported into k3d | Example `ghcr.io/urumi-example` registry references |
| Ingress and cookies | Traefik HTTP and local cookie setting | Traefik websecure/TLS and secure cookies |
| PostgreSQL volume | 1 GiB | 10 GiB |
| Managed MySQL/content | 2 GiB each with a 4 GiB quota | 10 GiB each with a 20 GiB quota |
| StorageClass | `local-path` | `local-path` |
| Credentials | Existing namespace-scoped Secrets | Existing namespace-scoped Secret references |
| Certificate | Local routes use HTTP | `urumi-public-tls` through a default Traefik TLSStore |

The production domain and registry entries are configuration examples. The files keep passwords out of values and reference an existing certificate for the app, original store and managed wildcard. The certificate key stays in the platform namespace.

The worker applies **managed defaults → environment profile → fixed per-store plan**. The profile supplies images, storage, quotas and ingress. The last layer supplies each store's identity, namespace, address and security settings. Gateway values map the domain and scheme to `STORE_BASE_DOMAIN` and `STORE_URL_SCHEME`.

Schema and template checks validate hosts, TLS/cookies, images, annotations, storage quotas and worker API destinations. Tests render two stores using the actual worker values order and check separate addresses, identities, network rules and matching quotas. The [README deployment guide](../README.md#vps--k3s-setup) gives the commands.

### Why these choices, and their costs

| Decision | Benefit in this project | Cost or limit |
|---|---|---|
| Shared gateway execution core | One place to enforce grants and attribute calls | Gateway/DB availability affects every client |
| REST at runtime, WP-CLI for setup | Uses existing Woo business validation; keeps shell access out of user tools | REST pagination, plugin compatibility and shared integration rights need care |
| Separate analytics with a RO key | Clear credential scope and explicit sales semantics | Another pod per store; bounded reads rather than a warehouse |
| PostgreSQL queue and leases | Atomic admission/completion beside application state | Polling, row/advisory locks and database connection budget |
| Namespace/Secret/PVC/PV UID records | Recover accepted work without adopting replacements | More validation and retained identity records |
| Durable bootstrap Job and inspect-before-recovery | Worker restart can finish the same store | Helm-format-specific recovery and fixed version compatibility |
| One WP/MySQL stack per store | Clear data, credential and network separation | Higher memory/storage use than shared-database tenancy |
| In-process caches | Small footprint and bounded memory | Per-replica caches; no global cache invalidation service |
| ForwardAuth maintenance gate | Store disablement preserves data and blocks MCP/public access | Gateway or PostgreSQL outage can close public managed stores |
| Local-path storage | Simple persistence and automatic local reclaim | Single-node failure risk; no backup or replication |
| No blind write retries | Avoids silently duplicating products or installs after lost replies | Some failures require inspection and explicit recovery |

Platform image updates use Helm. Gateway migrations run before application startup in the init container. Helm release history records manifest changes, while PostgreSQL/MySQL data remains on persistent volumes. A Helm rollback changes manifests rather than reversing application database writes.

The [verification and feature map](system-design-reference.md#q-verification-and-feature-locations) links the tested flows to their source files and suites.
