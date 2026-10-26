<?php
/* Standalone fixtures only: WordPress is not loaded and all storage is fake. */
define('ARRAY_A', 'ARRAY_A');
define('DB_NAME', 'urumi_media_fixture');
$helper = base64_decode('__URUMI_MEDIA_SOURCE_BASE64__', true);
$legacy = '__URUMI_MEDIA_HASH_MODE__' === 'legacy';

function check($ok, $label) {
    if (!$ok) { throw new RuntimeException($label); }
}
class FixtureCLIError extends RuntimeException {}
class WP_CLI {
    public static $messages = array();
    public static function error($message) {
        self::$messages[] = $message;
        throw new FixtureCLIError();
    }
    public static function success($message) { self::$messages[] = $message; }
}
function fixture_hash($password, $style) {
    global $wpdb;
    $wpdb->hashes++;
    return $wpdb->mode === 'hash_failure' ? '' : $style . ':' . hash('sha256', $password);
}
function wp_hash_password($password) { return fixture_hash($password, 'legacy'); }
function wp_check_password($password, $hash) {
    return hash_equals('legacy:' . hash('sha256', $password), $hash);
}
class FixturePasswordConstants {
    const USERMETA_KEY_APPLICATION_PASSWORDS = '_application_passwords';
    const OPTION_KEY_IN_USE = 'using_application_passwords';
}
if ($legacy) {
    class WP_Application_Passwords extends FixturePasswordConstants {}
} else {
    class WP_Application_Passwords extends FixturePasswordConstants {
        public static function hash_password($password) {
            return fixture_hash($password, 'current');
        }
        public static function check_password($password, $hash) {
            return wp_check_password($password, $hash)
                || hash_equals('current:' . hash('sha256', $password), $hash);
        }
    }
}
class FixtureDB {
    public $prefix = 'wp_';
    public $usermeta = 'wp_usermeta';
    public $last_error = '';
    public $mode = '';
    public $locked = false;
    public $suppressed = false;
    public $rows = array();
    public $roles = array('administrator' => array('capabilities' => array('manage_options' => true)));
    public $users = array();
    public $active = false;
    public $calls = 0;
    public $role_writes = 0;
    public $user_writes = 0;
    public $meta_writes = 0;
    public $option_writes = 0;
    public $hashes = 0;
    public $uuids = 0;
    public $passwords = 0;

    public function fault($stage) {
        $this->calls++;
        $this->last_error = '';
        if ($this->mode === $stage) {
            $this->last_error = 'private fixture detail: ' . getenv('WP_MEDIA_APP_PASSWORD');
            return true;
        }
        return false;
    }
    public function suppress_errors($value) {
        $old = $this->suppressed;
        $this->suppressed = $value;
        return $old;
    }
    public function prepare($sql, ...$args) {
        return json_encode(array($sql, $args), JSON_THROW_ON_ERROR);
    }
    private function decode($query) {
        return json_decode($query, true, 512, JSON_THROW_ON_ERROR);
    }
    public function get_var($query) {
        list($sql, $args) = $this->decode($query);
        if (strpos($sql, 'GET_LOCK') !== false) {
            if ($this->fault('lock')) { return null; }
            check(strlen($args[0]) <= 64, 'Invalid setup lock');
            if ($this->mode === 'busy') { return '0'; }
            $this->locked = true;
            return '1';
        }
        check(strpos($sql, 'RELEASE_LOCK') !== false, 'Unexpected scalar query');
        $this->locked = false;
        return $this->fault('release') ? null : '1';
    }
    public function get_results($query, $format) {
        list($sql, $args) = $this->decode($query);
        check($format === ARRAY_A && $args === array(7, '_application_passwords'), 'Wrong metadata read');
        if ($this->fault('meta_read')) { return array(); }
        if ($this->mode === 'bad_rows') { return null; }
        if ($this->mode === 'readback' && $this->meta_writes > 0) {
            $this->last_error = 'private fixture readback failure';
            return array();
        }
        return array_slice($this->rows, 0, 2);
    }
    public function query($query) {
        list($sql, $args) = $this->decode($query);
        check($this->locked, 'Metadata write without setup ownership');
        if ($this->fault('meta_write')) { return false; }
        if (strpos($sql, 'INSERT INTO') === 0) {
            check(strpos($sql, 'HAVING COUNT(*) = 0') !== false, 'Missing insert guard');
            check(count($args) === 5 && $args[0] === 7 && $args[3] === 7, 'Wrong insertion target');
            if ($this->mode === 'competing_insert' && !$this->rows) {
                $this->rows = array(meta_row(array(other_item())));
            }
            if ($this->rows) { return 0; }
            $this->rows = array(array('umeta_id' => 11, 'meta_value' => $args[2]));
        } else {
            check(strpos($sql, 'UPDATE') === 0 && count($args) === 5, 'Unexpected metadata write');
            check(strpos($sql, 'CAST(meta_value AS BINARY)') !== false, 'Missing byte comparison');
            check($args[2] === 7 && $args[3] === '_application_passwords', 'Wrong update target');
            if ($this->mode === 'cas_race') {
                $items = unserialize($this->rows[0]['meta_value']);
                $items[] = other_item('concurrent');
                $this->rows[0]['meta_value'] = serialize($items);
            }
            if (count($this->rows) !== 1 || $this->rows[0]['umeta_id'] !== $args[1]
                || $this->rows[0]['meta_value'] !== $args[4]) { return 0; }
            $this->rows[0]['meta_value'] = $args[0];
        }
        $this->meta_writes++;
        if ($this->mode === 'write_unknown') {
            $this->last_error = 'private fixture uncertain acknowledgement';
            return false;
        }
        return 1;
    }
    public function __call($name, $args) {
        throw new RuntimeException('Unexpected broad storage operation');
    }
}

