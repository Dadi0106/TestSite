<?php
session_start();

// Hier würde normalerweise die Überprüfung der Anmeldedaten stattfinden
// Dies ist nur ein Beispiel und sollte nicht in der Produktion verwendet werden

if ($_SERVER["REQUEST_METHOD"] == "POST") {
    $email = $_POST["email"];
    $password = $_POST["password"];

    // Hier würde die Überprüfung in der Datenbank stattfinden
    // Für dieses Beispiel leiten wir einfach weiter

    $_SESSION["user"] = $email;
    header("Location: welcome.php");
    exit();
}
?>