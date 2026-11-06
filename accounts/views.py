# Standard library
import csv
import hashlib
import logging
from datetime import timedelta

# Django imports
from django.contrib.auth import login as auth_login, logout
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.http import Http404, HttpResponse, QueryDict, HttpResponseNotModified, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import dateformat, timezone
from django.utils.dateparse import parse_date
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

# Local imports
from .models import Attendance, UserImages
from .utils import (
    FaceImageError,
    MultipleFacesDetected,
    NoFaceDetected,
    client_ip,
    decode_face_image,
    encode_single_face,
    find_duplicate_face,
    identify_user,
    rate_limited,
    serialize_face_encoding,
)

logger = logging.getLogger(__name__)

# Seconds before a user can record the same attendance type again
ATTENDANCE_COOLDOWN_SECONDS = 60

# Rate limits as (requests, seconds) per IP
FACE_RATE_LIMIT = (60, 60)
LOGIN_RATE_LIMIT = (10, 300)

# Attendance records per dashboard page: allowed choices and default
ATTENDANCE_PAGE_SIZES = (25, 50, 100)
ATTENDANCE_PAGE_SIZE = 25

# Dashboard query parameters kept when returning after deleting a record
DASHBOARD_PARAMS = ('user_type', 'type', 'date_from', 'date_to', 'per_page', 'page')

# Version of the terms accepted at registration (update it when terms.html changes)
TERMS_VERSION = '2.0-2026-10'

# Spanish labels and formats for the dashboard and the CSV export
USER_TYPE_LABELS = {UserImages.ADMINISTRATIVE: 'Administrativo', UserImages.STUDENT: 'Estudiante'}
ATTENDANCE_TYPE_LABELS = {Attendance.ENTRY: 'Entrada', Attendance.EXIT: 'Salida'}
DATE_FORMAT = 'd/m/Y'
TIME_FORMAT = 'g:i:s A'

# Maximum field lengths (PostgreSQL rejects longer values)
USERNAME_MAX_LENGTH = User._meta.get_field('username').max_length
IDENTIFICATION_MAX_LENGTH = UserImages._meta.get_field('identification_number').max_length


def _validate_profile_fields(username, identification_number):
    # Validate the length and format of the profile fields
    if len(username) > USERNAME_MAX_LENGTH:
        return f'¡El nombre de usuario no puede superar {USERNAME_MAX_LENGTH} caracteres!'
    if len(identification_number) > IDENTIFICATION_MAX_LENGTH:
        return f'¡El número de identificación no puede superar {IDENTIFICATION_MAX_LENGTH} caracteres!'
    # Only ASCII digits (isdigit() alone also accepts characters like '²')
    if not (identification_number.isascii() and identification_number.isdigit()):
        return '¡El número de identificación solo puede contener dígitos!'
    return None


def _duplicate_face_message(encoding, exclude_user_id=None):
    # Check if the face is already registered to another user
    duplicate_id = find_duplicate_face(encoding, exclude_user_id=exclude_user_id)
    if duplicate_id is None:
        return None
    other = User.objects.filter(pk=duplicate_id).values_list('username', flat=True).first()
    return f'¡Este rostro ya está registrado como "{other}"!'


def admin_required(view_func):
    # Restrict a view to staff users (anonymous users go to the login)
    def check(user):
        if not user.is_authenticated:
            return False
        if not user.is_staff:
            raise PermissionDenied
        return True

    return user_passes_test(check)(view_func)


def spanish_auth_form(request=None, data=None):
    # Django login form with the labels in Spanish
    form = AuthenticationForm(request, data=data)
    form.fields['username'].label = 'Nombre de usuario'
    form.fields['password'].label = 'Contraseña'
    return form