function wp_cache_delete($key, $group) { return true; }
function clean_user_cache($id) {}
function wp_roles() {
    return new class { public function for_site() {} };
}
function get_option($key) {
    global $wpdb;
    check($key === 'wp_user_roles', 'Unexpected option read');
    if ($wpdb->fault('role_read')) { return false; }
    if ($wpdb->mode === 'role_readback' && $wpdb->meta_writes > 0) { return false; }
    return $wpdb->roles;
}
function get_role($name) {
    global $wpdb;
    return isset($wpdb->roles[$name]) ? (object) $wpdb->roles[$name] : null;
}
function add_role($name, $label, $caps) {
    global $wpdb;
    if ($wpdb->fault('role_write')) { return null; }
    check(!isset($wpdb->roles[$name]), 'An existing role was rewritten');
    $wpdb->roles[$name] = array('name' => $label, 'capabilities' => $caps);
    $wpdb->role_writes++;
    return get_role($name);
}
function get_user_by($field, $value) {
    global $wpdb;
    if ($wpdb->fault('user_read')) { return false; }
    if ($wpdb->mode === 'user_readback' && $field === 'id' && $wpdb->meta_writes > 0) {
        return false;
    }
    foreach ($wpdb->users as $user) {
        if (($field === 'id' && $user->ID === $value)
            || ($field === 'login' && $user->user_login === $value)) { return $user; }
    }
    return false;
}
class WP_Error {}
function is_wp_error($value) { return $value instanceof WP_Error; }
function wp_insert_user($data) {
    global $wpdb;
    if ($wpdb->fault('user_write')) { return new WP_Error(); }
    check(!$wpdb->users, 'An existing user was rewritten');
    $wpdb->users[7] = (object) array(
        'ID' => 7, 'user_login' => $data['user_login'], 'user_email' => $data['user_email'],
        'roles' => array($data['role']), 'caps' => array($data['role'] => true),
        'user_pass' => hash('sha256', $data['user_pass']), 'super' => false,
    );
    $wpdb->user_writes++;
    return $wpdb->mode === 'user_write_unknown' ? new WP_Error() : 7;
}
function user_can($user, $cap) { return !empty($user->caps[$cap]); }
function is_super_admin($id) {
    global $wpdb;
    return $wpdb->users[$id]->super;
}
function wp_generate_password($length, $special, $extra) {
    global $wpdb;
    $wpdb->passwords++;
    return str_repeat('Z', $length);
}
function wp_generate_uuid4() {
    global $wpdb;
    return sprintf('00000000-0000-4000-8000-%012d', ++$wpdb->uuids);
}
function wp_is_uuid($uuid) {
    return preg_match('/\A[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\z/i', $uuid) === 1;
}
function get_main_network_id() { return 1; }
function get_network_option($network, $key) {
    global $wpdb;
    check($network === 1 && $key === 'using_application_passwords', 'Wrong activation option');
    return $wpdb->fault('option_read') ? false : $wpdb->active;
}
function update_network_option($network, $key, $value) {
    global $wpdb;
    if ($wpdb->fault('option_write')) { return false; }
    if (!$wpdb->active) { $wpdb->option_writes++; }
    $wpdb->active = $value;
    return true;
}

