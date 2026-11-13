// Initialize DOM references and variables for facial recognition update
const video = document.getElementById('video');
const canvas = document.getElementById('canvas');
const updateFace = document.getElementById('update-face');
const updateForm = document.getElementById('update-form');
const message = document.getElementById('message');
let isSubmitting = false;

// Access the camera and start the video stream
setStatus(message, 'Iniciando cámara... Si el navegador lo solicita, permita el acceso.');
navigator.mediaDevices.getUserMedia({ video: true })
    .then(stream => {
        video.srcObject = stream;
        setCameraState('active');
        setStatus(message, 'Edite los datos y pulse Guardar. Para cambiar el rostro, marque la casilla y mire a la cámara.');
    })
    .catch(error => {
        console.error('Error accessing camera:', error);
        setCameraState('error');
        setStatus(message, 'Cámara no accesible. Puede guardar los datos sin cambiar el rostro.', 'error');
    });

// Save the user data, capturing a new face photo only when the box is checked
updateForm.onsubmit = async (e) => {
    e.preventDefault();

    if (isSubmitting) return;

    const username = document.getElementById('username').value;
    const identification_number = document.getElementById('identification_number').value;
    const user_type = document.getElementById('user_type').value;

    if (!username || !identification_number || !user_type) {
        setStatus(message, "Por favor complete todos los campos obligatorios", 'error');
        return;
    }

    if (updateFace.checked && !cameraReady(video)) {
        setStatus(message, "Por favor habilite la cámara o desmarque Actualizar también el rostro.", 'error');
        return;
    }

    isSubmitting = true;
    const formData = new FormData(updateForm);
    if (updateFace.checked) {
        formData.append('face_image', captureFrame(video, canvas));
        setCameraState('scanning');
    }
    setStatus(message, 'Guardando...');

    let data;
    try {
        const response = await fetch(updateForm.action, {
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
        if (updateFace.checked) setCameraState('success');
        setStatus(message, data.message || '¡Actualización exitosa!', 'success');
        if (data.redirect) {
            setTimeout(() => {
                window.location.href = data.redirect;
            }, 1000);
        }
    } else {
        // Show the error; the next click takes a fresh photo
        if (updateFace.checked) setCameraState('error');
        setStatus(message, data.message || 'La actualización falló.', 'error');
        isSubmitting = false;
    }
};