# Create your views here.
@login_required
@admin_required
def register(request):
    # User registration with facial recognition
    if request.method == 'POST':
        # Get the data sent from the form
        username = (request.POST.get('username') or '').strip()
        face_image_data = request.POST.get('face_image')
        identification_number = (request.POST.get('identification_number') or '').strip()
        user_type = request.POST.get('user_type')
        consent = request.POST.get('consent')

        # Validate that all required fields are provided
        if not username or not face_image_data or not identification_number or not user_type:
            return JsonResponse({'status': 'error', 'message': '¡Todos los campos son obligatorios!'})

        # Validate the field lengths and the user type
        length_error = _validate_profile_fields(username, identification_number)
        if length_error:
            return JsonResponse({'status': 'error', 'message': length_error})

        if user_type not in dict(UserImages.USER_TYPE_CHOICES):
            return JsonResponse({'status': 'error', 'message': '¡Tipo de usuario inválido!'})

        # Biometric data requires the holder's authorization (Ley 1581 de 2012)
        if consent not in ('si', 'no'):
            return JsonResponse({'status': 'error', 'message': '¡Indique si la persona acepta los términos y condiciones!'})
        if consent == 'no':
            return JsonResponse({'status': 'error', 'message': 'Sin aceptar los términos y condiciones no se puede registrar el rostro. La persona puede marcar su asistencia por otro medio dispuesto por la Universidad.'})

        # Check if identification number or username already exists in the system
        if UserImages.objects.filter(identification_number=identification_number).exists():
            return JsonResponse({'status': 'error', 'message': '¡El número de identificación ya está registrado!'})

        if User.objects.filter(username__iexact=username).exists():
            return JsonResponse({'status': 'error', 'message': '¡El nombre de usuario ya existe!'})

        # Process and validate the facial image data
        try:
            face_image_binary, rgb_image = decode_face_image(face_image_data)
            face_encoding = encode_single_face(rgb_image)
        except NoFaceDetected:
            return JsonResponse({'status': 'error', 'message': '¡No se detectó ningún rostro!'})
        except MultipleFacesDetected:
            return JsonResponse({'status': 'error', 'message': '¡Se detectó más de un rostro en la imagen!'})
        except FaceImageError:
            return JsonResponse({'status': 'error', 'message': '¡Datos de imagen facial inválidos!'})

        # Reject a face already registered to another user
        duplicate = _duplicate_face_message(face_encoding)
        if duplicate:
            return JsonResponse({'status': 'error', 'message': duplicate})

        face_encoding_serialized = serialize_face_encoding(face_encoding)

        # Create the user and its face profile in a single transaction
        try:
            with transaction.atomic():
                user = User(username=username)
                user.set_unusable_password()
                user.save()

                UserImages.objects.create(
                    user=user,
                    face_image=face_image_binary,
                    identification_number=identification_number,
                    user_type=user_type,
                    face_encoding=face_encoding_serialized,
                    consent_accepted_at=timezone.now(),
                    consent_version=TERMS_VERSION,
                    consent_recorded_by=request.user,
                )
        except IntegrityError:
            return JsonResponse({'status': 'error', 'message': '¡El nombre de usuario o el número de identificación ya está registrado!'})

        return JsonResponse({'status': 'success', 'message': '¡Usuario creado correctamente!', 'redirect': reverse('users')})

    return render(request, 'register.html')


def terms(request):
    # Public page with the terms and data processing authorization
    return render(request, 'terms.html', {'terms_version': TERMS_VERSION})


def face(request):
    # Attendance screen (home page)
    if request.method == 'POST':
        return _face_post(request)
    return render(request, 'face.html')


