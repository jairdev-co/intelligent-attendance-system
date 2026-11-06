// Initialize DOM references and variables for facial recognition registration
const video = document.getElementById('video');
const canvas = document.getElementById('canvas');
const captureButton = document.getElementById('capture-button');
const registerForm = document.getElementById('register-form');
const message = document.getElementById('message');
let capturedImage = null;
let isSubmitting = false;

// Access the camera and start the video stream
setStatus(message, 'Iniciando cámara... Si el navegador lo solicita, permita el acceso.');
navigator.mediaDevices.getUserMedia({ video: true })
    .then(stream => {
        video.srcObject = stream;
        setCameraState('active');
        setStatus(message, 'Mire a la cámara y pulse Capturar rostro.');
    })
    .catch(error => {
        console.error('Error accessing camera:', error);
        setCameraState('error');
        setStatus(message, 'Cámara no accesible. Por favor verifique los permisos.', 'error');
    });

// Capture the face from the current video frame
captureButton.addEventListener('click', () => {
    if (!cameraReady(video)) {
        setStatus(message, "Por favor habilite la cámara.", 'error');
        return;
    }
    capturedImage = captureFrame(video, canvas);
    setCameraState('success');
    setStatus(message, "Rostro capturado correctamente.", 'success');
});

// Handle register form submission and send user data with face image
registerForm.onsubmit = async (e) => {
    e.preventDefault();

    if (isSubmitting) return;
    if (!capturedImage) {
        setStatus(message, "Por favor capture su rostro primero.", 'error');
        return;
    }

    const username = document.getElementById('username').value;
    const identification_number = document.getElementById('identification_number').value;
    const user_type = document.getElementById('user_type').value;

    if (!username || !identification_number || !user_type) {
        setStatus(message, "Por favor complete todos los campos obligatorios", 'error');
        return;
    }

    isSubmitting = true;
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
        setStatus(message, data.message || '¡Registro exitoso!', 'success');
        if (data.redirect) {
            setTimeout(() => {
                window.location.href = data.redirect;
            }, 1000);
        }
    } else {
        setStatus(message, data.message || 'El registro falló.', 'error');
        isSubmitting = false;
    }
};
