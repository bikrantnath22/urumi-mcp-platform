<?php
/* Reuse the Secret's keys; never delete, rotate or repair existing records. */
(static function () {
    global $wpdb;

    $previous = $wpdb->suppress_errors(true);
    $locked = false;
    $transaction = false;
    $failed = false;
    $lock_name = '';

    try {
        $table = $wpdb->prefix . 'woocommerce_api_keys';
        if (!preg_match('/\A[A-Za-z0-9_]{1,64}\z/', $table)
            || !function_exists('wc_api_hash')) {
            throw new RuntimeException('WooCommerce is unavailable.');
        }
        $user = get_user_by('login', getenv('WP_ADMIN_USER'));
        if (!$user || (int) $user->ID < 1 || !user_can($user, 'manage_woocommerce')) {
            throw new RuntimeException('Store administrator is unavailable.');
        }

        $definitions = array(
            array('woo-mcp (read/write)', 'read_write', 'WC_RW_KEY', 'WC_RW_SECRET'),
            array('analytics (read-only)', 'read', 'WC_RO_KEY', 'WC_RO_SECRET'),
        );
        $keys = array();
        foreach ($definitions as $definition) {
            list($description, $permission, $key_env, $secret_env) = $definition;
            $key = getenv($key_env);
            $secret = getenv($secret_env);
            if (!is_string($key) || !preg_match('/\Ack_[0-9a-f]{40}\z/', $key)
                || !is_string($secret) || !preg_match('/\Acs_[0-9a-f]{40}\z/', $secret)) {
                throw new RuntimeException('Invalid API credentials.');
            }
            $keys[] = array(
                'user_id' => (int) $user->ID,
                'description' => $description,
                'permissions' => $permission,
                'consumer_key' => wc_api_hash($key),
                'consumer_secret' => $secret,
                'truncated_key' => substr($key, -7),
            );
        }
        if ($keys[0]['consumer_key'] === $keys[1]['consumer_key']
            || $keys[0]['consumer_secret'] === $keys[1]['consumer_secret']) {
            throw new RuntimeException('API credentials must be independent.');
        }

        $read = static function ($expected, $for_update = false) use ($wpdb, $table) {
            $sql = $wpdb->prepare(
                "SELECT * FROM `$table` WHERE description = %s OR consumer_key = %s LIMIT 2",
                $expected['description'], $expected['consumer_key']
            );
            $rows = $wpdb->get_results($sql . ($for_update ? ' FOR UPDATE' : ''), ARRAY_A);
            if ($wpdb->last_error !== '' || !is_array($rows)) {
                throw new RuntimeException('API key lookup failed.');
            }
            return $rows;
        };
        $matches = static function ($rows, $expected) {
            if (count($rows) !== 1) {
                return false;
            }
            foreach ($expected as $column => $value) {
                if (!array_key_exists($column, $rows[0])
                    || !is_scalar($rows[0][$column])
                    || !hash_equals((string) $value, (string) $rows[0][$column])) {
                    return false;
                }
            }
            return true;
        };

        // Serialize bootstrap retries; WooCommerce does not make this key index unique.
        $lock_name = 'urumi-api-' . substr(hash('sha256', DB_NAME . ':' . $table), 0, 54);
        if ((string) $wpdb->get_var($wpdb->prepare('SELECT GET_LOCK(%s, 5)', $lock_name)) !== '1'
            || $wpdb->last_error !== '') {
            throw new RuntimeException('API key setup is busy.');
        }
        $locked = true;
        $engine = $wpdb->get_var($wpdb->prepare(
            'SELECT ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s',
            $table
        ));
        if ($wpdb->last_error !== '' || strtoupper((string) $engine) !== 'INNODB') {
            throw new RuntimeException('Transactional key storage is required.');
        }
        if ($wpdb->query('START TRANSACTION') === false) {
            throw new RuntimeException('API key transaction failed.');
        }
        $transaction = true;

        foreach ($keys as $expected) {
            $rows = $read($expected, true);
            if (count($rows) === 0) {
                if ($wpdb->insert($table, $expected, array('%d', '%s', '%s', '%s', '%s', '%s')) !== 1) {
                    throw new RuntimeException('API key insertion failed.');
                }
            } elseif (!$matches($rows, $expected)) {
                throw new RuntimeException('Existing API credentials conflict.');
            }
        }
        if ($wpdb->query('COMMIT') === false) {
            throw new RuntimeException('API key commit failed.');
        }
        $transaction = false;
        foreach ($keys as $expected) {
            if (!$matches($read($expected), $expected)) {
                throw new RuntimeException('Saved API keys could not be verified.');
            }
        }
    } catch (Throwable $error) {
        $failed = true;
        if ($transaction) {
            try {
                $wpdb->query('ROLLBACK');
            } catch (Throwable $ignored) {
                // The operation still fails; no success is reported.
            }
        }
    } finally {
        if ($locked) {
            try {
                if ((string) $wpdb->get_var(
                    $wpdb->prepare('SELECT RELEASE_LOCK(%s)', $lock_name)
                ) !== '1' || $wpdb->last_error !== '') {
                    $failed = true;
                }
            } catch (Throwable $ignored) {
                $failed = true;
            }
        }
        $wpdb->suppress_errors($previous);
    }

    if ($failed) {
        WP_CLI::error('WooCommerce API key setup could not be verified. Existing keys were not replaced.');
    }
    WP_CLI::success('WooCommerce API keys verified.');
})();