def _face_post(request):
    # Identify the face and, if requested, record the attendance
    if rate_limited(request, 'face', limit=FACE_RATE_LIMIT[0], window=FACE_RATE_LIMIT[1]):
        return JsonResponse(
            {'status': 'error', 'message': '¡Demasiadas solicitudes! Espere un momento.'},
            status=429,
        )

    # Get the data sent from the attendance screen
    face_image_data = request.POST.get('face_image')
    attendance_type = request.POST.get('attendance_type')

    # Validate that a face image was provided
    if not face_image_data:
        return JsonResponse({'status': 'error', 'message': '¡La imagen facial es obligatoria!'})

    # Decode the uploaded image and compute its face encoding
    try:
        _, rgb_image = decode_face_image(face_image_data)
        uploaded_face_encoding = encode_single_face(rgb_image)
    except NoFaceDetected:
        # Normal while nobody is in front of the camera
        return JsonResponse({'status': 'error', 'code': 'no_face', 'message': '¡Rostro no detectado!'})
    except MultipleFacesDetected:
        return JsonResponse({'status': 'error', 'message': '¡Se detectó más de un rostro! Acérquese solo usted.'})
    except FaceImageError:
        return JsonResponse({'status': 'error', 'message': '¡Datos de imagen facial inválidos!'})

    # Match the face against the registered users
    identified_user = identify_user(uploaded_face_encoding)
    if not identified_user:
        return JsonResponse({'status': 'error', 'message': '¡No se encontró un usuario que coincida!'})

    # Register attendance if requested
    if attendance_type in (Attendance.ENTRY, Attendance.EXIT):
        # Check that the same person recognized before is still in front of the camera
        expected_username = request.POST.get('expected_username')
        if expected_username and expected_username != identified_user.username:
            return JsonResponse({
                'status': 'error',
                'code': 'person_changed',
                'message': '¡La persona frente a la cámara cambió! Intente de nuevo.',
            })

        # Prevent duplicate attendance within the cooldown window
        recent = Attendance.objects.filter(
            user=identified_user,
            attendance_type=attendance_type,
            timestamp__gte=timezone.now() - timedelta(seconds=ATTENDANCE_COOLDOWN_SECONDS)
        ).exists()
        if recent:
            return JsonResponse({'status': 'error', 'message': '¡La asistencia ya fue registrada recientemente para este usuario!'})

        try:
            Attendance.objects.create(user=identified_user, attendance_type=attendance_type)
        except Exception:
            logger.exception("Failed to record attendance for user_id=%s", identified_user.pk)
            return JsonResponse({'status': 'error', 'message': 'No se pudo registrar la asistencia. Intente de nuevo.'})

        return JsonResponse({
            'status': 'success',
            'message': '¡Asistencia registrada correctamente!',
            'username': identified_user.username
        })

    # If no attendance registration, just return a successful identification
    return JsonResponse({
        'status': 'success',
        'message': f'¡Bienvenido {identified_user.username}!',
        'username': identified_user.username
    })


def _safe_next(request):
    # Page to return to after logging in (?next=), only if it is on this site
    next_url = request.POST.get('next') or request.GET.get('next') or ''
    if url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()},
                                       require_https=request.is_secure()):
        return next_url
    return ''


def login(request):
    # Admin login with username and password
    next_url = _safe_next(request)

    # Display the login form to the user
    if request.method == 'GET':
        return render(request, 'login.html', {
            'form': spanish_auth_form(request),
            'next': next_url,
            # Protected pages send here with ?next= when there is no active session
            'expired': bool(next_url),
        })

    # Limit failed attempts per IP
    if rate_limited(request, 'login', limit=LOGIN_RATE_LIMIT[0], window=LOGIN_RATE_LIMIT[1]):
        return render(request, 'login.html', {
            'form': spanish_auth_form(request),
            'next': next_url,
            'error': 'Demasiados intentos de inicio de sesión. Intente de nuevo en unos minutos.'
        }, status=429)

    # Authenticate the user
    form = spanish_auth_form(request, data=request.POST)
    if form.is_valid():
        auth_login(request, form.get_user())
        if request.user.is_staff:
            return redirect(next_url or 'main')
        return redirect('face')

    # Log the failed login with the proxy header to verify DJANGO_TRUSTED_PROXY_COUNT
    logger.warning(
        "Failed login for %r from %s (X-Forwarded-For=%r)",
        request.POST.get('username', ''), client_ip(request), request.META.get('HTTP_X_FORWARDED_FOR', ''),
    )
    return render(request, 'login.html', {
        'form': form,
        'next': next_url,
        'error': 'El nombre de usuario o la contraseña es incorrecto.'
    })


def _safe_parse_date(value):
    # Parse a YYYY-MM-DD date, or None if it is invalid
    try:
        return parse_date(value or '')
    except ValueError:
        return None


