<?php
/**
 * Plugin Name: Urumi WordPress password setup
 * A one-use dashboard capability authorizes a password change on this store.
 * The password is submitted directly to WordPress, never to the gateway.
 */
if (!defined('ABSPATH')) { exit; }

function urumi_setup_context() {
    $id = getenv('URUMI_SETUP_STORE_ID');
    $endpoint = getenv('URUMI_SETUP_ENDPOINT');
    $login = getenv('URUMI_SETUP_ADMIN_USER');
    $name = getenv('URUMI_STORE_NAME');
    if (!is_string($id) || !preg_match('/^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$/D', $id)
        || !is_string($endpoint)
        || !preg_match('~^http://gateway\.[a-z0-9][a-z0-9-]*\.svc\.cluster\.local:8000/api/store-wordpress-setup/redeem$~D', $endpoint)
        || !in_array($login, array('admin', 'admin_' . str_replace('-', '', $id)), true)
        || !is_string($name) || $name === '' || strlen($name) > 400
        || preg_match('/[\x00-\x1f\x7f]/', $name) || is_multisite()) {
        return false;
    }
    $user = get_user_by('login', $login);
    if (!$user || !in_array('administrator', $user->roles, true)
        || !user_can($user, 'manage_options')) { return false; }
    return array('id' => $id, 'endpoint' => $endpoint, 'user' => $user, 'name' => $name);
}

function urumi_setup_apply($token, $password, $confirm, $nonce) {
    $context = urumi_setup_context();
    if (!$context || !is_string($token) || !preg_match('/^[A-Za-z0-9_-]{43}$/D', $token)
        || !is_string($password) || !is_string($confirm)
        || !preg_match('/^.{6,16}$/usD', $password)
        || preg_match('/[\x00-\x1f\x7f]/', $password)
        || !hash_equals($password, $confirm)
        || !is_string($nonce) || !wp_verify_nonce($nonce, 'urumi-wordpress-setup')) {
        return false;
    }
    // The only authority is the scoped, live, one-use dashboard token.
    $response = wp_remote_post($context['endpoint'], array(
        'timeout' => 10, 'redirection' => 0, 'limit_response_size' => 1024,
        'headers' => array('Content-Type' => 'application/json'),
        'body' => wp_json_encode(array('store_id' => $context['id'], 'token' => $token)),
        'cookies' => array(),
    ));
    if (is_wp_error($response) || wp_remote_retrieve_response_code($response) !== 204) {
        return false;
    }
    // WordPress hashes the password. The immutable provisioning Secret stays intact:
    // its initial random admin password is a bootstrap credential, not the new login.
    global $wpdb;
    $previous = $wpdb->suppress_errors(true);
    try {
        $id = (int) $context['user']->ID;
        wp_set_password($password, $id);
        $saved = get_userdata($id);
        if (!$saved || !wp_check_password($password, $saved->user_pass, $id)) {
            return false;
        }
        WP_Session_Tokens::get_instance($id)->destroy_all();
        wp_clear_auth_cookie();
        update_option('blogname', $context['name']);
        return get_option('blogname') === $context['name'];
    } finally {
        $wpdb->suppress_errors($previous);
    }
}

function urumi_setup_page() {
    nocache_headers();
    header('Cache-Control: no-store, max-age=0');
    header('Referrer-Policy: no-referrer');
    header('X-Content-Type-Options: nosniff');
    header("Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'");
    $context = urumi_setup_context();
    $token = isset($_POST['urumi_setup_token']) && is_string($_POST['urumi_setup_token'])
        ? wp_unslash($_POST['urumi_setup_token']) : '';
    $valid = $_SERVER['REQUEST_METHOD'] === 'POST'
        && (!isset($_SERVER['CONTENT_LENGTH']) || (int) $_SERVER['CONTENT_LENGTH'] <= 4096)
        && preg_match('/^[A-Za-z0-9_-]{43}$/D', $token) && $context;
    $submitted = isset($_POST['urumi_setup_save']);
    $success = false;
    if ($valid && $submitted) {
        try {
            $success = urumi_setup_apply($token,
                isset($_POST['password']) ? wp_unslash($_POST['password']) : null,
                isset($_POST['confirm']) ? wp_unslash($_POST['confirm']) : null,
                isset($_POST['_wpnonce']) ? wp_unslash($_POST['_wpnonce']) : null);
        } catch (Throwable $error) { $success = false; }
    }
    status_header($success || ($valid && !$submitted) ? 200 : 403);
    echo '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">';
    echo '<title>Set WordPress password</title><style>body{font:16px system-ui;background:#fafafa;color:#171717;margin:0}main{max-width:28rem;margin:8vh auto;padding:2rem;background:white;border:1px solid #ddd;border-radius:12px}label{display:block;margin:1rem 0}input{box-sizing:border-box;display:block;width:100%;padding:.7rem;margin-top:.4rem}button{padding:.8rem 1rem;background:#171717;color:white;border:0;border-radius:6px}p{line-height:1.6}a{color:inherit}</style><main>';
    if ($success) {
        echo '<h1>Password saved</h1><p>Your WordPress username is <strong>' . esc_html($context['user']->user_login) . '</strong>.</p>';
        echo '<p><a href="' . esc_url(wp_login_url()) . '">Log in to WordPress</a></p>';
    } elseif (!$valid || $submitted) {
        echo '<h1>Setup could not be confirmed</h1><p>Return to your dashboard and start WordPress setup again. If you already saved a password, try logging in with it first.</p>';
    } else {
        echo '<h1>Set WordPress password</h1><p>Store: <strong>' . esc_html($context['name']) . '</strong><br>Username: <strong>' . esc_html($context['user']->user_login) . '</strong></p>';
        echo '<p>Choose a password of 6–16 characters. This changes only this store’s WordPress login. Its website title will use the store name shown above.</p>';
        echo '<form method="post" action="' . esc_url(site_url('wp-login.php?action=urumi_setup', 'login')) . '">';
        echo '<input type="hidden" name="urumi_setup_token" value="' . esc_attr($token) . '">';
        wp_nonce_field('urumi-wordpress-setup');
        echo '<label>New password<input name="password" type="password" autocomplete="new-password" required minlength="6" maxlength="16"></label>';
        echo '<label>Confirm password<input name="confirm" type="password" autocomplete="new-password" required minlength="6" maxlength="16"></label>';
        echo '<button type="submit" name="urumi_setup_save" value="1">Save WordPress password</button></form>';
    }
    echo '</main></html>';
    exit;
}
add_action('login_form_urumi_setup', 'urumi_setup_page');
