<?php
require_once 'config.php';

// Проверяем авторизацию по IP (упрощенно)
session_start();
if (!isset($_SESSION['admin_logged_in'])) {
    // Если нет сессии, проверяем по IP (для теста)
    $ip = $_SERVER['REMOTE_ADDR'];
    if ($ip !== 'ваш_ip') { // Замените на свой IP или уберите эту проверку
        echo json_encode(['success' => false, 'error' => 'Не авторизован']);
        exit;
    }
}

$result = $pdo->query("SELECT path, type FROM exceptions ORDER BY created_at DESC");
$exceptions = $result->fetchAll();

$formatted = ['files' => [], 'folders' => []];
foreach ($exceptions as $item) {
    if ($item['type'] === 'file') {
        $formatted['files'][] = $item['path'];
    } else {
        $formatted['folders'][] = $item['path'];
    }
}

echo json_encode(['success' => true, 'exceptions' => $formatted]);
?>