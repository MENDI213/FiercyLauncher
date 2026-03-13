<?php
require_once 'config.php';

session_start();
if (!isset($_SESSION['admin_logged_in'])) {
    echo json_encode(['success' => false, 'error' => 'Не авторизован']);
    exit;
}

$data = json_decode(file_get_contents('php://input'), true);
$path = $data['path'] ?? '';
$type = $data['type'] ?? '';

if (empty($path) || !in_array($type, ['file', 'folder'])) {
    echo json_encode(['success' => false, 'error' => 'Неверные данные']);
    exit;
}

// Проверяем, нет ли уже такого
$stmt = $pdo->prepare("SELECT id FROM exceptions WHERE path = ? AND type = ?");
$stmt->execute([$path, $type]);
if ($stmt->fetch()) {
    echo json_encode(['success' => false, 'error' => 'Такая запись уже есть']);
    exit;
}

$stmt = $pdo->prepare("INSERT INTO exceptions (path, type) VALUES (?, ?)");
$success = $stmt->execute([$path, $type]);

echo json_encode(['success' => $success]);
?>