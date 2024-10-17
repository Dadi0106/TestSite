<?php
session_start();

if (!isset($_SESSION["user"])) {
    header("Location: index.html");
    exit();
}
?>

<!DOCTYPE html>
<html lang="de">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Willkommen</title>
    <link href="https://fonts.googleapis.com/css2?family=Roboto:wght@400;500&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="style.css">
</head>
<body>
    <div class="container">
        <h1>Willkommen, <?php echo htmlspecialchars($_SESSION["user"]); ?>!</h1>
        <p>Sie sind erfolgreich eingeloggt.</p>
        <a href="logout.php">Ausloggen</a>
    </div>
</body>
</html>