def _filtered_attendance(request):
    # Attendance records with the dashboard filters applied
    qs = Attendance.objects.select_related('user', 'user__face_profile')

    user_type = request.GET.get('user_type', 'all')
    if user_type in dict(UserImages.USER_TYPE_CHOICES):
        qs = qs.filter(user__face_profile__user_type=user_type)

    attendance_type = request.GET.get('type', 'all')
    if attendance_type in (Attendance.ENTRY, Attendance.EXIT):
        qs = qs.filter(attendance_type=attendance_type)

    date_from = _safe_parse_date(request.GET.get('date_from'))
    if date_from:
        qs = qs.filter(timestamp__date__gte=date_from)
    date_to = _safe_parse_date(request.GET.get('date_to'))
    if date_to:
        qs = qs.filter(timestamp__date__lte=date_to)

    return qs.order_by('-timestamp')


def _page_size(value):
    # Records per page from the query string, limited to the allowed choices
    try:
        size = int(value)
    except (TypeError, ValueError):
        return ATTENDANCE_PAGE_SIZE
    return size if size in ATTENDANCE_PAGE_SIZES else ATTENDANCE_PAGE_SIZE


def _dashboard_url(querystring):
    # Dashboard URL rebuilt from a querystring, keeping only the known parameters.
    # The path is always the dashboard, so the value can't redirect elsewhere.
    received = QueryDict(querystring)
    params = QueryDict(mutable=True)
    for key in DASHBOARD_PARAMS:
        if received.get(key):
            params[key] = received.get(key)
    url = reverse('main')
    return f'{url}?{params.urlencode()}' if params else url


def _attendance_row(attendance):
    # Build the dashboard and CSV row for an attendance record
    face_profile = getattr(attendance.user, 'face_profile', None)
    return {
        'id': attendance.id,
        'number_identification': face_profile.identification_number if face_profile else 'N/A',
        'fullname': attendance.user.username,
        'type_register': attendance.attendance_type,
        'timestamp': timezone.localtime(attendance.timestamp),
        'user_type': face_profile.user_type if face_profile else UserImages.STUDENT,
    }


# File-like object that lets csv.writer stream each row
class _Echo:
    def write(self, value):
        return value


