<?php
require_once 'config.php';

// Для лаунчера разрешаем всем
header("Access-Control-Allow-Origin: *");

$result = $pdo->query("SELECT path, type FROM exceptions");
$exceptions = $result->fetchAll();

$formatted = ['files' => [], 'folders' => []];
foreach ($exceptions as $item) {
    if ($item['type'] === 'file') {
        $formatted['files'][] = $item['path'];
    } else {
        $formatted['folders'][] = $item['path'];
    }
}

echo json_encode($formatted);
?>