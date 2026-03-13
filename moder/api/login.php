<?php
require_once 'config.php';

$ip = $_SERVER['REMOTE_ADDR'];
$data = json_decode(file_get_contents('php://input'), true);
$password = $data['password'] ?? '';

// Проверяем количество попыток за последние 2 минуты
$stmt = $pdo->prepare("SELECT COUNT(*) as attempts FROM login_attempts WHERE ip_address = ? AND attempt_time > DATE_SUB(NOW(), INTERVAL ? MINUTE)");
$stmt->execute([$ip, LOCK_TIME_MINUTES]);
$attempts = $stmt->fetch()['attempts'];

if ($attempts >= MAX_LOGIN_ATTEMPTS) {
    echo json_encode(['success' => false, 'error' => 'Слишком много попыток', 'locked' => true]);
    exit;
}

if ($password === ADMIN_PASSWORD) {
    // Успешный вход - очищаем попытки
    $stmt = $pdo->prepare("DELETE FROM login_attempts WHERE ip_address = ?");
    $stmt->execute([$ip]);
    echo json_encode(['success' => true]);
} else {
    // Неудачная попытка - записываем
    $stmt = $pdo->prepare("INSERT INTO login_attempts (ip_address) VALUES (?)");
    $stmt->execute([$ip]);
    
    $attemptsLeft = MAX_LOGIN_ATTEMPTS - ($attempts + 1);
    echo json_encode([
        'success' => false, 
        'error' => 'Неверный пароль',
        'attemptsLeft' => $attemptsLeft
    ]);
}
?>