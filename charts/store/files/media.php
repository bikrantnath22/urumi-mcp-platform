<?php
/* Import the existing Secret's media credential without rotating other passwords. */
(static function () {
    global $wpdb;
    $previous_errors = $wpdb->suppress_errors(true);
    $locked = false;
    $failed = false;
    $lock_name = '';

    try {
        $login = getenv('WP_MEDIA_USER');
        $password = getenv('WP_MEDIA_APP_PASSWORD');
        $role_name = 'media_manager';
        $app_name = 'woo-mcp media';
        if (!is_string($login) || !preg_match('/\A[a-z][a-z0-9_]{0,59}\z/', $login)
            || !is_string($password) || !preg_match('/\A[A-Za-z0-9]{24}\z/', $password)
            || strcasecmp($login, (string) getenv('WP_ADMIN_USER')) === 0
            || !class_exists('WP_Application_Passwords')
            || !preg_match('/\A[A-Za-z0-9_]{1,64}\z/', $wpdb->usermeta)) {
            throw new RuntimeException('Invalid media setup configuration.');
        }
        $meta_key = WP_Application_Passwords::USERMETA_KEY_APPLICATION_PASSWORDS;
        $lock_name = 'urumi-media-' . substr(hash('sha256', DB_NAME . ':' . $wpdb->prefix), 0, 52);
        if ((string) $wpdb->get_var($wpdb->prepare('SELECT GET_LOCK(%s, 5)', $lock_name)) !== '1'
            || $wpdb->last_error !== '') {
            throw new RuntimeException('Media setup is busy.');
        }
        $locked = true;
        $caps = array(
            'read' => true, 'upload_files' => true,
            'edit_posts' => true, 'edit_others_posts' => true,
            'delete_posts' => true, 'delete_others_posts' => true,
        );
        $verify_role = static function ($allow_missing = false) use ($wpdb, $role_name, $caps) {
            $key = $wpdb->prefix . 'user_roles';
            wp_cache_delete('alloptions', 'options');
            wp_cache_delete($key, 'options');
            $wpdb->last_error = '';
            $roles = get_option($key);
            if ($wpdb->last_error !== '' || !is_array($roles) || !$roles) {
                throw new RuntimeException('Stored roles could not be verified.');
            }
            if (!array_key_exists($role_name, $roles) && $allow_missing) {
                return false;
            }
            $actual = $roles[$role_name]['capabilities'] ?? null;
            if (!is_array($actual)) {
                throw new RuntimeException('Media role could not be verified.');
            }
            $expected = $caps;
            ksort($actual);
            ksort($expected);
            if ($actual !== $expected) {
                throw new RuntimeException('Existing media role conflicts.');
            }
            return true;
        };
        if (!$verify_role(true)) {
            wp_roles()->for_site();
            if (!get_role($role_name)) {
                add_role($role_name, 'Media Manager', $caps);
            }
        }
        $verify_role();
        wp_roles()->for_site();

        $wpdb->last_error = '';
        $user = get_user_by('login', $login);
        if ($wpdb->last_error !== '') {
            throw new RuntimeException('Media account lookup failed.');
        }
        if (!$user) {
            $id = wp_insert_user(array(
                'user_login' => $login,
                'user_pass' => wp_generate_password(64, true, true),
                'user_email' => $login . '@store.invalid',
                'role' => $role_name,
            ));
            if (is_wp_error($id) || !is_int($id) || $id < 1) {
                throw new RuntimeException('Media account creation failed.');
            }
        } else {
            $id = (int) $user->ID;
        }
        $verify_user = static function () use ($wpdb, $id, $login, $role_name) {
            clean_user_cache($id);
            $wpdb->last_error = '';
            $user = get_user_by('id', $id);
            if ($wpdb->last_error !== '' || !$user
                || $user->user_login !== $login
                || strtolower($user->user_email) !== $login . '@store.invalid'
                || array_values($user->roles) !== array($role_name)
                || $user->caps !== array($role_name => true)
                || is_super_admin($id)
                || user_can($user, 'manage_options')) {
                throw new RuntimeException('Existing media account conflicts.');
            }
        };
        $verify_user();

        $read = static function () use ($wpdb, $id, $meta_key) {
            $rows = $wpdb->get_results($wpdb->prepare(
                "SELECT umeta_id, meta_value FROM `{$wpdb->usermeta}` WHERE user_id = %d AND meta_key = %s LIMIT 2",
                $id, $meta_key
            ), ARRAY_A);
            if ($wpdb->last_error !== '' || !is_array($rows) || count($rows) > 1) {
                throw new RuntimeException('Media credential storage is unavailable.');
            }
            if (!$rows) {
                return array(null, array());
            }
            $row = $rows[0];
            if (!isset($row['umeta_id'], $row['meta_value']) || !is_string($row['meta_value'])) {
                throw new RuntimeException('Invalid media credential record.');
            }
            $items = @unserialize($row['meta_value'], array('allowed_classes' => false));
            if (!is_array($items)) {
                throw new RuntimeException('Invalid application-password list.');
            }
            return array($row, $items);
        };
        $find = static function ($items) use ($app_name) {
            $found = null;
            $seen = array();
            foreach ($items as $item) {
                if (!is_array($item) || !isset($item['uuid'], $item['name'], $item['password'])
                    || !is_string($item['uuid']) || !wp_is_uuid($item['uuid'])
                    || !is_string($item['name']) || !is_string($item['password'])
                    || $item['password'] === '' || isset($seen[strtolower($item['uuid'])])) {
                    throw new RuntimeException('Invalid application-password record.');
                }
                $seen[strtolower($item['uuid'])] = true;
                if (strcasecmp($item['name'], $app_name) === 0) {
                    if ($found !== null || ($item['app_id'] ?? '') !== '') {
                        throw new RuntimeException('Media application identity conflicts.');
                    }
                    $found = $item;
                }
            }
            return $found;
        };
        $matches = static function ($item) use ($password) {
            if ($item === null) { return false; }
            return method_exists('WP_Application_Passwords', 'check_password')
                ? WP_Application_Passwords::check_password($password, $item['password'])
                : wp_check_password($password, $item['password']);
        };

        list($row, $items) = $read();
        $item = $find($items);
        if ($item !== null && !$matches($item)) {
            throw new RuntimeException('Stored media password conflicts with the Secret.');
        }
        if ($item === null) {
            $hash = method_exists('WP_Application_Passwords', 'hash_password')
                ? WP_Application_Passwords::hash_password($password)
                : wp_hash_password($password);
            $item = array(
                'uuid' => wp_generate_uuid4(), 'app_id' => '', 'name' => $app_name,
                'password' => $hash, 'created' => time(),
                'last_used' => null, 'last_ip' => null,
            );
            if (!$matches($item)) {
                throw new RuntimeException('Media password hashing could not be verified.');
            }
            $items[] = $item;
            $serialized = serialize($items);
            if ($row === null) {
                // A replay after a lost acknowledgement must not insert a second row.
                $saved = $wpdb->query($wpdb->prepare(
                    "INSERT INTO `{$wpdb->usermeta}` (user_id, meta_key, meta_value) SELECT %d, %s, %s FROM `{$wpdb->usermeta}` AS existing WHERE existing.user_id = %d AND existing.meta_key = %s HAVING COUNT(*) = 0",
                    $id, $meta_key, $serialized, $id, $meta_key
                ));
            } else {
                // Compare exact bytes, including an existing empty list.
                $saved = $wpdb->query($wpdb->prepare(
                    "UPDATE `{$wpdb->usermeta}` SET meta_value = %s WHERE umeta_id = %d AND user_id = %d AND meta_key = %s AND CAST(meta_value AS BINARY) = CAST(%s AS BINARY)",
                    $serialized, $row['umeta_id'], $id, $meta_key, $row['meta_value']
                ));
            }
            if ($saved !== 1 || $wpdb->last_error !== '') {
                throw new RuntimeException('Media credential changed or could not be saved.');
            }
            wp_cache_delete($id, 'user_meta');
        }
        $expected_uuid = $item['uuid'];
        $network = get_main_network_id();
        $option = WP_Application_Passwords::OPTION_KEY_IN_USE;
        if (!get_network_option($network, $option)) {
            update_network_option($network, $option, true);
        }
        if ($wpdb->last_error !== '' || !get_network_option($network, $option)) {
            throw new RuntimeException('Application-password activation failed.');
        }
        list($saved_row, $saved_items) = $read();
        $saved_item = $find($saved_items);
        if (!$matches($saved_item) || $saved_item['uuid'] !== $expected_uuid) {
            throw new RuntimeException('Saved media credential could not be verified.');
        }
        $verify_role();
        $verify_user();
    } catch (Throwable $error) {
        $failed = true;
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
        $wpdb->suppress_errors($previous_errors);
    }
    if ($failed) {
        WP_CLI::error('Media account setup could not be verified. Existing credentials were not replaced.');
    }
    WP_CLI::success('Media account verified.');
})();
