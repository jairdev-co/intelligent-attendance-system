// Initialize DOM references and variables for facial recognition attendance
const video = document.getElementById('video');
const canvas = document.getElementById('canvas');
const message = document.getElementById('message');
const userInfo = document.getElementById('user-info');
const faceUrl = document.getElementById('face-url').value;
const entryButton = document.getElementById('entry-button');
const exitButton = document.getElementById('exit-button');

let capturedImage = null;
let faceDetectionInterval = null;
let recognizedUsername = null;
let lastDetectionAttempt = 0;
let isProcessing = false;
const DETECTION_INTERVAL = 3000;
// Time before an unused recognition is forgotten, so the next person can't use it
const RECOGNITION_TIMEOUT = 10000;
let recognitionTimeout = null;
// Pause after the server answers 429 (too many requests)
const RATE_LIMIT_PAUSE = 30000;
let rateLimitPause = null;

// Read the CSRF token from the cookie, since Django rotates it on every login
function getCsrfToken() {
    const match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    return match ? decodeURIComponent(match[1]) : document.getElementById('csrf-token').value;
}

// Function to update the message display and camera ring with different alert types
function updateMessage(text, type = 'info') {
    setCameraState(type === 'info' ? 'active' : type);
    message.innerText = text;
    message.classList.remove('alert-success', 'alert-danger', 'alert-info');
    switch (type) {
        case 'success':
            message.classList.add('alert-success');
            break;
        case 'error':
            message.classList.add('alert-danger');
            break;
        case 'info':
        default:
            message.classList.add('alert-info');
    }
    message.style.display = 'block';
}

// Access the camera and start face detection once it works
updateMessage('Iniciando cámara... Si el navegador lo solicita, permita el acceso.', 'info');
navigator.mediaDevices.getUserMedia({ video: true })
    .then(stream => {
        video.srcObject = stream;
        updateMessage('Cámara habilitada correctamente.', 'success');
        startFaceDetection();
    })
    .catch(error => {
        console.error('Error accessing camera:', error);
        updateMessage('Cámara no accesible. Por favor verifique los permisos.', 'error');
    });

// Start periodic face detection using the video stream
function startFaceDetection() {
    clearTimeout(rateLimitPause);
    rateLimitPause = null;
    updateMessage('Buscando rostros... Por favor mire a la cámara.', 'info');
    if (faceDetectionInterval) {
        clearInterval(faceDetectionInterval);
    }
    faceDetectionInterval = setInterval(() => {
        if (!recognizedUsername) {
            const now = Date.now();
            if (now - lastDetectionAttempt > DETECTION_INTERVAL) {
                lastDetectionAttempt = now;
                detectFace();
            }
        }
    }, 1000);
}

// Pause face detection after a 429 and resume it later
function pauseDetection() {
    if (rateLimitPause) return;
    if (faceDetectionInterval) {
        clearInterval(faceDetectionInterval);
        faceDetectionInterval = null;
    }
    updateMessage('Sistema en pausa, reintentando en unos segundos...', 'info');
    rateLimitPause = setTimeout(startFaceDetection, RATE_LIMIT_PAUSE);
}

// Capture an image from the video stream and process it for face detection
function detectFace() {
    if (isProcessing) return;
    if (!cameraReady(video)) {
        // Camera lost after start, don't send blank frames
        updateMessage('Cámara no disponible. Verifique la cámara del dispositivo.', 'error');
        return;
    }
    updateMessage('Procesando rostro...', 'info');
    setCameraState('scanning');
    capturedImage = captureFrame(video, canvas);
    sendImageForRecognition();
}

