# Sourced by bootstrap.sh using /bin/sh.
# Uses the existing $WP and $ADMIN variables.

seed_option() {
  if $WP option update "$1" "$2"; then
    return 0
  else
    echo "[seed] ERROR: could not update option $1" >&2
    return 1
  fi
}

seed_store() {
  echo "[seed] site settings"

  seed_option blogname "$STORE_NAME" || return 1
  seed_option blogdescription \
    "Fresh tea from the hills of Assam" || return 1
  seed_option woocommerce_default_country "IN:AS" || return 1
  seed_option woocommerce_currency "INR" || return 1

  echo "[seed] customer accounts"

  seed_option woocommerce_enable_myaccount_registration yes || return 1
  seed_option woocommerce_enable_guest_checkout yes || return 1
  seed_option woocommerce_enable_signup_and_login_from_checkout yes || return 1
  seed_option woocommerce_registration_generate_password no || return 1

  echo "[seed] done"
}

seed_store || return 1
