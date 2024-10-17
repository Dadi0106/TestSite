<?php
// Hier würde normalerweise die Registrierung in der Datenbank stattfinden
// Dies ist nur ein Beispiel und sollte nicht in der Produktion verwendet werden

if ($_SERVER["REQUEST_METHOD"] == "POST") {
    $vorname = $_POST["vorname"];
    $nachname = $_POST["nachname"];
    $email = $_POST["email"];
    $password = $_POST["password"];
    $geschlecht = $_POST["geschlecht"];
    $adresse = $_POST["adresse"];

    // Hier würden die Daten in der Datenbank gespeichert werden
    // Für dieses Beispiel geben wir einfach eine Erfolgsmeldung aus

    echo "Registrierung erfolgreich! Bitte <a href='index.html'>hier klicken</a> um sich anzumelden.";
}
?>