// Send the captured image to the server for face recognition
async function sendImageForRecognition() {
    if (isProcessing) return;
    if (!capturedImage) {
        updateMessage('No se capturó ninguna imagen.', 'error');
        return;
    }
    isProcessing = true;
    const formData = new FormData();
    formData.append("face_image", capturedImage);
    try {
        const response = await fetch(faceUrl, {
            method: 'POST',
            body: formData,
            headers: {
                'X-CSRFToken': getCsrfToken()
            }
        });
        if (response.status === 403) {
            // CSRF token no longer valid, reload the page
            updateMessage('Reiniciando el sistema...', 'info');
            setTimeout(() => window.location.reload(), 3000);
            return;
        }
        if (response.status === 429) {
            pauseDetection();
            return;
        }
        const data = await response.json();
        if (data.status === 'success') {
            recognizedUsername = data.username;
            userInfo.style.display = 'block';
            updateMessage(data.message || `¡Bienvenido ${recognizedUsername}!`, 'success');
            if (faceDetectionInterval) {
                clearInterval(faceDetectionInterval);
            }
            recognitionTimeout = setTimeout(resetRecognition, RECOGNITION_TIMEOUT);
        } else if (data.code === 'no_face') {
            // Nobody in front of the camera
            updateMessage('Buscando rostros... Por favor mire a la cámara.', 'info');
        } else {
            updateMessage(data.message || 'Rostro no reconocido. Por favor intente de nuevo.', 'error');
        }
    } catch (error) {
        console.error('Error in face recognition:', error);
        updateMessage('Error al procesar la imagen facial. Por favor intente de nuevo.', 'error');
    } finally {
        isProcessing = false;
    }
}

// Forget the recognized person and look for the next one
function resetRecognition() {
    clearTimeout(recognitionTimeout);
    recognizedUsername = null;
    capturedImage = null;
    userInfo.style.display = 'none';
    setButtonsDisabled(false);
    startFaceDetection();
}

// Enable or disable the attendance buttons
function setButtonsDisabled(disabled) {
    entryButton.disabled = disabled;
    exitButton.disabled = disabled;
}

// Record attendance (entry or exit) after successful face recognition
async function recordAttendance(type) {
    if (!recognizedUsername || isProcessing) {
        updateMessage('Por favor espere el reconocimiento facial primero.', 'error');
        return;
    }
    if (!cameraReady(video)) {
        updateMessage('Cámara no disponible. Verifique la cámara del dispositivo.', 'error');
        return;
    }
    clearTimeout(recognitionTimeout);
    setButtonsDisabled(true);
    isProcessing = true;
    const tipo = type === 'entry' ? 'Entrada' : 'Salida';
    updateMessage(`Registrando ${tipo}...`, 'info');
    setCameraState('scanning');
    // Take a new photo to confirm the person in front of the camera
    const formData = new FormData();
    formData.append("face_image", captureFrame(video, canvas));
    formData.append("attendance_type", type);
    formData.append("expected_username", recognizedUsername);
    try {
        const response = await fetch(faceUrl, {
            method: 'POST',
            body: formData,
            headers: {
                'X-CSRFToken': getCsrfToken()
            }
        });
        const data = await response.json();
        if (data.status === 'success') {
            updateMessage(data.message || '¡Asistencia registrada correctamente!', 'success');
            // Keep the buttons disabled until the reset to avoid a second record
            setTimeout(resetRecognition, 3000);
        } else if (data.code === 'person_changed') {
            updateMessage(data.message, 'error');
            setTimeout(resetRecognition, 2000);
        } else {
            setButtonsDisabled(false);
            updateMessage(data.message || 'No se pudo registrar la asistencia.', 'error');
            recognitionTimeout = setTimeout(resetRecognition, RECOGNITION_TIMEOUT);
        }
    } catch (error) {
        console.error('Error recording attendance:', error);
        setButtonsDisabled(false);
        updateMessage('Ocurrió un error al registrar la asistencia.', 'error');
        recognitionTimeout = setTimeout(resetRecognition, RECOGNITION_TIMEOUT);
    } finally {
        isProcessing = false;
    }
}

// Attach event listeners
entryButton.addEventListener('click', () => recordAttendance('entry'));
exitButton.addEventListener('click', () => recordAttendance('exit'));
