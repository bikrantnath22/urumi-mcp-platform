<?php
/* Standalone fixture: WordPress is not loaded; every database method is fake. */
define('ARRAY_A', 'ARRAY_A');
define('DB_NAME', 'urumi_key_fixture');
$helper = base64_decode('__URUMI_KEYS_SOURCE_BASE64__', true);

function check($condition, $label) {
    if (!$condition) {
        throw new RuntimeException($label);
    }
}
class FixtureCLIError extends RuntimeException {}
class WP_CLI {
    public static $messages = array();
    public static function error($message) {
        self::$messages[] = $message;
        throw new FixtureCLIError();
    }
    public static function success($message) {
        self::$messages[] = $message;
    }
}
function wc_api_hash($key) { return hash('sha256', $key); }
function get_user_by($field, $login) {
    global $fixture_admin;
    return $login === 'fixture_admin' ? $fixture_admin : false;
}
function user_can($user, $capability) {
    return $capability === 'manage_woocommerce' && $user->allowed;
}

class FixtureDB {
    public $prefix = 'wp_';
    public $last_error = '';
    public $rows = array();
    public $snapshot = null;
    public $mode = '';
    public $locked = false;
    public $suppressed = false;
    public $inserts = 0;
    public $calls = 0;

    public function suppress_errors($value) {
        $old = $this->suppressed;
        $this->suppressed = $value;
        return $old;
    }
    public function prepare($sql, ...$args) {
        return json_encode(array($sql, $args), JSON_THROW_ON_ERROR);
    }
    private function decode($sql) {
        return json_decode(str_replace(' FOR UPDATE', '', $sql), true, 512, JSON_THROW_ON_ERROR);
    }
    private function fault($stage) {
        $this->calls++;
        $this->last_error = '';
        if ($this->mode === $stage) {
            $this->last_error = 'private fixture SQL detail: ' . getenv('WC_RW_SECRET');
            return true;
        }
        return false;
    }
    public function get_var($query) {
        list($sql, $args) = $this->decode($query);
        if (strpos($sql, 'GET_LOCK') !== false) {
            if ($this->fault('lock')) { return null; }
            check(strlen($args[0]) <= 64, 'Invalid lock name');
            if ($this->mode === 'busy') { return '0'; }
            $this->locked = true;
            return '1';
        }
        if (strpos($sql, 'RELEASE_LOCK') !== false) {
            $this->locked = false;
            return $this->fault('release') ? null : '1';
        }
        check(strpos($sql, 'SELECT ENGINE') === 0, 'Unexpected scalar query');
        $this->fault('engine_error');
        return $this->mode === 'engine' ? 'MyISAM' : 'InnoDB';
    }
    public function query($sql) {
        if ($sql === 'START TRANSACTION') {
            if ($this->fault('start')) { return false; }
            check($this->locked, 'Transaction started without the setup lock');
            $this->snapshot = $this->rows;
        } elseif ($sql === 'COMMIT') {
            if ($this->fault('commit')) { return false; }
            $this->snapshot = null;
            if ($this->mode === 'commit_unknown') {
                $this->last_error = 'uncertain fixture acknowledgement';
                return false;
            }
        } elseif ($sql === 'ROLLBACK') {
            $this->fault('rollback');
            if ($this->snapshot !== null) { $this->rows = $this->snapshot; }
            $this->snapshot = null;
        } else {
            throw new RuntimeException('Unexpected write query');
        }
        return 0;
    }
    public function get_results($query, $format) {
        list($sql, $args) = $this->decode($query);
        check($format === ARRAY_A && count($args) === 2, 'Unexpected row query');
        if ($this->fault('read')) { return array(); }
        if ($this->mode === 'bad_rows') { return null; }
        if ($this->mode === 'readback' && $this->snapshot === null) {
            $this->last_error = 'private fixture readback detail';
            return array();
        }
        return array_slice(array_values(array_filter(
            $this->rows,
            static function ($row) use ($args) {
                return $row['description'] === $args[0] || $row['consumer_key'] === $args[1];
            }
        )), 0, 2);
    }
    public function insert($table, $row, $formats) {
        $this->inserts++;
        if ($this->fault('insert' . $this->inserts)) { return false; }
        check($this->snapshot !== null && $this->locked, 'Unprotected insert');
        check($table === 'wp_woocommerce_api_keys', 'Wrong target table');
        $ids = array_column($this->rows, 'key_id');
        $row['key_id'] = $ids ? max($ids) + 1 : 1;
        $row['nonces'] = 'preserved fixture metadata';
        $row['last_access'] = null;
        $this->rows[] = $row;
        return 1;
    }
    public function __call($name, $args) {
        throw new RuntimeException('Existing records must not be rewritten');
    }
}

