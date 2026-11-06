// Login page: show or hide the password
document.querySelectorAll('.password-toggle').forEach((toggle) => {
    const input = toggle.parentElement.querySelector('input');
    if (!input) return;

    toggle.addEventListener('click', () => {
        const show = input.type === 'password';
        input.type = show ? 'text' : 'password';

        const label = show ? 'Ocultar contraseña' : 'Mostrar contraseña';
        toggle.classList.toggle('is-visible', show);
        toggle.setAttribute('aria-pressed', String(show));
        toggle.setAttribute('aria-label', label);
        toggle.title = label;

        // Keep the cursor in the field at the end of the text
        input.focus();
        const end = input.value.length;
        input.setSelectionRange(end, end);
    });
});
