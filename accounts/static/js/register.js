// Initialize DOM references and variables for facial recognition registration
const video = document.getElementById('video');
const canvas = document.getElementById('canvas');
const registerForm = document.getElementById('register-form');
const message = document.getElementById('message');
let isSubmitting = false;

// Access the camera and start the video stream
setStatus(message, 'Iniciando cámara... Si el navegador lo solicita, permita el acceso.');
navigator.mediaDevices.getUserMedia({ video: true })
    .then(stream => {
        video.srcObject = stream;
        setCameraState('active');
        setStatus(message, 'Complete los datos, mire a la cámara y pulse Registrar.');
    })
    .catch(error => {
        console.error('Error accessing camera:', error);
        setCameraState('error');
        setStatus(message, 'Cámara no accesible. Por favor verifique los permisos.', 'error');
    });

// Capture the face and send the user data in one step
registerForm.onsubmit = async (e) => {
    e.preventDefault();

    if (isSubmitting) return;

    const username = document.getElementById('username').value;
    const identification_number = document.getElementById('identification_number').value;
    const user_type = document.getElementById('user_type').value;

    if (!username || !identification_number || !user_type) {
        setStatus(message, "Por favor complete todos los campos obligatorios", 'error');
        return;
    }

    if (!cameraReady(video)) {
        setStatus(message, "Por favor habilite la cámara.", 'error');
        return;
    }

    isSubmitting = true;
    const capturedImage = captureFrame(video, canvas);
    setCameraState('scanning');
    setStatus(message, 'Registrando...');

    const formData = new FormData(registerForm);
    formData.append('face_image', capturedImage);

    let data;
    try {
        const response = await fetch(registerForm.action, {
            method: "POST",
            body: formData,
        });
        // Expired session: the server answers with the login page
        if (response.redirected) {
            setStatus(message, 'Su sesión expiró. Inicie sesión de nuevo.', 'error');
            setTimeout(() => { window.location.href = response.url; }, 2000);
            return;
        }
        data = await response.json();
    } catch (error) {
        // Network failure or non-JSON error page
        console.error('Request failed:', error);
        data = { status: 'error', message: 'Error de conexión con el servidor. Intente de nuevo.' };
    }
    if (data.status === 'success') {
        setCameraState('success');
        setStatus(message, data.message || '¡Registro exitoso!', 'success');
        if (data.redirect) {
            setTimeout(() => {
                window.location.href = data.redirect;
            }, 1000);
        }
    } else {
        // Show the error; the next click takes a fresh photo
        setCameraState('error');
        setStatus(message, data.message || 'El registro falló.', 'error');
        isSubmitting = false;
    }
};
