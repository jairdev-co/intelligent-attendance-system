// Camera helpers shared by the attendance, register and update pages

// True once the camera is actually delivering frames
function cameraReady(video) {
    return !!video.srcObject && video.readyState >= 2 && video.videoWidth > 0;
}

// Draw the current video frame on the canvas and return it as a JPEG data URL
function captureFrame(video, canvas) {
    const context = canvas.getContext('2d');

    // Fit the frame inside the canvas keeping its aspect ratio
    const canvasAspectRatio = canvas.width / canvas.height;
    const videoAspectRatio = video.videoWidth / video.videoHeight;

    let drawX = 0, drawY = 0, drawWidth = canvas.width, drawHeight = canvas.height;

    if (videoAspectRatio > canvasAspectRatio) {
        drawHeight = canvas.width / videoAspectRatio;
        drawY = (canvas.height - drawHeight) / 2;
    } else {
        drawWidth = canvas.height * videoAspectRatio;
        drawX = (canvas.width - drawWidth) / 2;
    }

    context.clearRect(0, 0, canvas.width, canvas.height);
    context.drawImage(video, drawX, drawY, drawWidth, drawHeight);

    return canvas.toDataURL('image/jpeg', 0.9);
}

// Color the camera ring: 'active', 'scanning', 'success', 'error' or '' to reset
function setCameraState(state) {
    const ring = document.getElementById('camera-container');
    ring.classList.remove('active', 'scanning', 'success', 'error');
    if (state) ring.classList.add(state);
}

// Show a status line under the camera; kind is '', 'success' or 'error'
function setStatus(element, text, kind = '') {
    element.innerText = text;
    element.className = ('status ' + kind).trim();
}
