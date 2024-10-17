document.addEventListener('DOMContentLoaded', () => {
    const darkModeToggle = document.getElementById('dark-mode-toggle');
    const body = document.body;

    // Check for saved theme preference or use device preference
    const savedTheme = localStorage.getItem('theme');
    const prefersDarkScheme = window.matchMedia('(prefers-color-scheme: dark)');

    if (savedTheme === 'dark' || (!savedTheme && prefersDarkScheme.matches)) {
        body.classList.add('dark-mode');
        darkModeToggle.checked = true;
    }

    // Toggle dark mode
    darkModeToggle.addEventListener('change', () => {
        if (darkModeToggle.checked) {
            body.classList.add('dark-mode');
            localStorage.setItem('theme', 'dark');
        } else {
            body.classList.remove('dark-mode');
            localStorage.setItem('theme', 'light');
        }
    });

    // Form submission (prevent default for demo)
    const loginForm = document.getElementById('login-form');
    loginForm.addEventListener('submit', (e) => {
        e.preventDefault();
        alert('Login-Funktion hier implementieren');
    });
    // Geschlecht-Dropdown-Funktionalität
document.addEventListener('DOMContentLoaded', function() {
    const geschlechtSelect = document.getElementById('geschlecht');
    if (geschlechtSelect) {
        geschlechtSelect.addEventListener('focus', function() {
            if (this.options[0].text === 'Geschlecht wählen') {
                this.options[0].text = 'Bitte wählen';
            }
        });

        geschlechtSelect.addEventListener('blur', function() {
            if (this.value === '') {
                this.options[0].text = 'Geschlecht wählen';
            }
        });
    }
});
});