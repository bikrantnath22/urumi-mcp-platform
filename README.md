# Urumi

Urumi connects WooCommerce and other MCP servers through one gateway. It includes a dashboard, chat, user permissions and store management.

## Local setup

These steps run the project on your computer using Docker and k3d. Use them for a fresh installation.

On Windows, run the commands in **WSL Ubuntu**, with Docker Desktop running and WSL integration enabled. The commands below use Bash, not PowerShell.

Open a terminal in the project folder. You should see `apps`, `charts` and `scripts`. Run the steps in order and keep this terminal open. Stop if a command fails.

### 1. Check the tools

You need:

- Docker Desktop or Docker Engine, with Linux containers.
- [k3d](https://k3d.io/stable/), version 5.8 or newer, with Kubernetes 1.30 or newer.
- [kubectl](https://kubernetes.io/docs/tasks/tools/) compatible with your cluster.
- [Helm](https://helm.sh/docs/intro/install/), version 3.22.0. The store worker includes the same version.
- [uv](https://docs.astral.sh/uv/getting-started/installation/) and Python 3.12 for the gateway.
- `python3`, `openssl`, `curl` and `base64` in your WSL/Linux terminal.
- A Groq API key for chat.
- Internet access for the first image builds and the WordPress/WooCommerce downloads.

Check that the tools are available:

~~~bash
docker info
k3d version
kubectl version --client
helm version --short
uv --version
python3 --version
openssl version
curl --version
~~~

Install the gateway's Python version and dependencies:

~~~bash
uv python install 3.12
uv sync --project apps/gateway --frozen
~~~

The dashboard builds inside Docker. You do not need Node.js or npm on your computer for this setup. This also avoids mixing Windows and Linux `node_modules`.

### 2. Create the local cluster

Check whether the cluster already exists:

~~~bash
k3d cluster list
~~~

For a new cluster:

~~~bash
k3d cluster create urumi \
  --servers 1 --agents 0 \
  -p "127.0.0.1:80:80@loadbalancer" \
  -p "127.0.0.1:443:443@loadbalancer" \
  --wait
~~~

Ports 80 and 443 must be free on your computer. These mappings let the browser reach the cluster's Ingress. See the [k3d port-mapping guide](https://k3d.io/v5.8.3/usage/exposing_services/).

If `urumi` already exists but is stopped, use `k3d cluster start urumi` instead of creating it again. Keep the existing cluster and its data.

Select the cluster and wait for it:

~~~bash
kubectl config use-context k3d-urumi
kubectl wait --for=condition=Ready nodes --all --timeout=180s
kubectl -n kube-system rollout status deployment/traefik --timeout=180s
kubectl -n kube-system rollout status deployment/local-path-provisioner --timeout=180s
kubectl get nodes -o wide
kubectl get storageclass local-path
~~~

Use Kubernetes 1.30 or newer. The store worker uses [ValidatingAdmissionPolicy](https://kubernetes.io/docs/reference/access-authn-authz/validating-admission-policy/), which is stable from 1.30. Keep k3s's default Traefik, DNS, local-path storage and [NetworkPolicy controller](https://docs.k3s.io/networking/networking-services) enabled.

Check that the APIs needed for managed stores are present:

~~~bash
kubectl api-resources --api-group=admissionregistration.k8s.io
kubectl api-resources --api-group=traefik.io
~~~

The results must include `validatingadmissionpolicies`, `validatingadmissionpolicybindings` and `middlewares`.

The remaining commands use the current context. It should stay `k3d-urumi`.

### 3. Build and import the application images

Run these commands from the project folder. The `local` tag is used throughout this guide.

~~~bash
docker build -t woo-mcp:local apps/woo-mcp &&
docker build -t mcp-analytics:local apps/mcp-analytics &&
docker build -t mcp-notes:local apps/mcp-notes &&
docker build -t urumi-gateway:local apps/gateway &&
docker build -t urumi-dashboard:local apps/dashboard &&
docker build -f apps/gateway/Dockerfile.provisioner \
  -t urumi-store-provisioner:local .
~~~

The worker's build context is the project root because its image also contains the store Helm chart.

Check that the worker image contains the required files:

~~~bash
docker run --rm --network none --read-only \
  --cap-drop ALL --security-opt=no-new-privileges:true \
  --entrypoint python urumi-store-provisioner:local \
  -m app.store_worker_main --check-package
~~~

Expected: `Worker package checked. No services contacted.`

Import all six images into k3d:

~~~bash
k3d image import \
  woo-mcp:local \
  mcp-analytics:local \
  mcp-notes:local \
  urumi-gateway:local \
  urumi-dashboard:local \
  urumi-store-provisioner:local \
  -c urumi
~~~

WordPress, MySQL and PostgreSQL use the images specified in the charts. k3s pulls those images when the pods start.

### 4. Create the Secrets

Create the database credentials, dashboard login credentials and the first store's credentials:

~~~bash
bash scripts/platform-secrets.sh
bash scripts/gateway-secrets.sh
bash scripts/secrets.sh
~~~

These scripts keep existing Secrets. `scripts/secrets.sh` also reads the root `.env` file if one exists; review your own file before running it. You can leave `.env` absent and let the script generate the store passwords.

Create the Groq Secret. If `gateway-llm-secrets` already exists, keep it and skip this block.

~~~bash
read -rsp "Groq API key: " URUMI_GROQ_KEY
printf '\n'
kubectl -n platform create secret generic gateway-llm-secrets \
  --from-literal=GROQ_API_KEY="$URUMI_GROQ_KEY"
unset URUMI_GROQ_KEY
~~~

Enter your real key at the prompt. It is not displayed as you type. Keep keys and passwords out of source files and screenshots.

### 5. Install the first store

This is the original store at `store.localtest.me`. Stores created later from the dashboard get separate namespaces and addresses.

~~~bash
helm upgrade --install store charts/store -n store \
  -f charts/store/values-local.yaml \
  --set-string storeName=Urumi \
  --set-string wooMcp.image=woo-mcp:local \
  --set-string analytics.image=mcp-analytics:local \
  --wait --timeout 20m
~~~

Leave Helm hooks enabled for this install. The bootstrap Job installs WordPress and WooCommerce, sets up API credentials and checkout settings. New stores start without products; add products through chat or WordPress admin. The first run may take several minutes while images and the plugin download.

Check the result:

~~~bash
kubectl -n store get pods,jobs,pvc
kubectl -n store logs job/store-bootstrap -c bootstrap --tail=60
~~~

The `store-bootstrap` Job should show `Complete` with `1/1` completions. The MySQL and WordPress claims should be `Bound`.

### 6. Install the platform

~~~bash
helm upgrade --install platform charts/platform -n platform \
  -f charts/platform/values-local.yaml \
  -f charts/platform/values-apps-local.yaml \
  --set-string gateway.image=urumi-gateway:local \
  --set-string dashboard.image=urumi-dashboard:local \
  --set-string notes.image=mcp-notes:local \
  --wait --timeout 10m
~~~

This starts PostgreSQL, Notes, the gateway and the dashboard. The gateway's `migrate` init container applies the database migrations before the gateway starts. Store creation is paused on a fresh database; the worker is set up in step 8.

~~~bash
kubectl -n platform get pods,pvc,ingress
kubectl -n platform rollout status deployment/gateway --timeout=180s
kubectl -n platform rollout status deployment/dashboard --timeout=180s
~~~

### 7. Open the dashboard

| Page | Local address |
|---|---|
| Dashboard | http://app.localtest.me |
| Original store | http://store.localtest.me |

The local Ingress uses HTTP. Use these exact hostnames, since the gateway checks the browser's origin. A public domain is not needed for this setup.

The initial dashboard admin email is `admin@urumi.local`. Display its generated password in your own terminal:

~~~bash
kubectl -n platform get secret gateway-auth-secrets \
  -o jsonpath='{.data.ADMIN_PASSWORD}' | base64 --decode
printf '\n'
~~~

Sign in at `http://app.localtest.me`. These are the initial credentials for a new database; changing the bootstrap Secret does not reset an existing account's password.

The gateway registers WooCommerce, Sales Analytics and Team Notes on its first start. In **MCP servers**, refresh their status after the store is ready.

### 8. Set up the store worker

The worker handles stores created from the dashboard. Keep **Usage limits → Store provisioning → Allow new stores** off until this step is complete.

First install the worker's security rules without starting it or granting its namespace/storage access:

~~~bash
helm upgrade platform charts/platform -n platform \
  --reuse-values \
  -f charts/platform/values-provisioner-local.yaml \
  --set-string storeProvisioner.image=urumi-store-provisioner:local \
  --set-string storeProvisioner.profile.wooMcp.image=woo-mcp:local \
  --set-string storeProvisioner.profile.analytics.image=mcp-analytics:local \
  --set storeProvisioner.enabled=false \
  --set storeProvisioner.security.bindNamespaceRole=false \
  --set storeProvisioner.security.bindStorageRole=false \
  --wait --timeout 5m

python3 scripts/verify-provisioner-admission.py
~~~

The check creates an empty temporary namespace, tests allowed and blocked requests, then removes it. Stop if it fails.

Grant the namespace role and check that the worker's permissions stay inside its own store namespaces:

~~~bash
helm upgrade platform charts/platform -n platform \
  --reuse-values \
  --set storeProvisioner.security.bindNamespaceRole=true \
  --wait --timeout 5m

python3 scripts/verify-provisioner-worker-rbac.py
~~~

This check also creates and removes temporary test resources. It does not create an application store.

Next, read this cluster's API addresses. Do not copy an API IP from another installation:

~~~bash
export URUMI_API_SERVICE_IP="$(kubectl -n default get service kubernetes \
  -o jsonpath='{.spec.clusterIP}')"
export URUMI_API_ENDPOINT_IP="$(kubectl -n default get endpoints kubernetes \
  -o jsonpath='{.subsets[0].addresses[0].ip}')"
export URUMI_API_ENDPOINT_PORT="$(kubectl -n default get endpoints kubernetes \
  -o jsonpath='{.subsets[0].ports[0].port}')"

printf 'API service: %s\nAPI endpoint: %s:%s\n' \
  "$URUMI_API_SERVICE_IP" "$URUMI_API_ENDPOINT_IP" "$URUMI_API_ENDPOINT_PORT"
~~~

For this single-node IPv4 setup, both IPs must be present and the endpoint port is normally `6443`. The worker's NetworkPolicy allows only these API destinations.

Start the worker with the addresses you just read:

~~~bash
helm upgrade platform charts/platform -n platform \
  --reuse-values \
  --set storeProvisioner.enabled=true \
  --set storeProvisioner.security.bindStorageRole=true \
  --set-string storeProvisioner.apiServer.serviceCidr="$URUMI_API_SERVICE_IP/32" \
  --set-string storeProvisioner.apiServer.endpointCidr="$URUMI_API_ENDPOINT_IP/32" \
  --set storeProvisioner.apiServer.endpointPort="$URUMI_API_ENDPOINT_PORT" \
  --wait --timeout 5m

kubectl -n platform rollout status deployment/store-provisioner --timeout=180s
kubectl -n platform logs deployment/store-provisioner -c store-provisioner --tail=40
~~~

The worker pod should be `1/1 Running`. It can be quiet when no jobs are queued.

Check its storage access:

~~~bash
kubectl auth can-i get persistentvolumes \
  --as=system:serviceaccount:platform:store-provisioner-platform
kubectl auth can-i list persistentvolumes \
  --as=system:serviceaccount:platform:store-provisioner-platform
kubectl auth can-i delete persistentvolumes \
  --as=system:serviceaccount:platform:store-provisioner-platform
~~~

Expected answers: `yes`, `yes`, `no`.

In the dashboard, open **Usage limits → Store provisioning**, turn on **Allow new stores**, and save. The default creation timeout is 20 minutes. The original store is separate from the managed-store limits.

### 9. Check the local installation

~~~bash
kubectl -n platform get pods,pvc,ingress
kubectl -n store get pods,jobs,pvc,ingress
curl --fail --silent --show-error --output /dev/null http://app.localtest.me/
curl --fail --silent --show-error --output /dev/null http://store.localtest.me/
~~~

The two `curl` commands should finish without an error. The application pods should be ready, the bootstrap Job complete, and the storage claims bound.

You can also check the charts without changing the cluster:

~~~bash
uv run --project apps/gateway --with pyyaml \
  python scripts/verify-chart-security.py
~~~

### If something does not start

- **Browser cannot open the site:** check that Docker and `k3d-urumi` are running, port 80 is mapped, and the pods are ready. If your DNS blocks `localtest.me`, add `app.localtest.me` and `store.localtest.me` as `127.0.0.1` entries on the computer running your browser. Add each managed store's hostname there too if needed.
- **`ImagePullBackOff`:** check that all six images were imported into `urumi`, and that the Helm image names match the `:local` tags above. Managed stores need the image overrides under `storeProvisioner.profile` as well.
- **Gateway stays in `Init:CrashLoopBackOff`:** read `kubectl -n platform logs deployment/gateway -c migrate --tail=80`. The gateway image must contain the migrations for the database revision. Build it from the same checkout as the charts.
- **Store bootstrap fails:** read `kubectl -n store logs job/store-bootstrap -c bootstrap --tail=100` and `kubectl -n store get events --sort-by=.lastTimestamp`. The bootstrap needs writable WordPress storage and HTTPS access to download WooCommerce.
- **Worker does not become ready:** check its logs, the API IPs, both role bindings and the admission checks from step 8. Keep new creation paused until the worker is ready.

For later code changes, use a new image tag when building, importing and updating Helm. Reusing a tag does not restart an existing pod by itself.

### PostgreSQL port-forwarding for local database tests

Browser access does not need a port-forward. Use one only when running gateway code or database tests from WSL against the cluster's PostgreSQL.

In a separate WSL terminal:

~~~bash
kubectl --context k3d-urumi -n platform port-forward \
  service/postgres 5433:5432 --address=127.0.0.1
~~~

Keep that terminal open. In the terminal where you will run the tests, start from the project folder and load the local settings:

~~~bash
source scripts/gateway-local-env.sh
~~~

This sets the database port to `5433` and reads the needed credentials from Kubernetes Secrets. You do not need to run `alembic upgrade head` manually for the normal in-cluster setup; the gateway init container already does it.

## VPS / k3s setup

This guide uses the same `charts/platform` and `charts/store` as the local setup. Run the blocks in order and stop if a command fails. It describes a fresh, single-node IPv4 k3s installation. The `urumi.example.com` domain and `ghcr.io/urumi-example` registry below are examples. Use your own domain and public registry account.

### 1. Prepare the server and domain

You need a Linux server with SSH access, a public IPv4 address and disk space for the application images and volumes. The production values request 10 GiB for PostgreSQL and 20 GiB for each managed store's two volumes. Leave room for the original store and the operating system too.

On the server, install Helm 3.22.0, uv, Python 3, OpenSSL and curl using the tool links in the local prerequisites. Docker is needed on the image-build machine; k3s uses its own container runtime on the server.

Create these DNS A records, pointing to the server's public IPv4 address:

| DNS name | Purpose |
|---|---|
| `app.<your-domain>` | Dashboard and gateway |
| `store.<your-domain>` | Original store |
| `*.shops.<your-domain>` | Dashboard-created stores |

Allow incoming HTTPS on port 443 and HTTP on port 80. Restrict SSH and Kubernetes API access to your management network. Keep the pod/service traffic needed by k3s working; its [network requirements](https://docs.k3s.io/installation/requirements) list the relevant ports and default ranges.

You also need a trusted TLS certificate and its private key. The certificate must cover the three names above, including the wildcard. A wildcard certificate can be issued through an ACME [DNS-01 challenge](https://letsencrypt.org/docs/challenge-types/#dns-01-challenge). These charts use an existing certificate; they do not install a certificate issuer.

### 2. Install k3s

Run this on the fresh Linux server, following the [k3s installation guide](https://docs.k3s.io/quick-start):

~~~bash
curl -sfL https://get.k3s.io -o /tmp/urumi-k3s-install.sh
~~~

Review the downloaded script, then install:

~~~bash
sudo sh /tmp/urumi-k3s-install.sh
mkdir -p -m 700 "$HOME/.kube"
sudo install -m 600 -o "$(id -u)" -g "$(id -g)" \
  /etc/rancher/k3s/k3s.yaml "$HOME/.kube/urumi-vps.yaml"
export KUBECONFIG="$HOME/.kube/urumi-vps.yaml"
kubectl config rename-context default urumi-vps
kubectl config current-context
kubectl wait --for=condition=Ready nodes --all --timeout=180s
kubectl -n kube-system rollout status deployment/traefik --timeout=180s
kubectl -n kube-system rollout status deployment/local-path-provisioner --timeout=180s
kubectl get storageclass local-path
kubectl api-resources --api-group=admissionregistration.k8s.io
kubectl api-resources --api-group=traefik.io
~~~

The context should be `urumi-vps`. Keep `KUBECONFIG` set in this terminal. Do not run these deployment commands against `k3d-urumi`.

Use Kubernetes 1.30 or newer. Keep the default CoreDNS, Traefik, local-path provisioner and NetworkPolicy controller enabled. The API lists must include ValidatingAdmissionPolicy, its binding, Middleware and TLSStore. See the [k3s networking notes](https://docs.k3s.io/networking/networking-services) for the bundled controllers.

This guide uses k3s's default pod range `10.42.0.0/16` and service range `10.43.0.0/16`. A cluster using different ranges also needs matching `gateway.trustedProxyCidrs` and `gateway.upstreamSecurity.internalCidrs` values.

### 3. Build and push the six images

Run this part on your Docker build machine, from the project root. Choose a new tag for each release. Replace the example registry with an account you control, and make these application image repositories publicly readable.

~~~bash
export URUMI_REGISTRY='ghcr.io/urumi-example'
export URUMI_TAG='0.2.0'

docker login ghcr.io

docker build -t "$URUMI_REGISTRY/woo-mcp:$URUMI_TAG" apps/woo-mcp &&
docker build -t "$URUMI_REGISTRY/mcp-analytics:$URUMI_TAG" apps/mcp-analytics &&
docker build -t "$URUMI_REGISTRY/mcp-notes:$URUMI_TAG" apps/mcp-notes &&
docker build -t "$URUMI_REGISTRY/urumi-gateway:$URUMI_TAG" apps/gateway &&
docker build -t "$URUMI_REGISTRY/urumi-dashboard:$URUMI_TAG" apps/dashboard &&
docker build -f apps/gateway/Dockerfile.provisioner \
  -t "$URUMI_REGISTRY/urumi-store-provisioner:$URUMI_TAG" .
~~~

Build for the server's CPU architecture. For example, an amd64 image needs an amd64 server. The worker image must come from this checkout because it contains the store chart.

Check the worker package, then push the images:

~~~bash
docker run --rm --network none --read-only \
  --cap-drop ALL --security-opt=no-new-privileges:true \
  --entrypoint python "$URUMI_REGISTRY/urumi-store-provisioner:$URUMI_TAG" \
  -m app.store_worker_main --check-package

docker push "$URUMI_REGISTRY/woo-mcp:$URUMI_TAG" &&
docker push "$URUMI_REGISTRY/mcp-analytics:$URUMI_TAG" &&
docker push "$URUMI_REGISTRY/mcp-notes:$URUMI_TAG" &&
docker push "$URUMI_REGISTRY/urumi-gateway:$URUMI_TAG" &&
docker push "$URUMI_REGISTRY/urumi-dashboard:$URUMI_TAG" &&
docker push "$URUMI_REGISTRY/urumi-store-provisioner:$URUMI_TAG"
~~~

Every push must finish successfully. k3d image import is only for the local cluster; the VPS pulls from the registry. Public images also let new store namespaces pull their MCP images without receiving registry credentials.

### 4. Set the deployment values

Copy or check out the same project on the server. Run the remaining commands there, from the project root. Use the registry and tag you just pushed, and replace the example domain:

~~~bash
export URUMI_DOMAIN='urumi.example.com'
export URUMI_REGISTRY='ghcr.io/urumi-example'
export URUMI_TAG='0.2.0'
export URUMI_CONFIG_DIR="$HOME/.config/urumi"
mkdir -p -m 700 "$URUMI_CONFIG_DIR"

cat > "$URUMI_CONFIG_DIR/platform.yaml" <<YAML
gateway:
  image: "$URUMI_REGISTRY/urumi-gateway:$URUMI_TAG"
  stores:
    baseDomain: "shops.$URUMI_DOMAIN"
dashboard:
  image: "$URUMI_REGISTRY/urumi-dashboard:$URUMI_TAG"
notes:
  image: "$URUMI_REGISTRY/mcp-notes:$URUMI_TAG"
ingress:
  host: "app.$URUMI_DOMAIN"
storeProvisioner:
  image: "$URUMI_REGISTRY/urumi-store-provisioner:$URUMI_TAG"
  adminEmail: "owner@$URUMI_DOMAIN"
  profile:
    wooMcp:
      image: "$URUMI_REGISTRY/woo-mcp:$URUMI_TAG"
    analytics:
      image: "$URUMI_REGISTRY/mcp-analytics:$URUMI_TAG"
YAML

cat > "$URUMI_CONFIG_DIR/store.yaml" <<YAML
storeName: Urumi
storeHost: "store.$URUMI_DOMAIN"
wooMcp:
  image: "$URUMI_REGISTRY/woo-mcp:$URUMI_TAG"
analytics:
  image: "$URUMI_REGISTRY/mcp-analytics:$URUMI_TAG"
YAML
~~~

These files contain deployment settings, not passwords. Keep them for later Helm updates.

Use `values-prod.yaml` and these overrides together. Do not add the local values files. The production values already set HTTPS, secure cookies, Traefik, storage sizes and matching managed-store quotas.

Managed-store storage checks currently support `local-path` with provisioner `rancher.io/local-path` and reclaim policy `Delete`. This is node-local storage. It preserves data across pod restarts on that node, but it does not replicate data to another server.

### 5. Create new Secrets and install the certificate

Prepare Python dependencies and generate this environment's credentials:

~~~bash
uv python install 3.12
uv sync --project apps/gateway --frozen
bash scripts/platform-secrets.sh
bash scripts/gateway-secrets.sh
bash scripts/secrets.sh
~~~

Use fresh credentials for this server. The scripts preserve existing Secrets. The store script reads a root `.env` if one exists, so check that file before running it.

Create the Groq Secret if it does not already exist:

~~~bash
read -rsp "Groq API key: " URUMI_GROQ_KEY
printf '\n'
kubectl -n platform create secret generic gateway-llm-secrets \
  --from-literal=GROQ_API_KEY="$URUMI_GROQ_KEY"
unset URUMI_GROQ_KEY
~~~

Set the paths to your certificate chain and matching private key, then create the TLS Secret:

~~~bash
export URUMI_TLS_CERT="$HOME/certs/urumi/fullchain.pem"
export URUMI_TLS_KEY="$HOME/certs/urumi/privkey.pem"

openssl x509 -in "$URUMI_TLS_CERT" -noout -dates -ext subjectAltName
kubectl -n platform create secret tls urumi-public-tls \
  --cert="$URUMI_TLS_CERT" --key="$URUMI_TLS_KEY"
~~~

Check that the displayed certificate names cover the app, original store and managed-store wildcard. If this Secret already exists, keep it unless you are deliberately renewing the certificate.

The platform chart references this Secret through Traefik's default TLSStore. Use one TLSStore named `default` for this controller, and keep the platform namespace in the controller's watched namespaces. The wildcard private key stays in `platform`. See [Traefik's TLSStore reference](https://doc.traefik.io/traefik/reference/routing-configuration/kubernetes/crd/tls/tlsstore/).

### 6. Render and install the platform and original store

Check the charts before installing:

~~~bash
helm lint charts/platform -f charts/platform/values-prod.yaml \
  -f "$URUMI_CONFIG_DIR/platform.yaml"
helm lint charts/store -f charts/store/values-prod.yaml \
  -f "$URUMI_CONFIG_DIR/store.yaml"

helm template platform charts/platform -n platform \
  -f charts/platform/values-prod.yaml \
  -f "$URUMI_CONFIG_DIR/platform.yaml" > /dev/null
helm template store charts/store -n store \
  -f charts/store/values-prod.yaml \
  -f "$URUMI_CONFIG_DIR/store.yaml" > /dev/null
~~~

Stop if a check fails. Install the platform first so the default TLS certificate reference is present:

~~~bash
helm upgrade --install platform charts/platform -n platform \
  -f charts/platform/values-prod.yaml \
  -f "$URUMI_CONFIG_DIR/platform.yaml" \
  --wait --timeout 10m

helm upgrade --install store charts/store -n store \
  -f charts/store/values-prod.yaml \
  -f "$URUMI_CONFIG_DIR/store.yaml" \
  --wait --timeout 20m
~~~

Keep hooks enabled for the original store install. Its bootstrap Job installs and configures WordPress/WooCommerce. The gateway applies its database migrations in the `migrate` init container. The original WooCommerce and analytics entries become reachable after the store is ready.

### 7. Verify the worker's permissions and start it

Fresh databases start with store creation paused. Keep **Allow new stores** off during these checks. The production values install the worker's security definitions with the worker and namespace/storage bindings disabled.

~~~bash
python3 scripts/verify-provisioner-admission.py

helm upgrade platform charts/platform -n platform \
  --reuse-values \
  --set storeProvisioner.security.bindNamespaceRole=true \
  --wait --timeout 5m

python3 scripts/verify-provisioner-worker-rbac.py
~~~

The verification scripts create and remove temporary test resources. Continue only when they pass.

Read this server's actual Kubernetes API destinations:

~~~bash
export URUMI_API_SERVICE_IP="$(kubectl -n default get service kubernetes \
  -o jsonpath='{.spec.clusterIP}')"
export URUMI_API_ENDPOINT_IP="$(kubectl -n default get endpoints kubernetes \
  -o jsonpath='{.subsets[0].addresses[0].ip}')"
export URUMI_API_ENDPOINT_PORT="$(kubectl -n default get endpoints kubernetes \
  -o jsonpath='{.subsets[0].ports[0].port}')"

python3 - <<'PY'
import ipaddress
import os
for field in ("URUMI_API_SERVICE_IP", "URUMI_API_ENDPOINT_IP"):
    assert ipaddress.ip_address(os.environ[field]).version == 4, field
assert 1 <= int(os.environ["URUMI_API_ENDPOINT_PORT"]) <= 65535
print("API destinations are valid IPv4 addresses and a port.")
PY

cat > "$URUMI_CONFIG_DIR/worker.yaml" <<YAML
storeProvisioner:
  enabled: true
  apiServer:
    serviceCidr: "$URUMI_API_SERVICE_IP/32"
    endpointCidr: "$URUMI_API_ENDPOINT_IP/32"
    endpointPort: $URUMI_API_ENDPOINT_PORT
  security:
    bindNamespaceRole: true
    bindStorageRole: true
YAML

helm upgrade platform charts/platform -n platform \
  --reuse-values -f "$URUMI_CONFIG_DIR/worker.yaml" \
  --wait --timeout 5m
~~~

This replaces the documentation-only API endpoint in the example production values. The worker's NetworkPolicy uses these exact destinations.

~~~bash
kubectl -n platform rollout status deployment/store-provisioner --timeout=180s
kubectl -n platform get pods,pvc,ingress
kubectl -n store get pods,jobs,pvc,ingress
kubectl get tlsstores.traefik.io -A
kubectl -n platform logs deployment/store-provisioner -c store-provisioner --tail=40

kubectl auth can-i get persistentvolumes \
  --as=system:serviceaccount:platform:store-provisioner-platform
kubectl auth can-i list persistentvolumes \
  --as=system:serviceaccount:platform:store-provisioner-platform
kubectl auth can-i delete persistentvolumes \
  --as=system:serviceaccount:platform:store-provisioner-platform
~~~

The worker should be ready. The three permission answers should be `yes`, `yes` and `no`.

### 8. Check the public routes

~~~bash
curl --fail --silent --show-error --output /dev/null "https://app.$URUMI_DOMAIN/"
curl --fail --silent --show-error --output /dev/null "https://store.$URUMI_DOMAIN/"
kubectl -n platform exec deployment/gateway -c gateway -- alembic current
~~~

Keep certificate verification enabled. A certificate or DNS error needs fixing before browser use.

Sign in to the dashboard using the generated admin credentials described in local setup step 7. Refresh the three initial MCP servers. Enable new stores only after the worker checks pass. A store created with slug `assamtea` then uses `https://assamtea.shops.<your-domain>`.

For an existing installation, pause creation and wait for active creation/deletion work to finish before changing worker images. Use new image tags with Helm and retain the saved domain, storage, TLS and worker settings. Helm rollback changes manifests; it does not restore database contents. Keep the database, content data and required Secrets together in your backup plan.

## Using Urumi

The examples below use the local dashboard and store. With the VPS settings, use the configured HTTPS addresses instead.

### 1. Sign in and invite another user

Sign in as the administrator at `http://app.localtest.me`. Open **Users & invites → Members**, enter the member's email and create an invitation. Share the invitation link privately. The member opens it, chooses a password and signs in. Invitations expire after 24 hours.

In **Users & invites → Tool permissions**, select the member and grant the tools they need. Members start with no access. For the required product-and-order flow, grant these on the intended store:

- `list_products`, `get_product` and `create_product`.
- `list_orders` and `get_order`.
- `upload_media` if the member will attach product photos.
- The store's analytics tools for sales/count questions, and Notes tools for a two-upstream example.

Permissions are grouped by store/server. The original **WooCommerce** and **Sales Analytics** entries belong to the original store. A managed store has its name in its two server entries. A grant selects where a member can act; it does not make them the store owner.

### 2. Create a product in chat

Sign in as the member and open **Chat**. If several stores are available, name the destination:

> In Cloth Assam, create a published product named Guwahati Tea with price 299.

The response shows the tool activity and product link. Open that link and confirm the product is on the intended website. For a photo, attach a supported image of up to 2 MiB and select that same store as the image destination before sending.

The chat uses the signed-in user's grants and records their tool calls. Each turn allows up to three upstream actions. If a request needs more work, send a follow-up. After an uncertain write or a lost reply, inspect the tool activity and store before repeating a product-creation request.

### 3. Place a test order and check it

1. Open the product link, or go to `http://store.localtest.me/shop/` for the original store.
2. Add a product to the cart and open checkout.
3. Enter test billing/shipping details and choose **Cash on delivery**.
4. Place the order and note its order number.
5. Open the same store's `/wp-admin/`, sign in and check **WooCommerce → Orders**.

For the original store, its initial WordPress login is stored in the `store` namespace:

~~~bash
kubectl -n store get secret store-secrets \
  -o jsonpath='{.data.WP_ADMIN_USER}' | base64 --decode
printf '\n'
kubectl -n store get secret store-secrets \
  -o jsonpath='{.data.WP_ADMIN_PASSWORD}' | base64 --decode
printf '\n'
~~~

These are bootstrap credentials. A later password change takes precedence.

For a dashboard-created store, its owner opens **Stores → View details → Set WordPress password** and chooses the password directly on that store. New stores use username `admin` and a password of 6–16 characters. A platform administrator can use this setup only for a store they own. Dashboard login, WordPress login and gateway API keys are separate credentials.

Back in chat, ask:

> Show today's orders for Cloth Assam, including the order numbers and statuses.

The order appears only under the store where checkout happened. Analytics dates use the WordPress site timezone. COD makes testing checkout easy, but an order total is not proof that payment was collected.

### 4. Connect an external MCP client

As the second user, open **My API keys**, enter a name and create a key. Copy it when shown; the full key cannot be retrieved later.

Use these connection settings in an MCP client:

| Setting | Local value |
|---|---|
| Transport | Streamable HTTP |
| Gateway URL | `http://app.localtest.me/mcp` |
| Header name | `Authorization` |
| Header value | `Bearer <your-urm_-API-key>` |

Use the member's gateway key here, not a WordPress/WooCommerce credential. The same one URL exposes all of that member's permitted enabled tools.

For MCP Inspector, use a machine with Node.js 22.19 or newer, then run:

~~~bash
npx @modelcontextprotocol/inspector
~~~

Open the local URL printed by Inspector. Choose Streamable HTTP, enter the gateway URL and add the Authorization header. Keep Inspector's normal proxy/token settings. Its per-launch proxy token is separate from the Urumi API key. The [Inspector documentation](https://modelcontextprotocol.io/docs/tools/inspector) describes the client.

Connect and list tools. Names use `server__tool`, for example `woo__list_orders` or a managed store's `woo-<store>-<id>__list_orders`. Call the exact order-list tool returned for the intended store. For example, list recent orders with:

~~~json
{
  "status": "any",
  "per_page": 20,
  "page": 1
}
~~~

The result should include the test order. Add ISO-8601 `after` and `before` filters if you need a specific day. A client that supports prompts can ask the same orders question as the dashboard; Inspector lets you call the tools directly.

Revoke this key in **My API keys** and make another request with the old key. It should be rejected. Creating another key does not restore a revoked one.

### 5. Use more than one upstream

Grant the member the selected store's order/analytics reads and Notes `add_note`. In chat, ask:

> Count today's orders for Cloth Assam and save that count in a team note titled Daily order check.

Check the activity for calls to the store's analytics server and **Team Notes**. Notes are shared team data, so use test content.

To compare stores, grant both stores' analytics tools and ask:

> Compare today's order counts for Cloth Assam and my second store. Show each store separately.

The activity should show the two different qualified analytics tools. A zero result is valid when no orders match.

### 6. Check access, failures and audits

- **Audit history:** members see their own tool calls. Administrators can review calls across users, including server, tool, source, time, duration and outcome.
- **Disable an upstream:** in **MCP servers**, disable the chosen entry. Its tools disappear on the next list/refresh, and direct calls are rejected. Re-enable it to restore access.
- **Unreachable upstream:** stop only the chosen test service, refresh its server status and check that another upstream still works. An unreachable server is reported separately.
- **Monitoring:** administrators can review per-server calls, errors/latency and store jobs. Its alerts appear on the page when refreshed.

For a controlled local failure check, this stops and restores the original analytics Deployment:

~~~bash
kubectl -n store scale deployment/mcp-analytics --replicas=0
~~~

Refresh **Sales Analytics** and call a Notes or WooCommerce tool. Then restore it:

~~~bash
kubectl -n store scale deployment/mcp-analytics --replicas=1
kubectl -n store rollout status deployment/mcp-analytics --timeout=180s
~~~

Refresh its status again. Use this check on a test installation because analytics calls are unavailable while its replica count is zero.

### 7. Create, disable or delete a managed store

An administrator enables creation in **Usage limits → Store provisioning** and sets the per-user, total-store, parallel-creation and timeout limits.

The member opens **Stores → Create store**, enters a display name and website slug, and submits once. Display names may contain spaces. Slugs such as `assamtea` and `assam-tea` are valid website labels. The page shows provisioning progress, then Ready or a failure reason. There is no separate admin acceptance step.

A Ready store has its own website, WordPress/MySQL data and two MCP registrations. The administrator grants the desired tools before a member uses them. Store ownership controls password setup; tool grants control business operations.

Administrators can disable/enable a Ready store. Disablement shows maintenance and blocks its MCP tools while preserving its data. The owner or administrator can delete a store. Deletion removes the store's namespace, storage and MCP registrations after cleanup is verified. Confirm the selected store before deleting it.

## Design and security notes

- [System design and tradeoffs](docs/system-design.md)
- [Detailed components, interfaces and feature locations](docs/system-design-reference.md)
- [Threat model](docs/threat-model.md)

## Files to keep out of GitHub

Use Git to upload the source. The root `.gitignore` excludes local `.env` files, Python virtual environments and caches, `node_modules`, dashboard `dist`, logs, kubeconfigs, private keys and database backups. Keep these on your machine.

Include `.env.example`, the Helm values and templates, Dockerfiles, `uv.lock`, `package-lock.json`, migrations, tests, scripts and docs. The example environment file has empty password and API key fields. Generate real credentials during setup.

Before a commit, check `git status --short --ignored` and review `git diff --cached`. Ignoring a file does not remove it if it was already committed. Do not include terminal output or screenshots containing passwords or API keys.