function fresh($mode = '') {
    global $wpdb;
    putenv('WP_MEDIA_USER=fixture_media');
    putenv('WP_ADMIN_USER=fixture_admin');
    putenv('WP_MEDIA_APP_PASSWORD=' . str_repeat('A1b2', 6));
    $wpdb = new FixtureDB();
    $wpdb->mode = $mode;
}
function meta_row($items) {
    return array('umeta_id' => 11, 'meta_value' => serialize($items));
}
function other_item($name = 'unrelated') {
    return array(
        'uuid' => $name === 'concurrent' ? '22222222-2222-4222-8222-222222222222'
            : '11111111-1111-4111-8111-111111111111',
        'name' => $name, 'app_id' => '', 'password' => 'legacy:' . hash('sha256', $name),
        'created' => 100, 'last_used' => 200, 'last_ip' => '192.0.2.1',
    );
}
function state() {
    global $wpdb;
    return serialize(array($wpdb->roles, $wpdb->users, $wpdb->rows, $wpdb->active));
}
function counts() {
    global $wpdb;
    return array($wpdb->role_writes, $wpdb->user_writes, $wpdb->meta_writes,
        $wpdb->option_writes, $wpdb->hashes, $wpdb->uuids, $wpdb->passwords);
}
function seed($rows) {
    global $wpdb, $good_roles, $good_users;
    fresh();
    $wpdb->roles = $good_roles;
    $wpdb->users = unserialize(serialize($good_users));
    $wpdb->rows = $rows;
    $wpdb->active = true;
}
function invoke() {
    global $helper, $wpdb;
    WP_CLI::$messages = array();
    ob_start();
    try {
        eval('?>' . $helper);
        $ok = true;
    } catch (FixtureCLIError $error) {
        $ok = false;
    } finally {
        $output = ob_get_clean();
    }
    check($output === '', 'Unexpected helper output');
    check(!$wpdb->locked && !$wpdb->suppressed, 'Fixture ownership or error settings leaked');
    check(count(WP_CLI::$messages) === 1, 'Unexpected completion messages');
    $message = implode(' ', WP_CLI::$messages);
    $secret = getenv('WP_MEDIA_APP_PASSWORD');
    check(!$secret || strpos($message, $secret) === false, 'Credential escaped');
    check(strpos($message, 'private fixture') === false, 'Raw diagnostic escaped');
    return $ok;
}

