# Threat model

Urumi sits between team members and the services they can use. The gateway checks each user's access, but the upstream services run with shared integration credentials. This note explains what that means and where the checks happen.

## What we protect

| Data or access | Where it belongs |
|---|---|
| User passwords, sessions and API keys | The user's account and gateway authentication layer |
| Upstream credentials | Encrypted registry fields or Kubernetes Secrets |
| Store products, orders and uploads | That store's WordPress/MySQL and content volume |
| Private chat history | The signed-in user's conversations |
| Provisioning permissions | The worker's active job and its owned store namespace |
| Tool and security history | Gateway audit and security-event records |

Platform administrators manage servers, grants and account access. Kubernetes/database operators are a higher-trust group: they can access the underlying workloads and storage. The owner-only password form does not remove that operator access.

## Shared upstream credentials

A member never sees the WooCommerce key or a remote server's stored credential. The gateway still uses that shared credential when it executes an allowed tool.

For example, granting `create_product` lets a member create products on that tool's store. Hiding the key does not make the operation read-only. Grants are checked for both tool listing and execution, and denied tools are hidden. Store creation does not automatically grant business tools.

The WooCommerce MCP holds a read/write key. Analytics has a separate read-only key. Media uses its own restricted WordPress account. Each managed store has separate credentials and data. Audit records attribute the call to the gateway user, while WooCommerce sees the integration credential.

An upstream that ignores its own tool description can still misuse the rights of its credential. Tool annotations are hints, not a security control.

## Client tokens stop at the gateway

Each external request carries a personal Urumi Bearer key. The gateway checks its hash, revocation and current account state. Browser requests use a signed, database-backed session with origin/CSRF checks for mutations.

The gateway does not forward the member's key, browser cookie or CSRF token upstream. It builds a separate outbound request using only that server's configured credential. This prevents an upstream from receiving a credential it could reuse against Urumi.

API keys are random and stored as hashes. Dashboard passwords use Argon2. Recoverable registry credentials use Fernet encryption. Its encryption key comes from a Kubernetes Secret. Kubernetes Secret base64 encoding is separate from encryption.

## Untrusted servers and model instructions

An administrator can register an external MCP server whose tool descriptions or results contain misleading instructions. A saved note or product description can carry the same kind of content. Those strings are data from another service.

The chat limits the active tool set and tells the model to use only available tools. Execution still goes through the gateway's current account, grant, server and store checks. Model output cannot add a permission. External tools also pass destination validation: approved public HTTPS addresses, DNS checks and pinned connections. Redirects and endpoint changes are blocked. Exact internal services have their own configured exceptions.

These controls limit access, but an allowed model-selected action can still be wrong. Descriptions and tool hints do not guarantee safe intent. Some destructive tools ask for confirmation, but there is no universal human-approval gate for every mutating tool.

Relevant tool results can be sent to Groq as model context. Credentials stay server-side, while returned business data may leave the cluster for inference.

## One member's effect on another

Members can read their own chat and audit records. Administrators have wider audit visibility. Notes are shared team data, so a member granted Notes reads can see notes added by another member.

Store ownership and tool grants are separate. An administrator can grant member 2 an update tool on member 3's store. That allows the granted operation, but it does not transfer ownership or permit WordPress password setup. Only the owner can issue/redeem that store's one-use setup token. Account/session changes, store disablement, deletion and expiry invalidate setup.

NetworkPolicies block one store's MCP pods from another store's WordPress/MySQL. Each store also has its own namespace, Secret and volumes. Per-user request limits, store quotas and the shared creation limit reduce resource competition.

## Worker access and uncertain writes

The worker can create/delete managed namespaces and manage workloads through a scoped RoleBinding. Admission rules constrain namespace and binding identities. It can get/list PVs but cannot delete PVs or read platform Secrets through its assigned roles.

Before and after external work, guards check the lease, generation and saved namespace/Secret UIDs. Storage checks keep exact PVC/PV identities. A stale worker cannot adopt a replacement resource or commit Ready after its lease expires.

Calls can succeed upstream even when the reply is lost. Urumi records an attempt before execution and reports an uncertain outcome rather than blindly repeating a write. Audit and recovery help inspect the result; they are not a distributed transaction.

The maintenance gate closes access when the gateway/database cannot confirm an enabled Ready store. This protects the disable switch, with the cost that gateway/database availability affects managed storefront access.

Implementation paths and the corresponding checks are listed in the [system reference](system-design-reference.md).
