# Standard library
import base64
import io
import logging
import time
import uuid
import warnings

# Third-party imports
import cv2
import face_recognition
import numpy as np
from PIL import Image, UnidentifiedImageError

# Django imports
from django.conf import settings
from django.contrib.auth.models import User
from django.core.cache import cache, caches

# Local imports
from .models import UserImages

logger = logging.getLogger(__name__)

# Maximum distance between two encodings of the same person
FACE_MATCH_TOLERANCE = 0.5
# Minimum gap to the second closest user, so ambiguous matches are rejected
FACE_MATCH_MARGIN = 0.07
# Distance under which a new face counts as an already registered person
FACE_DUPLICATE_THRESHOLD = 0.4
FACE_ENCODING_LENGTH = 128

# Largest accepted image side and formats (the camera sends 640x480 JPEG)
MAX_IMAGE_SIDE = 1280
ALLOWED_IMAGE_FORMATS = {"JPEG", "PNG"}

# In-memory face index per worker, invalidated through a shared cache version
_FACE_INDEX_VERSION_KEY = "accounts.face_index.version"
_local_index = {"version": None, "ids": [], "matrix": np.empty((0, FACE_ENCODING_LENGTH))}


# Invalid or unreadable face image
class FaceImageError(ValueError):
    pass


# The image contains no face
class NoFaceDetected(FaceImageError):
    pass


# The image contains more than one face
class MultipleFacesDetected(FaceImageError):
    pass


def serialize_face_encoding(encoding):
    # Serialize a face encoding as base64 float32 bytes
    return base64.b64encode(np.asarray(encoding, dtype=np.float32).tobytes())


def deserialize_face_encoding(encoded):
    # Read a stored face encoding (ValueError if it is invalid)
    try:
        arr = np.frombuffer(base64.b64decode(encoded), dtype=np.float32)
        if arr.shape[0] == FACE_ENCODING_LENGTH:
            return arr.astype(np.float64)
    except Exception:
        pass
    raise ValueError("Invalid stored face encoding")


def decode_face_image(data_url):
    # Decode a base64 data URL into its raw bytes and an RGB image
    if not data_url:
        raise FaceImageError("Missing face image")
    try:
        payload = data_url.split(",", 1)[1] if "," in data_url else data_url
        binary = base64.b64decode(payload)
    except Exception as exc:
        raise FaceImageError("Invalid base64 image data") from exc

    # Check format and size from the header, before decoding the pixels
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(binary)) as probe:
                image_format, (width, height) = probe.format, probe.size
    except Image.DecompressionBombError as exc:
        raise FaceImageError("Image too large") from exc
    except (UnidentifiedImageError, OSError) as exc:
        raise FaceImageError("Image could not be decoded") from exc
    if image_format not in ALLOWED_IMAGE_FORMATS:
        raise FaceImageError(f"Unsupported image format: {image_format}")
    if width > MAX_IMAGE_SIDE or height > MAX_IMAGE_SIDE:
        raise FaceImageError(f"Image too large: {width}x{height}")

    nparr = np.frombuffer(binary, np.uint8)
    bgr = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if bgr is None:
        raise FaceImageError("Image could not be decoded")
    # OpenCV decodes to BGR; face_recognition expects RGB
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    return binary, rgb


def encode_single_face(rgb_image):
    # Encoding of the only face in the image (raises if there are none or several)
    try:
        encodings = face_recognition.face_encodings(rgb_image)
    except Exception as exc:
        raise FaceImageError("Face image could not be processed") from exc
    if not encodings:
        raise NoFaceDetected("No face detected")
    if len(encodings) > 1:
        raise MultipleFacesDetected("More than one face detected")
    return encodings[0]


def _build_face_index():
    # Load every stored encoding into one matrix (one row per user)
    ids, vectors = [], []
    rows = UserImages.objects.exclude(face_encoding__isnull=True).values_list(
        "user_id", "face_encoding"
    )
    for user_id, raw in rows:
        if not raw:
            continue
        try:
            vectors.append(deserialize_face_encoding(raw))
            ids.append(user_id)
        except ValueError:
            logger.warning("Skipping corrupt face encoding for user_id=%s", user_id)
    matrix = np.vstack(vectors) if vectors else np.empty((0, FACE_ENCODING_LENGTH))
    return ids, matrix


def get_face_index():
    # Face index of this worker, rebuilt only when the shared version changes
    version = cache.get(_FACE_INDEX_VERSION_KEY)
    if version is None:
        version = uuid.uuid4().hex
        cache.set(_FACE_INDEX_VERSION_KEY, version, None)
    if _local_index["version"] != version:
        ids, matrix = _build_face_index()
        _local_index.update(version=version, ids=ids, matrix=matrix)
    return _local_index["ids"], _local_index["matrix"]


def invalidate_face_index():
    # Force every worker to rebuild its face index
    cache.set(_FACE_INDEX_VERSION_KEY, uuid.uuid4().hex, None)


def find_duplicate_face(encoding, exclude_user_id=None):
    # Id of another user with a near-identical face, or None
    ids, matrix = get_face_index()
    if matrix.shape[0] == 0:
        return None
    distances = np.linalg.norm(matrix - np.asarray(encoding, dtype=np.float64), axis=1)
    for i in np.argsort(distances):
        if distances[i] > FACE_DUPLICATE_THRESHOLD:
            return None
        if ids[i] != exclude_user_id:
            return ids[i]
    return None


def identify_user(probe_encoding):
    # Registered user matching the face encoding, or None
    ids, matrix = get_face_index()
    if matrix.shape[0] == 0:
        return None
    distances = np.linalg.norm(matrix - np.asarray(probe_encoding, dtype=np.float64), axis=1)
    best = int(np.argmin(distances))
    if distances[best] > FACE_MATCH_TOLERANCE:
        return None
    if len(distances) > 1:
        runner_up = np.partition(distances, 1)[1]
        if runner_up - distances[best] < FACE_MATCH_MARGIN:
            logger.info("Ambiguous face match (%.3f vs %.3f); rejected", distances[best], runner_up)
            return None

    try:
        return User.objects.get(pk=ids[best])
    except User.DoesNotExist:
        invalidate_face_index()
        return None


def client_ip(request):
    # Client IP from X-Forwarded-For, counting TRUSTED_PROXY_COUNT entries from the end
    # (the first entry is set by the client and can't be trusted)
    forwarded = [ip.strip() for ip in request.META.get("HTTP_X_FORWARDED_FOR", "").split(",") if ip.strip()]
    hops = settings.TRUSTED_PROXY_COUNT
    if hops and forwarded:
        return forwarded[-min(hops, len(forwarded))]
    return request.META.get("REMOTE_ADDR", "unknown")


def rate_limited(request, scope, *, limit, window):
    # True when the IP exceeds `limit` requests in the current `window` (seconds)
    store = caches["ratelimit"]
    # The window number in the key starts a fresh counter every window
    bucket = int(time.time() // window)
    key = f"rl:{scope}:{client_ip(request)}:{bucket}"
    try:
        store.add(key, 0, window)
        current = store.incr(key)
    except ValueError:
        # Key expired between add() and incr()
        store.set(key, 1, window)
        current = 1
    except Exception:
        # Never let the limiter take the site down
        logger.exception("Rate limiter backend error for scope=%s", scope)
        return False
    return current > limit
