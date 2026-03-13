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

$stmt = $pdo->prepare("DELETE FROM exceptions WHERE path = ? AND type = ?");
$success = $stmt->execute([$path, $type]);

echo json_encode(['success' => $success]);
?>