#!/bin/sh
set -eu

WP="php -d memory_limit=512M $(command -v wp) --path=/var/www/html"
ADMIN="--user=$WP_ADMIN_USER"
: "${STORE_NAME:?STORE_NAME is required}"
: "${WOOCOMMERCE_VERSION:?WOOCOMMERCE_VERSION is required}"

echo "[bootstrap] waiting for wp-config.php"
until [ -f /var/www/html/wp-config.php ]; do sleep 3; done

echo "[bootstrap] waiting for database"
until php -r 'exit(@mysqli_connect(getenv("DB_HOST"), getenv("DB_USER"), getenv("DB_PASSWORD"), getenv("DB_NAME")) ? 0 : 1);' 2>/dev/null; do sleep 3; done

if ! $WP core is-installed 2>/dev/null; then
  echo "[bootstrap] installing WordPress"
  $WP core install --url="$STORE_URL" --title="$STORE_NAME" \
    --admin_user="$WP_ADMIN_USER" --admin_password="$WP_ADMIN_PASSWORD" \
    --admin_email="$WP_ADMIN_EMAIL" --skip-email
else
  echo "[bootstrap] WordPress already installed"
fi

echo "[bootstrap] permalinks"
$WP rewrite structure '/%postname%/' --hard

echo "[bootstrap] WooCommerce plugin"
if ! $WP plugin is-installed woocommerce; then
  $WP plugin install woocommerce --version="$WOOCOMMERCE_VERSION"
fi
INSTALLED_WOOCOMMERCE=$($WP plugin get woocommerce --field=version)
if [ "$INSTALLED_WOOCOMMERCE" != "$WOOCOMMERCE_VERSION" ]; then
  echo "[bootstrap] WooCommerce version does not match the configured version" >&2
  exit 1
fi
$WP plugin activate woocommerce

echo "[bootstrap] store settings"
$WP option update woocommerce_coming_soon no
$WP option update woocommerce_store_pages_only no
$WP wc tool run install_pages $ADMIN
$WP wc payment_gateway update cod --enabled=true $ADMIN

echo "[bootstrap] shipping"
ZONE0_IDS=$($WP wc shipping_zone_method list 0 --field=id $ADMIN)
if [ -z "$ZONE0_IDS" ]; then
  $WP wc shipping_zone_method create 0 --method_id=free_shipping $ADMIN
fi

echo "[bootstrap] API keys"
$WP eval-file /scripts/keys.php

echo "[bootstrap] media account"
$WP eval-file /scripts/media.php

. /scripts/seed.sh

echo "[bootstrap] done"
