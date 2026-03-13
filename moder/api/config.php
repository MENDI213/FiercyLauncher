<?php
// ========== НАСТРОЙКИ БАЗЫ ДАННЫХ ==========
define('DB_HOST', 'sql.freedb.tech');
define('DB_USER', 'freedb_MENDI');
define('DB_PASS', 'bVySD2zG5%UsnC@');
define('DB_NAME', 'freedb_Fiercy');
define('DB_PORT', 3306);

// ========== НАСТРОЙКИ БЕЗОПАСНОСТИ ==========
define('ADMIN_PASSWORD', 'admin123'); // Пароль для входа
define('MAX_LOGIN_ATTEMPTS', 3);
define('LOCK_TIME_MINUTES', 2);

// Разрешаем доступ с GitHub
header("Access-Control-Allow-Origin: https://mendi213.github.io");
header("Access-Control-Allow-Methods: POST, GET, OPTIONS");
header("Access-Control-Allow-Headers: Content-Type");
header("Content-Type: application/json");

if ($_SERVER['REQUEST_METHOD'] === 'OPTIONS') {
    exit(0);
}

// Подключение к базе
try {
    $pdo = new PDO(
        "mysql:host=" . DB_HOST . ";port=" . DB_PORT . ";dbname=" . DB_NAME . ";charset=utf8mb4",
        DB_USER,
        DB_PASS
    );
    $pdo->setAttribute(PDO::ATTR_ERRMODE, PDO::ERRMODE_EXCEPTION);
} catch (PDOException $e) {
    die(json_encode(['success' => false, 'error' => 'Ошибка подключения к БД']));
}
?>