try {
    check(is_string($helper) && strpos($helper, '<?php') === 0, 'Missing helper source');
    echo 'Testing fixture hash path: ' . ($legacy ? 'legacy' : 'current') . "\n";
    fresh();
    check(invoke() && count($wpdb->rows) === 1, 'Initial setup failed');
    check(counts() === array(1, 1, 1, 1, 1, 1, 1), 'Unexpected initial writes');
    $items = unserialize($wpdb->rows[0]['meta_value']);
    $items[0]['last_used'] = 123456;
    $items[0]['last_ip'] = '192.0.2.5';
    $wpdb->rows[0]['meta_value'] = serialize($items);
    $good_roles = $wpdb->roles;
    $good_users = unserialize(serialize($wpdb->users));
    $good_rows = $wpdb->rows;
    $before = state();
    $writes = counts();
    check(invoke() && state() === $before && counts() === $writes, 'Replay changed credentials or metadata');

    $legacy_items = $items;
    $legacy_items[0]['password'] = 'legacy:' . hash('sha256', getenv('WP_MEDIA_APP_PASSWORD'));
    seed(array(meta_row($legacy_items)));
    $before = state();
    check(invoke() && state() === $before && $wpdb->hashes === 0, 'Legacy credential was rotated');
    echo "PASS: creation and replay preserve the account, UUID, password hash and usage metadata\n";

    foreach (array(array(), array(meta_row(array())), array(meta_row(array(other_item())))) as $rows) {
        seed($rows);
        check(invoke(), 'Missing media credential did not recover');
        $saved = unserialize($wpdb->rows[0]['meta_value']);
        if ($rows && unserialize($rows[0]['meta_value'])) {
            check($saved[0] === other_item(), 'Unrelated credential changed');
        }
        $before = state();
        $writes = counts();
        check(invoke() && state() === $before && counts() === $writes, 'Recovery was not idempotent');
    }
    echo "PASS: absent and empty lists recover; unrelated application passwords remain unchanged\n";

    foreach (array('password', 'app_id', 'duplicate_name', 'duplicate_uuid', 'malformed', 'duplicate_rows') as $case) {
        seed($good_rows);
        $bad = unserialize($wpdb->rows[0]['meta_value']);
        if ($case === 'password') { $bad[0]['password'] = 'legacy:' . hash('sha256', 'wrong'); }
        if ($case === 'app_id') { $bad[0]['app_id'] = '33333333-3333-4333-8333-333333333333'; }
        if ($case === 'duplicate_name') {
            $extra = $bad[0]; $extra['uuid'] = other_item()['uuid']; $bad[] = $extra;
        }
        if ($case === 'duplicate_uuid') {
            $extra = other_item(); $extra['uuid'] = $bad[0]['uuid']; $bad[] = $extra;
        }
        $wpdb->rows = array(meta_row($bad));
        if ($case === 'malformed') { $wpdb->rows[0]['meta_value'] = 'invalid serialized data'; }
        if ($case === 'duplicate_rows') { $wpdb->rows[] = $wpdb->rows[0]; }
        $before = state();
        check(!invoke() && state() === $before && $wpdb->meta_writes === 0, 'Conflicting credential was changed');
    }
    foreach (array('role_extra', 'role_missing', 'email', 'user_roles', 'user_caps', 'super') as $case) {
        seed($good_rows);
        if ($case === 'role_extra') { $wpdb->roles['media_manager']['capabilities']['manage_options'] = true; }
        if ($case === 'role_missing') { unset($wpdb->roles['media_manager']['capabilities']['upload_files']); }
        if ($case === 'email') { $wpdb->users[7]->user_email = 'other@example.test'; }
        if ($case === 'user_roles') { $wpdb->users[7]->roles = array('administrator'); }
        if ($case === 'user_caps') { $wpdb->users[7]->caps['manage_options'] = true; }
        if ($case === 'super') { $wpdb->users[7]->super = true; }
        $before = state();
        check(!invoke() && state() === $before, 'An existing role or account was repaired or adopted');
    }
    echo "PASS: conflicting credentials, malformed storage and privileged or unrelated accounts are rejected\n";

    foreach (array('lock', 'busy', 'role_read', 'role_write', 'user_read', 'user_write',
        'user_write_unknown', 'meta_read', 'bad_rows', 'hash_failure', 'meta_write',
        'write_unknown', 'option_read', 'option_write', 'readback', 'role_readback',
        'user_readback', 'release') as $mode) {
        fresh($mode);
        check(!invoke(), 'Injected failure reported success');
        $saved_rows = $wpdb->rows;
        $saved_users = serialize($wpdb->users);
        $passwords = $wpdb->passwords;
        $hashes = $wpdb->hashes;
        $uuids = $wpdb->uuids;
        $meta_writes = $wpdb->meta_writes;
        $wpdb->mode = '';
        check(invoke(), 'Failed setup could not recover');
        check(count($wpdb->rows) === 1, 'Recovery duplicated metadata');
        if ($saved_rows) {
            check($wpdb->rows === $saved_rows && $wpdb->hashes === $hashes
                && $wpdb->uuids === $uuids && $wpdb->meta_writes === $meta_writes, 'Committed credential was repeated');
        }
        if ($saved_users !== serialize(array())) {
            check(serialize($wpdb->users) === $saved_users && $wpdb->passwords === $passwords, 'Recovery replaced an existing account');
        }
    }
    echo "PASS: storage failures and uncertain acknowledgements recover without rotating committed credentials\n";

    foreach (array('competing_insert', 'cas_race') as $mode) {
        if ($mode === 'cas_race') { seed(array(meta_row(array(other_item())))); }
        else { seed(array()); }
        $wpdb->mode = $mode;
        check(!invoke(), 'Concurrent metadata change was not rejected');
        $external = unserialize($wpdb->rows[0]['meta_value']);
        $wpdb->mode = '';
        check(invoke(), 'Concurrent-change recovery failed');
        $saved = unserialize($wpdb->rows[0]['meta_value']);
        foreach ($external as $entry) {
            check(in_array($entry, $saved, true), 'Concurrent credential was overwritten');
        }
        check(count($wpdb->rows) === 1, 'Concurrent recovery duplicated the metadata row');
    }
    foreach (array(
        array('WP_MEDIA_USER', 'UPPERCASE'),
        array('WP_MEDIA_USER', 'fixture_admin'),
        array('WP_MEDIA_APP_PASSWORD', ''),
        array('WP_MEDIA_APP_PASSWORD', 'invalid-password'),
    ) as $invalid) {
        fresh();
        putenv($invalid[0] . '=' . $invalid[1]);
        check(!invoke() && $wpdb->calls === 0, 'Invalid credentials reached storage');
    }
    echo "PASS: conditional writes preserve concurrent changes; invalid inputs send no SQL\n";
    echo "Media helper fixtures passed. No real database or WordPress API calls were made.\n";
} catch (Throwable $error) {
    fwrite(STDERR, 'FAIL: ' . $error->getMessage() . "\n");
    exit(1);
}
