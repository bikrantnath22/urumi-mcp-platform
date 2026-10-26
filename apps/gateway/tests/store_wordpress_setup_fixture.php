<?php
define('ABSPATH', '/fixture/');
$source = base64_decode('__URUMI_SETUP_SOURCE_BASE64__', true);
$store = '02e539a9-ea63-4ffd-9133-1b527af88704';
putenv('URUMI_SETUP_STORE_ID=' . $store);
putenv('URUMI_SETUP_ENDPOINT=http://gateway.platform.svc.cluster.local:8000/api/store-wordpress-setup/redeem');
putenv('URUMI_SETUP_ADMIN_USER=admin');
putenv('URUMI_STORE_NAME=urumi');

function check($value, $message) { if (!$value) { throw new RuntimeException($message); } }
class WP_Error {}
function is_wp_error($value) { return $value instanceof WP_Error; }
function add_action($name, $callback) { check($name === 'login_form_urumi_setup', 'Wrong hook'); }
function is_multisite() { global $multisite; return $multisite; }
function get_user_by($field, $login) { global $user; return $user && $user->user_login === $login ? $user : false; }
function get_userdata($id) { global $user; return $user && $user->ID === $id ? $user : false; }
function user_can($item, $capability) { global $privileged; return $privileged && $capability === 'manage_options'; }
function wp_verify_nonce($nonce, $action) { return $nonce === 'fixture-nonce' && $action === 'urumi-wordpress-setup'; }
function wp_json_encode($data) { return json_encode($data); }
function wp_remote_post($url, $options) {
    global $calls, $response, $store, $writes;
    $body = json_decode($options['body'], true);
    check($url === 'http://gateway.platform.svc.cluster.local:8000/api/store-wordpress-setup/redeem', 'Changed gateway');
    check(array_keys($body) === array('store_id', 'token') && $body['store_id'] === $store, 'Unexpected credential forwarding');
    check($options['redirection'] === 0 && $options['timeout'] === 10 && $options['limit_response_size'] === 1024, 'Missing request bounds');
    check($writes === 0, 'Password changed before authorization');
    $calls++;
    return $response;
}
function wp_remote_retrieve_response_code($response) { return $response['code']; }
function wp_set_password($password, $id) {
    global $writes, $user, $failed_write;
    check($id === 7, 'Another account changed');
    $writes++;
    if (!$failed_write) { $user->user_pass = password_hash($password, PASSWORD_DEFAULT); }
}
function wp_check_password($password, $hash, $id) { return password_verify($password, $hash); }
function wp_clear_auth_cookie() {}
function update_option($key, $value) { global $title; check($key === 'blogname', 'Unrelated option'); $title = $value; }
function get_option($key) { global $title; return $title; }
class WP_Session_Tokens {
    static function get_instance($id) { check($id === 7, 'Wrong session owner'); return new self(); }
    function destroy_all() { global $destroyed; $destroyed++; }
}
class FakeDB {
    function suppress_errors($value) { return false; }
}
$wpdb = new FakeDB();
function fresh() {
    global $user, $multisite, $privileged, $calls, $writes, $response, $failed_write, $title, $destroyed;
    $user = (object) array('ID' => 7, 'user_login' => 'admin', 'roles' => array('administrator'), 'user_pass' => 'old-hash');
    $multisite = false; $privileged = true; $calls = 0; $writes = 0;
    $response = array('code' => 204); $failed_write = false; $title = 'Urumi Co.'; $destroyed = 0;
}
try {
    check(is_string($source) && strpos($source, '<?php') === 0, 'Missing plugin');
    eval(substr($source, 5));
    $token = str_repeat('A', 43); $password = 'FixturePass123';
    fresh();
    check(urumi_setup_apply($token, $password, $password, 'fixture-nonce'), 'Setup failed');
    check($calls === 1 && $writes === 1 && $destroyed === 1 && $title === 'urumi', 'Unexpected setup effects');
    check(password_verify($password, $user->user_pass), 'Password was not hashed');
    foreach (array(5, 6, 16, 17) as $length) {
        fresh();
        $candidate = str_repeat('a', $length);
        $allowed = $length >= 6 && $length <= 16;
        check(urumi_setup_apply($token, $candidate, $candidate, 'fixture-nonce') === $allowed, 'Wrong password length result');
        check($calls === (int) $allowed && $writes === (int) $allowed, 'Invalid length reached HTTP or storage');
    }
    echo "PASS: 6 and 16 characters pass; 5 and 17 stop before HTTP or writes\n";
    foreach (array('token', 'nonce', 'short', 'mismatch', 'customer', 'missing', 'privilege', 'multisite') as $case) {
        fresh(); $t = $token; $p = $password; $confirm = $password; $nonce = 'fixture-nonce';
        if ($case === 'token') { $t = 'invalid'; }
        if ($case === 'nonce') { $nonce = 'invalid'; }
        if ($case === 'short') { $p = $confirm = 'short'; }
        if ($case === 'mismatch') { $confirm = 'other'; }
        if ($case === 'customer') { $user->roles = array('customer'); }
        if ($case === 'missing') { $user = false; }
        if ($case === 'privilege') { $privileged = false; }
        if ($case === 'multisite') { $multisite = true; }
        check(!urumi_setup_apply($t, $p, $confirm, $nonce) && $calls === 0 && $writes === 0, 'Unauthorized setup sent HTTP');
    }
    foreach (array(array('code' => 403), array('code' => 503), array('code' => 302), new WP_Error()) as $failure) {
        fresh(); $response = $failure;
        check(!urumi_setup_apply($token, $password, $password, 'fixture-nonce'), 'Rejected token passed');
        check($calls === 1 && $writes === 0 && $title === 'Urumi Co.', 'Rejected token changed data');
    }
    fresh(); $failed_write = true;
    check(!urumi_setup_apply($token, $password, $password, 'fixture-nonce'), 'Failed write reported success');
    check($title === 'Urumi Co.' && $destroyed === 0, 'Failed password write continued');
    fresh(); $user->user_login = 'admin_' . str_replace('-', '', $store);
    putenv('URUMI_SETUP_ADMIN_USER=' . $user->user_login);
    check(urumi_setup_apply($token, $password, $password, 'fixture-nonce'), 'Legacy administrator cannot set password');
    putenv('URUMI_SETUP_ENDPOINT=http://attacker.invalid');
    fresh();
    check(!urumi_setup_apply($token, $password, $password, 'fixture-nonce') && $calls === 0, 'Unsafe destination accepted');
    echo "PASS: own passwords are hashed; store titles match and old administrator usernames remain compatible\n";
    echo "PASS: invalid capabilities, wrong users and unsafe destinations cannot change passwords\n";
    echo "PASS: authorization failures and failed password writes stop without further changes\n";
    echo "WordPress setup PHP fixtures passed. No real WordPress, database or HTTP calls were made.\n";
} catch (Throwable $error) {
    fwrite(STDERR, 'FAIL: ' . $error->getMessage() . "\n");
    exit(1);
}