@login_required
@admin_required
def main(request):
    # Attendance dashboard with filters, pagination and CSV export
    attendance_qs = _filtered_attendance(request)

    # Export the whole filtered set as CSV, not just the current page
    if request.GET.get('export') == 'csv':
        writer = csv.writer(_Echo())
        header = ['Número de identificación', 'Nombre completo', 'Tipo de usuario', 'Tipo', 'Fecha', 'Hora']

        def rows():
            # UTF-8 BOM so Excel shows accents correctly
            yield '\ufeff'
            yield writer.writerow(header)
            for attendance in attendance_qs.iterator():
                row = _attendance_row(attendance)
                yield writer.writerow([
                    row['number_identification'],
                    row['fullname'],
                    USER_TYPE_LABELS.get(row['user_type'], row['user_type']),
                    ATTENDANCE_TYPE_LABELS.get(row['type_register'], row['type_register']),
                    dateformat.format(row['timestamp'], DATE_FORMAT),
                    dateformat.format(row['timestamp'], TIME_FORMAT),
                ])

        response = StreamingHttpResponse(rows(), content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = 'attachment; filename="registros_asistencia.csv"'
        return response

    # Paginate the records (only allowed page sizes, anything else uses the default)
    per_page = _page_size(request.GET.get('per_page'))
    paginator = Paginator(attendance_qs, per_page)
    page = paginator.get_page(request.GET.get('page'))

    querystring = request.GET.copy()
    querystring.pop('page', None)

    # Context preparation for rendering
    context = {
        'page_obj': page,
        'attendance_list': [_attendance_row(a) for a in page.object_list],
        'filters': {
            'user_type': request.GET.get('user_type', 'all'),
            'date_from': request.GET.get('date_from', ''),
            'date_to': request.GET.get('date_to', ''),
            'per_page': per_page,
        },
        'page_sizes': ATTENDANCE_PAGE_SIZES,
        'base_querystring': querystring.urlencode(),
        'current_querystring': request.GET.urlencode(),
    }
    return render(request, 'main.html', context)


@login_required
@admin_required
def users(request):
    # List the registered users (photos are loaded from user_photo)
    users = UserImages.objects.select_related('user').defer('face_image', 'face_encoding').order_by('user__username')
    return render(request, 'users.html', {'users': users})


@login_required
@admin_required
def user_photo(request, pk):
    # Serve the registered face photo of a user
    face_image = get_object_or_404(UserImages.objects.only('face_image'), pk=pk).face_image
    if not face_image:
        raise Http404
    face_image = bytes(face_image)
    # The URL stays the same after an update, so the browser revalidates with the ETag
    etag = '"%s"' % hashlib.sha256(face_image).hexdigest()[:32]
    if request.headers.get('If-None-Match') == etag:
        response = HttpResponseNotModified()
    else:
        response = HttpResponse(face_image, content_type='image/jpeg')
    response['ETag'] = etag
    response['Cache-Control'] = 'private, no-cache'
    return response


@require_POST
@login_required
@admin_required
def delete(request, pk):
    # Delete the user (CASCADE also removes their face profile and attendance)
    user_img = get_object_or_404(UserImages.objects.select_related('user'), pk=pk)
    user_img.user.delete()

    return redirect('users')


@require_POST
@login_required
@admin_required
def remove(request, pk):
    # Delete an attendance record and go back to the same dashboard page and filters
    get_object_or_404(Attendance, pk=pk).delete()
    return redirect(_dashboard_url(request.POST.get('return_query', '')))


@login_required
@admin_required
def update(request, pk):
    # Get user extended data by primary key
    user_img = get_object_or_404(UserImages, pk=pk)
    user = user_img.user

    if request.method == 'POST':
        # Get the data sent from the form
        username = (request.POST.get('username') or '').strip()
        identification_number = (request.POST.get('identification_number') or '').strip()
        user_type = request.POST.get('user_type')
        face_image_data = request.POST.get('face_image')

        # Validate that all required text fields are provided
        if not username or not identification_number or not user_type:
            return JsonResponse({'status': 'error', 'message': '¡Todos los campos de texto son obligatorios!'})

        # Validate the field lengths and the user type
        length_error = _validate_profile_fields(username, identification_number)
        if length_error:
            return JsonResponse({'status': 'error', 'message': length_error})

        if user_type not in dict(UserImages.USER_TYPE_CHOICES):
            return JsonResponse({'status': 'error', 'message': '¡Tipo de usuario inválido!'})

        # Check if identification number or username already exists for another user
        if UserImages.objects.filter(identification_number=identification_number).exclude(pk=pk).exists():
            return JsonResponse({'status': 'error', 'message': '¡Este número de identificación ya está registrado para otro usuario!'})

        if User.objects.filter(username__iexact=username).exclude(pk=user.pk).exists():
            return JsonResponse({'status': 'error', 'message': '¡Este nombre de usuario ya está en uso por otro usuario!'})

        # Update text data
        user.username = username
        user_img.identification_number = identification_number
        user_img.user_type = user_type

        # If a new face image was sent, update it and its face encoding
        if face_image_data:
            try:
                face_image_binary, rgb_image = decode_face_image(face_image_data)
                face_encoding = encode_single_face(rgb_image)
            except NoFaceDetected:
                return JsonResponse({'status': 'error', 'message': '¡No se detectó ningún rostro en la nueva imagen!'})
            except MultipleFacesDetected:
                return JsonResponse({'status': 'error', 'message': '¡Se detectó más de un rostro en la nueva imagen!'})
            except FaceImageError:
                return JsonResponse({'status': 'error', 'message': '¡Datos de imagen facial inválidos!'})
            duplicate = _duplicate_face_message(face_encoding, exclude_user_id=user.pk)
            if duplicate:
                return JsonResponse({'status': 'error', 'message': duplicate})
            user_img.face_image = face_image_binary
            user_img.face_encoding = serialize_face_encoding(face_encoding)

        # Save updated user data
        try:
            with transaction.atomic():
                user.save()
                user_img.save()
        except IntegrityError:
            return JsonResponse({'status': 'error', 'message': '¡El nombre de usuario o el número de identificación ya está registrado!'})

        return JsonResponse({'status': 'success', 'message': '¡Usuario actualizado correctamente!', 'redirect': reverse('users')})

    return render(request, 'update.html', {'user_img': user_img})


@require_POST
@login_required
def signout(request):
    # Log out the user
    logout(request)
    return redirect('face')