function fresh($mode = '') {
    global $wpdb, $fixture_admin;
    foreach (array(
        'WP_ADMIN_USER' => 'fixture_admin',
        'WC_RW_KEY' => 'ck_' . str_repeat('a', 40),
        'WC_RW_SECRET' => 'cs_' . str_repeat('b', 40),
        'WC_RO_KEY' => 'ck_' . str_repeat('c', 40),
        'WC_RO_SECRET' => 'cs_' . str_repeat('d', 40),
    ) as $name => $value) { putenv($name . '=' . $value); }
    $fixture_admin = (object) array('ID' => 7, 'allowed' => true);
    $wpdb = new FixtureDB();
    $wpdb->mode = $mode;
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
    check($output === '', 'Helper emitted unexpected output');
    check($wpdb->suppressed === false, 'Error suppression was not restored');
    check(!$wpdb->locked && $wpdb->snapshot === null, 'Fixture ownership was not released');
    check(count(WP_CLI::$messages) === 1, 'Unexpected completion messages');
    $message = implode(' ', WP_CLI::$messages);
    foreach (array('WC_RW_KEY', 'WC_RW_SECRET', 'WC_RO_KEY', 'WC_RO_SECRET') as $name) {
        check(strpos($message, getenv($name)) === false, 'Credential appeared in a message');
    }
    check(strpos($message, 'private fixture') === false, 'Raw SQL diagnostic escaped');
    return $ok;
}

try {
    check(is_string($helper) && strpos($helper, '<?php') === 0, 'Missing helper source');
    fresh();
    check(invoke() && count($wpdb->rows) === 2, 'Initial key creation failed');
    check($wpdb->rows[0]['permissions'] === 'read_write', 'Woo scope changed');
    check($wpdb->rows[1]['permissions'] === 'read', 'Analytics scope changed');
    $wpdb->rows[0]['last_access'] = '2026-01-01 00:00:00';
    $good = $wpdb->rows;
    $before = $wpdb->rows;
    check(invoke() && $wpdb->rows === $before && $wpdb->inserts === 2, 'Replay changed keys or metadata');

    $unrelated = $good[0];
    $unrelated['key_id'] = 77;
    $unrelated['description'] = 'third-party application';
    $unrelated['consumer_key'] = hash('sha256', 'unrelated fixture key');
    $wpdb->rows[] = $unrelated;
    $before = $wpdb->rows;
    check(invoke() && $wpdb->rows === $before, 'Unrelated key changed');
    foreach (array(0, 1) as $index) {
        fresh();
        $wpdb->rows = array($good[$index]);
        check(invoke() && count($wpdb->rows) === 2, 'Partial setup did not recover');
        check(in_array($good[$index], $wpdb->rows, true), 'Recovery changed the existing key');
    }
    echo "PASS: creation, separate scopes, replay, partial recovery and unrelated keys\n";

    foreach (array('user_id', 'description', 'permissions', 'consumer_key', 'consumer_secret', 'truncated_key') as $field) {
        fresh();
        $wpdb->rows = $good;
        $wpdb->rows[0][$field] = $field === 'user_id' ? 99 : 'conflicting fixture value';
        $before = $wpdb->rows;
        check(!invoke() && $wpdb->rows === $before, 'Conflicting key was adopted or changed');
    }
    fresh();
    $wpdb->rows = array($good[0], $good[0], $good[1]);
    $wpdb->rows[1]['key_id'] = 88;
    $before = $wpdb->rows;
    check(!invoke() && $wpdb->rows === $before, 'Duplicate key records were accepted');
    echo "PASS: conflicting ownership, credentials, scopes and duplicate records are rejected\n";

    foreach (array('lock', 'busy', 'engine', 'engine_error', 'start', 'read', 'bad_rows', 'insert1', 'insert2', 'commit', 'commit_unknown', 'readback', 'release') as $mode) {
        fresh($mode);
        check(!invoke(), 'Injected storage failure was reported as success');
        $committed = in_array($mode, array('commit_unknown', 'readback', 'release'), true);
        check(count($wpdb->rows) === ($committed ? 2 : 0), 'Rollback or committed recovery state changed');
        $before = $wpdb->rows;
        $inserts = $wpdb->inserts;
        $wpdb->mode = '';
        check(invoke() && count($wpdb->rows) === 2, 'Retry could not recover');
        if ($committed) {
            check($wpdb->rows === $before && $wpdb->inserts === $inserts, 'Retry repeated a committed write');
        }
    }
    echo "PASS: storage and lock failures stop setup; rollback and uncertain-commit recovery preserve keys\n";

    foreach (array('WC_RW_KEY', 'WC_RW_SECRET', 'WC_RO_KEY', 'WC_RO_SECRET') as $name) {
        fresh();
        putenv($name . '=invalid-fixture-credential');
        check(!invoke() && $wpdb->calls === 0, 'Invalid input reached SQL');
    }
    foreach (array('WC_RO_KEY' => 'WC_RW_KEY', 'WC_RO_SECRET' => 'WC_RW_SECRET') as $target => $origin) {
        fresh();
        putenv($target . '=' . getenv($origin));
        check(!invoke() && $wpdb->calls === 0, 'Shared credentials reached SQL');
    }
    foreach (array(false, (object) array('ID' => 7, 'allowed' => false)) as $admin) {
        fresh();
        $fixture_admin = $admin;
        check(!invoke() && $wpdb->calls === 0, 'Unavailable administrator reached SQL');
    }
    echo "PASS: invalid inputs, shared credentials and unavailable administrators send no SQL\n";
    echo "WooCommerce key fixtures passed. No real database or WordPress API calls were made.\n";
} catch (Throwable $error) {
    fwrite(STDERR, "FAIL: " . $error->getMessage() . "\n");
    exit(1);
}
