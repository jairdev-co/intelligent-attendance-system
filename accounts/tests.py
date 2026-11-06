# Standard library
import base64
import time
from datetime import datetime, timezone as dt_timezone
from unittest import mock

# Third-party imports
import cv2
import numpy as np

# Django imports
from django.contrib.auth.models import User
from django.core.cache import cache, caches
from django.http import QueryDict
from django.test import TestCase, override_settings
from django.urls import reverse

# Local imports
from .models import Attendance, UserImages
from .utils import (
    FACE_MATCH_MARGIN,
    deserialize_face_encoding,
    identify_user,
    invalidate_face_index,
    serialize_face_encoding,
)

# Create your tests here.
# Fake encodings of two different people
FAKE_ENCODING = np.zeros(128, dtype=np.float64)
OTHER_ENCODING = np.ones(128, dtype=np.float64)

# Patch target for the face detector (mocked in most tests)
ENCODINGS_PATH = 'accounts.utils.face_recognition.face_encodings'
# Patch target for the rate limiter's clock
CLOCK_PATH = 'accounts.utils.time'


def fake_face_image():
    img = np.zeros((40, 40, 3), dtype=np.uint8)
    ok, buf = cv2.imencode('.jpg', img)
    b64 = base64.b64encode(buf.tobytes()).decode('utf-8')
    return 'data:image/jpeg;base64,' + b64


def create_registered_user(username='jair', identification='1001', encoding=FAKE_ENCODING):
    user = User.objects.create_user(username=username, password='password123')
    img = UserImages.objects.create(
        user=user,
        face_image=b'fake-image-bytes',
        identification_number=identification,
        user_type=UserImages.ADMINISTRATIVE,
        face_encoding=serialize_face_encoding(encoding),
    )
    return user, img


# Base test case, independent of .env (no HTTPS redirect or collectstatic needed)
@override_settings(
    SECURE_SSL_REDIRECT=False,
    STORAGES={'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}},
)
class BaseTest(TestCase):
    def setUp(self):
        # Start with empty rate limits and face index
        super().setUp()
        cache.clear()
        caches['ratelimit'].clear()
        invalidate_face_index()


# Storing and reading face encodings
class SerializationTests(BaseTest):
    def test_roundtrip_float32_encoding(self):
        encoded = serialize_face_encoding(FAKE_ENCODING)
        self.assertEqual(encoded, serialize_face_encoding(FAKE_ENCODING))
        restored = deserialize_face_encoding(encoded)
        self.assertEqual(restored.shape, (128,))
        self.assertEqual(restored.dtype, np.float64)
        np.testing.assert_allclose(restored, FAKE_ENCODING, atol=1e-6)

    def test_invalid_encoding_raises(self):
        with self.assertRaises(ValueError):
            deserialize_face_encoding(b'not-a-valid-encoding')


# Decoding of the images sent by the camera
class ImageDecodingTests(BaseTest):
    def test_decode_returns_rgb(self):
        from .utils import decode_face_image

        # A pure-red BGR JPEG must come back as red in the RED channel (RGB order)
        red_bgr = np.zeros((20, 20, 3), dtype=np.uint8)
        red_bgr[:, :, 2] = 255  # red in BGR
        ok, buf = cv2.imencode('.png', red_bgr)
        data_url = 'data:image/png;base64,' + base64.b64encode(buf.tobytes()).decode()
        _, rgb = decode_face_image(data_url)
        self.assertGreater(int(rgb[:, :, 0].mean()), 200)  # red channel
        self.assertLess(int(rgb[:, :, 2].mean()), 55)      # blue channel

    def test_invalid_data_raises(self):
        from .utils import FaceImageError, decode_face_image

        with self.assertRaises(FaceImageError):
            decode_face_image('data:image/jpeg;base64,not-base64!!')


# Identification and attendance at the attendance screen
class FaceRecognitionFlowTests(BaseTest):
    def setUp(self):
        super().setUp()
        self.user, self.user_img = create_registered_user()

    @mock.patch(ENCODINGS_PATH, return_value=[FAKE_ENCODING])
    def test_successful_recognition_no_attendance(self, _m):
        response = self.client.post(reverse('face'), {'face_image': fake_face_image()})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['status'], 'success')
        self.assertEqual(data['username'], 'jair')
        self.assertEqual(Attendance.objects.count(), 0)

    @mock.patch(ENCODINGS_PATH, return_value=[FAKE_ENCODING])
    def test_attendance_entry_recorded(self, _m):
        response = self.client.post(reverse('face'), {
            'face_image': fake_face_image(),
            'attendance_type': Attendance.ENTRY,
        })
        data = response.json()
        self.assertEqual(data['status'], 'success')
        attendance = Attendance.objects.get()
        self.assertEqual(attendance.user, self.user)
        self.assertEqual(attendance.attendance_type, Attendance.ENTRY)

    @mock.patch(ENCODINGS_PATH, return_value=[FAKE_ENCODING])
    def test_duplicate_attendance_blocked(self, _m):
        self.client.post(reverse('face'), {'face_image': fake_face_image(), 'attendance_type': Attendance.ENTRY})
        response = self.client.post(reverse('face'), {'face_image': fake_face_image(), 'attendance_type': Attendance.ENTRY})
        self.assertEqual(response.json()['status'], 'error')
        self.assertEqual(Attendance.objects.count(), 1)

    @mock.patch(ENCODINGS_PATH, return_value=[])
    def test_no_face_detected(self, _m):
        response = self.client.post(reverse('face'), {'face_image': fake_face_image()})
        self.assertEqual(response.json()['status'], 'error')

    @mock.patch(ENCODINGS_PATH, return_value=[FAKE_ENCODING, OTHER_ENCODING])
    def test_multiple_faces_rejected(self, _m):
        response = self.client.post(reverse('face'), {'face_image': fake_face_image()})
        self.assertEqual(response.json()['status'], 'error')
        self.assertEqual(Attendance.objects.count(), 0)

    @mock.patch(ENCODINGS_PATH, return_value=[OTHER_ENCODING])
    def test_unknown_face_not_matched(self, _m):
        response = self.client.post(reverse('face'), {'face_image': fake_face_image()})
        self.assertEqual(response.json()['status'], 'error')

    def test_face_image_required(self):
        response = self.client.post(reverse('face'), {})
        self.assertEqual(response.json()['status'], 'error')

    @mock.patch(CLOCK_PATH)
    @mock.patch(ENCODINGS_PATH, return_value=[FAKE_ENCODING])
    def test_face_rate_limited(self, _m, clock):
        from .views import FACE_RATE_LIMIT

        # Pin the clock so the burst can't straddle a window boundary
        clock.time.return_value = time.time()
        last = None
        for _ in range(FACE_RATE_LIMIT[0] + 2):
            last = self.client.post(reverse('face'), {'face_image': fake_face_image()})
        self.assertEqual(last.status_code, 429)

    @mock.patch(ENCODINGS_PATH, return_value=[])
    def test_no_face_returns_no_face_code(self, _m):
        response = self.client.post(reverse('face'), {'face_image': fake_face_image()})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'error')
        self.assertEqual(response.json()['code'], 'no_face')

    @mock.patch(ENCODINGS_PATH, return_value=[])
    def test_empty_frame_does_not_touch_database(self, _m):
        # Idle attendance screen polls must not hit the database
        with self.assertNumQueries(0):
            response = self.client.post(reverse('face'), {'face_image': fake_face_image()})
        self.assertEqual(response.json()['code'], 'no_face')

    @mock.patch(CLOCK_PATH)
    @mock.patch(ENCODINGS_PATH, return_value=[])
    def test_idle_attendance_screen_is_never_rate_limited(self, _m, clock):
        # An idle screen polling every 4 s for 10 minutes is never rate limited
        start = time.time()
        for i in range(150):
            clock.time.return_value = start + i * 4
            response = self.client.post(reverse('face'), {'face_image': fake_face_image()})
            self.assertEqual(response.status_code, 200, f'blocked at poll {i} (t={i * 4}s)')

    @mock.patch(CLOCK_PATH)
    @mock.patch(ENCODINGS_PATH, return_value=[])
    def test_rate_limit_resets_in_next_window(self, _m, clock):
        from .views import FACE_RATE_LIMIT

        limit, window = FACE_RATE_LIMIT
        start = (int(time.time()) // window) * window
        clock.time.return_value = start
        statuses = [
            self.client.post(reverse('face'), {'face_image': fake_face_image()}).status_code
            for _ in range(limit + 1)
        ]
        self.assertEqual(statuses[:limit], [200] * limit)
        self.assertEqual(statuses[limit], 429)

        clock.time.return_value = start + window
        response = self.client.post(reverse('face'), {'face_image': fake_face_image()})
        self.assertEqual(response.status_code, 200)


# Nearest-neighbor matching against a realistically sized enrollment
class IdentificationScaleTests(BaseTest):
    def test_finds_right_person_among_300(self):
        rng = np.random.default_rng(0)
        encodings = rng.normal(0, 0.1, (300, 128))
        # Bulk-created (no password hashing) to keep the test fast
        users = User.objects.bulk_create([User(username=f'u{i}') for i in range(300)])
        UserImages.objects.bulk_create([
            UserImages(user=u, face_image=b'x', identification_number=str(5000 + i),
                       face_encoding=serialize_face_encoding(enc))
            for i, (u, enc) in enumerate(zip(users, encodings))
        ])
        invalidate_face_index()  # bulk_create skips the post_save signal
        for k in (0, 137, 299):
            probe = encodings[k] + rng.normal(0, 0.01, 128)
            self.assertEqual(identify_user(probe), users[k])
        self.assertIsNone(identify_user(rng.normal(0, 0.1, 128)))

    def test_index_picks_up_users_enrolled_after_first_lookup(self):
        self.assertIsNone(identify_user(FAKE_ENCODING))  # builds an empty in-memory index
        user, _ = create_registered_user(encoding=FAKE_ENCODING)  # signal bumps the version
        self.assertEqual(identify_user(FAKE_ENCODING), user)

    def test_ambiguous_match_rejected(self):
        base = np.zeros(128)
        near = base.copy()
        near[0] = FACE_MATCH_MARGIN / 2
        create_registered_user('ana', '7001', encoding=base)
        create_registered_user('eva', '7002', encoding=near)
        self.assertIsNone(identify_user(base))

    def test_clear_winner_accepted_despite_second_candidate(self):
        base = np.zeros(128)
        other = base.copy()
        other[0] = 0.45
        ana, _ = create_registered_user('ana', '7001', encoding=base)
        create_registered_user('eva', '7002', encoding=other)
        self.assertEqual(identify_user(base), ana)


# Serving and caching of registered photos
class UserPhotoTests(BaseTest):
    def setUp(self):
        super().setUp()
        self.user, self.user_img = create_registered_user()

    def test_admin_gets_photo_and_list_does_not_inline_it(self):
        admin = User.objects.create_superuser(username='admin', password='password123')
        self.client.force_login(admin)
        response = self.client.get(reverse('user_photo', args=[self.user_img.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'image/jpeg')
        self.assertEqual(response.content, b'fake-image-bytes')
        page = self.client.get(reverse('users')).content.decode()
        self.assertIn(reverse('user_photo', args=[self.user_img.pk]), page)
        self.assertNotIn('base64', page)

    def test_updated_photo_is_not_served_from_cache(self):
        self.client.force_login(User.objects.create_superuser(username='admin', password='password123'))
        url = reverse('user_photo', args=[self.user_img.pk])
        first = self.client.get(url)
        self.assertIn('no-cache', first['Cache-Control'])
        # Unchanged photo: the browser's copy is still valid
        self.assertEqual(self.client.get(url, HTTP_IF_NONE_MATCH=first['ETag']).status_code, 304)

        self.user_img.face_image = b'new-image-bytes'
        self.user_img.save()
        # Updated photo: same URL, but the old ETag no longer matches
        second = self.client.get(url, HTTP_IF_NONE_MATCH=first['ETag'])
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.content, b'new-image-bytes')
        self.assertNotEqual(second['ETag'], first['ETag'])

    def test_photo_requires_admin(self):
        self.client.force_login(User.objects.create_user(username='regular', password='password123'))
        response = self.client.get(reverse('user_photo', args=[self.user_img.pk]))
        self.assertIn(response.status_code, (302, 403))
        self.client.logout()
        self.assertEqual(self.client.get(reverse('user_photo', args=[self.user_img.pk])).status_code, 302)


# User registration, validation and consent
class RegisterViewTests(BaseTest):
    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_superuser(username='admin', password='password123')
        self.client.force_login(self.admin)

    @mock.patch(ENCODINGS_PATH, return_value=[FAKE_ENCODING])
    def test_register_new_user(self, _m):
        response = self.client.post(reverse('register'), {
            'username': 'alice',
            'identification_number': '2002',
            'user_type': UserImages.STUDENT,
            'face_image': fake_face_image(),
            'consent': 'si',
        })
        data = response.json()
        self.assertEqual(data['status'], 'success')
        user_img = UserImages.objects.get(user__username='alice')
        self.assertEqual(user_img.identification_number, '2002')
        self.assertTrue(user_img.face_encoding)
        self.assertFalse(user_img.user.has_usable_password())

    @mock.patch(ENCODINGS_PATH, return_value=[FAKE_ENCODING])
    def test_register_duplicate_identification(self, _m):
        create_registered_user(identification='1001')
        response = self.client.post(reverse('register'), {
            'username': 'bob',
            'identification_number': '1001',
            'user_type': UserImages.STUDENT,
            'face_image': fake_face_image(),
            'consent': 'si',
        })
        self.assertEqual(response.json()['status'], 'error')
        self.assertEqual(UserImages.objects.count(), 1)

    @mock.patch(ENCODINGS_PATH, return_value=[FAKE_ENCODING, OTHER_ENCODING])
    def test_register_rejects_multi_face_photo(self, _m):
        response = self.client.post(reverse('register'), {
            'username': 'alice',
            'identification_number': '2002',
            'user_type': UserImages.STUDENT,
            'face_image': fake_face_image(),
            'consent': 'si',
        })
        self.assertEqual(response.json()['status'], 'error')
        self.assertFalse(User.objects.filter(username='alice').exists())

    @mock.patch(ENCODINGS_PATH, return_value=[FAKE_ENCODING])
    def test_register_rejects_face_already_enrolled(self, _m):
        create_registered_user(username='jair', identification='1001', encoding=FAKE_ENCODING)
        response = self.client.post(reverse('register'), {
            'username': 'jair_otra_vez',
            'identification_number': '2002',
            'user_type': UserImages.STUDENT,
            'face_image': fake_face_image(),
            'consent': 'si',
        })
        data = response.json()
        self.assertEqual(data['status'], 'error')
        self.assertIn('jair', data['message'])
        self.assertFalse(User.objects.filter(username='jair_otra_vez').exists())

    @mock.patch(ENCODINGS_PATH, return_value=[FAKE_ENCODING])
    def test_register_rejects_overlong_identification(self, _m):
        response = self.client.post(reverse('register'), {
            'username': 'alice',
            'identification_number': '1' * 16,
            'user_type': UserImages.STUDENT,
            'face_image': fake_face_image(),
            'consent': 'si',
        })
        self.assertEqual(response.json()['status'], 'error')
        self.assertFalse(User.objects.filter(username='alice').exists())

    @mock.patch(ENCODINGS_PATH, return_value=[FAKE_ENCODING])
    def test_register_stores_consent_proof(self, _m):
        from .views import TERMS_VERSION

        self.client.post(reverse('register'), {
            'username': 'alice',
            'identification_number': '2002',
            'user_type': UserImages.STUDENT,
            'face_image': fake_face_image(),
            'consent': 'si',
        })
        user_img = UserImages.objects.get(user__username='alice')
        self.assertIsNotNone(user_img.consent_accepted_at)
        self.assertEqual(user_img.consent_version, TERMS_VERSION)
        self.assertEqual(user_img.consent_recorded_by, self.admin)

    @mock.patch(ENCODINGS_PATH, return_value=[FAKE_ENCODING])
    def test_register_requires_consent(self, _m):
        response = self.client.post(reverse('register'), {
            'username': 'alice',
            'identification_number': '2002',
            'user_type': UserImages.STUDENT,
            'face_image': fake_face_image(),
        })
        data = response.json()
        self.assertEqual(data['status'], 'error')
        self.assertIn('términos', data['message'])
        self.assertFalse(User.objects.filter(username='alice').exists())

    @mock.patch(ENCODINGS_PATH, return_value=[FAKE_ENCODING])
    def test_register_refused_when_terms_not_accepted(self, _m):
        # "No acepta": the face can't be stored without the holder's authorization
        response = self.client.post(reverse('register'), {
            'username': 'alice',
            'identification_number': '2002',
            'user_type': UserImages.STUDENT,
            'face_image': fake_face_image(),
            'consent': 'no',
        })
        data = response.json()
        self.assertEqual(data['status'], 'error')
        self.assertIn('otro medio', data['message'])
        self.assertFalse(User.objects.filter(username='alice').exists())

    def test_register_missing_fields(self):
        response = self.client.post(reverse('register'), {'username': 'only-name'})
        self.assertEqual(response.json()['status'], 'error')
        self.assertFalse(User.objects.filter(username='only-name').exists())


# Editing a user's data and photo
class UpdateViewTests(BaseTest):
    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_superuser(username='admin', password='password123')
        self.client.force_login(self.admin)
        self.user, self.user_img = create_registered_user()

    def test_update_text_fields_only(self):
        response = self.client.post(reverse('update', args=[self.user_img.pk]), {
            'username': 'jair_new',
            'identification_number': '9999',
            'user_type': UserImages.STUDENT,
        })
        self.assertEqual(response.json()['status'], 'success')
        self.user_img.refresh_from_db()
        self.assertEqual(self.user_img.identification_number, '9999')
        self.assertEqual(self.user_img.user.username, 'jair_new')

    @mock.patch(ENCODINGS_PATH, return_value=[FAKE_ENCODING])
    def test_update_with_new_photo_recomputes_encoding(self, _m):
        old_image = self.user_img.face_image
        response = self.client.post(reverse('update', args=[self.user_img.pk]), {
            'username': self.user.username,
            'identification_number': self.user_img.identification_number,
            'user_type': self.user_img.user_type,
            'face_image': fake_face_image(),
        })
        self.assertEqual(response.json()['status'], 'success')
        self.user_img.refresh_from_db()
        self.assertNotEqual(self.user_img.face_image, old_image)
        deserialize_face_encoding(self.user_img.face_encoding)


    @mock.patch(ENCODINGS_PATH, return_value=[OTHER_ENCODING])
    def test_update_rejects_photo_of_another_enrolled_user(self, _m):
        create_registered_user(username='eva', identification='3003', encoding=OTHER_ENCODING)
        response = self.client.post(reverse('update', args=[self.user_img.pk]), {
            'username': self.user.username,
            'identification_number': self.user_img.identification_number,
            'user_type': self.user_img.user_type,
            'face_image': fake_face_image(),
        })
        self.assertEqual(response.json()['status'], 'error')
        self.user_img.refresh_from_db()
        np.testing.assert_allclose(deserialize_face_encoding(self.user_img.face_encoding), FAKE_ENCODING)


# Terms and conditions page
class TermsViewTests(BaseTest):
    def test_terms_page_is_public(self):
        response = self.client.get(reverse('terms'))
        self.assertEqual(response.status_code, 200)
        # The essentials the law requires: sensitive data and voluntary consent
        self.assertContains(response, 'dato sensible')
        self.assertContains(response, 'aceptar es voluntario')

    def test_registration_links_to_terms(self):
        # The Sí / No question about the terms must link to them
        admin = User.objects.create_user(username='admin', password='password123', is_staff=True)
        self.client.force_login(admin)
        response = self.client.get(reverse('register'))
        self.assertContains(response, reverse('terms'))


# Login, logout and session expiry
class AuthViewTests(BaseTest):
    def test_login_valid_credentials(self):
        User.objects.create_user(username='dave', password='password123')
        response = self.client.post(reverse('login'), {'username': 'dave', 'password': 'password123'})
        self.assertEqual(response.status_code, 302)

    def test_login_admin_goes_to_next_page(self):
        # e.g. /users/ redirects to the login with ?next=/users/
        User.objects.create_user(username='dave', password='password123', is_staff=True)
        response = self.client.post(reverse('login'), {
            'username': 'dave', 'password': 'password123', 'next': reverse('users'),
        })
        self.assertRedirects(response, reverse('users'), fetch_redirect_response=False)

    def test_login_ignores_external_next(self):
        User.objects.create_user(username='dave', password='password123', is_staff=True)
        response = self.client.post(reverse('login'), {
            'username': 'dave', 'password': 'password123', 'next': 'https://evil.example.com/',
        })
        self.assertRedirects(response, reverse('main'), fetch_redirect_response=False)

    def test_login_invalid_credentials(self):
        User.objects.create_user(username='dave', password='password123')
        response = self.client.post(reverse('login'), {'username': 'dave', 'password': 'wrong'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'El nombre de usuario o la contraseña es incorrecto.')

    @mock.patch(CLOCK_PATH)
    def test_login_rate_limited(self, clock):
        from .views import LOGIN_RATE_LIMIT

        clock.time.return_value = time.time()
        User.objects.create_user(username='dave', password='password123')
        last = None
        for _ in range(LOGIN_RATE_LIMIT[0] + 2):
            last = self.client.post(reverse('login'), {'username': 'dave', 'password': 'wrong'})
        self.assertEqual(last.status_code, 429)

    def test_admin_session_is_short_on_the_shared_attendance_pc(self):
        # Biometric data: the session ends after 15 idle minutes or on browser close
        User.objects.create_user(username='dave', password='password123', is_staff=True)
        self.client.post(reverse('login'), {'username': 'dave', 'password': 'password123'})
        self.assertEqual(self.client.session.get_expiry_age(), 15 * 60)
        self.assertTrue(self.client.session.get_expire_at_browser_close())

    def test_session_ends_after_max_age(self):
        # 8 hours after login the session ends even if the user stays active
        from django.conf import settings

        User.objects.create_user(username='dave', password='password123', is_staff=True)
        self.client.post(reverse('login'), {'username': 'dave', 'password': 'password123'})
        self.assertEqual(self.client.get(reverse('main')).status_code, 200)
        session = self.client.session
        session['login_at'] -= settings.SESSION_MAX_AGE + 1
        session.save()
        response = self.client.get(reverse('main'))
        self.assertRedirects(response, f"{reverse('login')}?next={reverse('main')}", fetch_redirect_response=False)

    def test_login_explains_expired_session(self):
        response = self.client.get(reverse('login'), {'next': reverse('main')})
        self.assertContains(response, 'Su sesión expiró')
        self.assertNotContains(self.client.get(reverse('login')), 'Su sesión expiró')

    def test_logout_requires_post(self):
        user, _ = create_registered_user()
        self.client.force_login(user)
        self.assertEqual(self.client.get(reverse('logout')).status_code, 405)
        self.assertEqual(self.client.post(reverse('logout')).status_code, 302)


# Dashboard, filters, CSV export and login-protected pages
class ProtectedViewTests(BaseTest):
    def test_main_requires_login(self):
        from django.conf import settings

        response = self.client.get(reverse('main'))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith(settings.LOGIN_URL))

    def test_users_requires_login(self):
        self.assertEqual(self.client.get(reverse('users')).status_code, 302)

    def test_main_requires_admin(self):
        user, _ = create_registered_user()
        self.client.force_login(user)
        self.assertIn(self.client.get(reverse('main')).status_code, (302, 403))

    def test_main_renders_context(self):
        user, _ = create_registered_user()
        user.is_staff = True
        user.is_superuser = True
        user.save()
        Attendance.objects.create(user=user, attendance_type=Attendance.ENTRY)
        self.client.force_login(user)
        response = self.client.get(reverse('main'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['attendance_list']), 1)
        record = response.context['attendance_list'][0]
        self.assertEqual(record['fullname'], 'jair')
        self.assertEqual(record['user_type'], UserImages.ADMINISTRATIVE)

    def test_main_filters_and_paginates(self):
        admin = User.objects.create_superuser(username='admin', password='password123')
        self.client.force_login(admin)
        student, _ = create_registered_user(username='stud', identification='7', encoding=OTHER_ENCODING)
        student_img = student.face_profile
        student_img.user_type = UserImages.STUDENT
        student_img.save()
        for _ in range(3):
            Attendance.objects.create(user=admin, attendance_type=Attendance.ENTRY)
        Attendance.objects.create(user=student, attendance_type=Attendance.EXIT)

        response = self.client.get(reverse('main'), {'type': Attendance.EXIT})
        self.assertEqual(len(response.context['attendance_list']), 1)

        response = self.client.get(reverse('main'), {'user_type': UserImages.STUDENT})
        self.assertEqual(len(response.context['attendance_list']), 1)

    def test_main_page_size_selector(self):
        from .views import ATTENDANCE_PAGE_SIZE

        admin = User.objects.create_superuser(username='admin', password='password123')
        self.client.force_login(admin)
        Attendance.objects.bulk_create(
            [Attendance(user=admin, attendance_type=Attendance.ENTRY) for _ in range(60)]
        )
        url = reverse('main')

        # Default size: 25 + 25 + 10
        response = self.client.get(url)
        self.assertEqual(len(response.context['attendance_list']), ATTENDANCE_PAGE_SIZE)
        self.assertEqual(response.context['page_obj'].paginator.num_pages, 3)
        response = self.client.get(url, {'page': 3})
        self.assertEqual(len(response.context['attendance_list']), 10)

        # Allowed sizes are used
        response = self.client.get(url, {'per_page': 50, 'page': 2})
        self.assertEqual(len(response.context['attendance_list']), 10)
        self.assertEqual(response.context['filters']['per_page'], 50)
        # Page links keep per_page but drop the current page number
        querystring = QueryDict(response.context['base_querystring'])
        self.assertEqual(querystring.get('per_page'), '50')
        self.assertNotIn('page', querystring)

        # Values outside the choices fall back to the default
        for value in ('100000', '7', '-1', 'abc', ''):
            response = self.client.get(url, {'per_page': value})
            self.assertEqual(len(response.context['attendance_list']), ATTENDANCE_PAGE_SIZE, value)

        # Out-of-range page shows the last one instead of failing
        response = self.client.get(url, {'page': 999})
        self.assertEqual(response.context['page_obj'].number, 3)

    def test_main_ignores_invalid_date_filter(self):
        admin = User.objects.create_superuser(username='admin', password='password123')
        self.client.force_login(admin)
        Attendance.objects.create(user=admin, attendance_type=Attendance.ENTRY)
        response = self.client.get(reverse('main'), {'date_from': 'garbage', 'date_to': '2020-02-30'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['attendance_list']), 1)

    def test_main_csv_export(self):
        admin = User.objects.create_superuser(username='admin', password='password123')
        self.client.force_login(admin)
        Attendance.objects.create(user=admin, attendance_type=Attendance.ENTRY)
        response = self.client.get(reverse('main'), {'export': 'csv'})
        self.assertEqual(response['Content-Type'], 'text/csv; charset=utf-8')
        body = b''.join(response.streaming_content).decode()
        self.assertIn('Nombre completo', body)
        self.assertIn('admin', body)

    def test_csv_export_matches_dashboard(self):
        admin = User.objects.create_superuser(username='admin', password='password123')
        self.client.force_login(admin)
        user, _ = create_registered_user(username='Brayan Jair Enriquez', identification='1007254236')
        attendance = Attendance.objects.create(user=user, attendance_type=Attendance.ENTRY)
        # 2026-09-16 21:22:45 UTC == 4:22:45 PM in America/Bogota
        Attendance.objects.filter(pk=attendance.pk).update(
            timestamp=datetime(2026, 9, 16, 21, 22, 45, tzinfo=dt_timezone.utc))

        csv_body = b''.join(self.client.get(reverse('main'), {'export': 'csv'}).streaming_content).decode('utf-8')
        self.assertTrue(csv_body.startswith('\ufeffNúmero de identificación,'))
        self.assertIn('1007254236,Brayan Jair Enriquez,Administrativo,Entrada,16/09/2026,4:22:45 PM', csv_body)

        html = self.client.get(reverse('main')).content.decode()
        for value in ('1007254236', 'Brayan Jair Enriquez', 'Administrativo', 'Entrada', '16/09/2026', '4:22:45 PM'):
            self.assertIn(value, html)


# Admin pages are forbidden for non-staff users
class AdminAuthorizationTests(BaseTest):
    def setUp(self):
        super().setUp()
        self.user, self.user_img = create_registered_user()
        self.non_admin = User.objects.create_user(username='regular', password='password123')

    def test_register_requires_admin(self):
        self.client.force_login(self.non_admin)
        response = self.client.post(reverse('register'), {
            'username': 'alice',
            'identification_number': '3001',
            'user_type': UserImages.STUDENT,
            'face_image': fake_face_image(),
            'consent': 'si',
        })
        self.assertIn(response.status_code, (302, 403))

    def test_update_requires_admin(self):
        self.client.force_login(self.non_admin)
        response = self.client.post(reverse('update', args=[self.user_img.pk]), {
            'username': 'hacked',
            'identification_number': '9999',
            'user_type': UserImages.STUDENT,
        })
        self.assertIn(response.status_code, (302, 403))
        self.user_img.refresh_from_db()
        self.assertEqual(self.user_img.identification_number, '1001')

    def test_delete_requires_admin(self):
        self.client.force_login(self.non_admin)
        response = self.client.post(reverse('delete', args=[self.user_img.pk]))
        self.assertIn(response.status_code, (302, 403))
        self.assertTrue(User.objects.filter(pk=self.user.pk).exists())

    def test_delete_requires_post(self):
        admin = User.objects.create_superuser(username='admin', password='password123')
        self.client.force_login(admin)
        self.assertEqual(self.client.get(reverse('delete', args=[self.user_img.pk])).status_code, 405)
        self.assertTrue(UserImages.objects.filter(pk=self.user_img.pk).exists())

    def test_remove_requires_admin(self):
        attendance = Attendance.objects.create(user=self.user, attendance_type=Attendance.ENTRY)
        self.client.force_login(self.non_admin)
        response = self.client.post(reverse('remove', args=[attendance.pk]))
        self.assertIn(response.status_code, (302, 403))
        self.assertTrue(Attendance.objects.filter(pk=attendance.pk).exists())

    def test_remove_requires_post(self):
        admin = User.objects.create_superuser(username='admin', password='password123')
        attendance = Attendance.objects.create(user=self.user, attendance_type=Attendance.ENTRY)
        self.client.force_login(admin)
        self.assertEqual(self.client.get(reverse('remove', args=[attendance.pk])).status_code, 405)
        self.assertTrue(Attendance.objects.filter(pk=attendance.pk).exists())

    def test_remove_returns_to_same_page_and_filters(self):
        admin = User.objects.create_superuser(username='admin', password='password123')
        attendance = Attendance.objects.create(user=self.user, attendance_type=Attendance.ENTRY)
        self.client.force_login(admin)
        response = self.client.post(reverse('remove', args=[attendance.pk]), {
            'return_query': 'user_type=student&date_from=2026-11-01&per_page=50&page=3',
        })
        self.assertFalse(Attendance.objects.filter(pk=attendance.pk).exists())
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith(reverse('main') + '?'))
        self.assertEqual(QueryDict(response.url.split('?', 1)[1]).dict(), {
            'user_type': 'student', 'date_from': '2026-11-01', 'per_page': '50', 'page': '3',
        })

    def test_remove_return_query_cannot_redirect_elsewhere(self):
        admin = User.objects.create_superuser(username='admin', password='password123')
        self.client.force_login(admin)
        for value in ('', 'next=https://evil.example', '//evil.example', 'export=csv'):
            attendance = Attendance.objects.create(user=self.user, attendance_type=Attendance.ENTRY)
            response = self.client.post(reverse('remove', args=[attendance.pk]), {'return_query': value})
            self.assertEqual(response.url, reverse('main'), value)

    def test_users_requires_admin(self):
        self.client.force_login(self.non_admin)
        self.assertIn(self.client.get(reverse('users')).status_code, (302, 403))


# The person tapping Entry/Exit must be the one recognized
class AttendanceTapTests(BaseTest):
    def setUp(self):
        super().setUp()
        self.ana, _ = create_registered_user('ana', '7001', encoding=FAKE_ENCODING)
        self.eva, _ = create_registered_user('eva', '7002', encoding=OTHER_ENCODING)

    @mock.patch(ENCODINGS_PATH, return_value=[OTHER_ENCODING])
    def test_someone_else_tapping_is_refused(self, _m):
        # The attendance screen showed "ana", but the fresh photo taken on tap is eva
        response = self.client.post(reverse('face'), {
            'face_image': fake_face_image(), 'attendance_type': Attendance.ENTRY,
            'expected_username': 'ana',
        })
        self.assertEqual(response.json()['code'], 'person_changed')
        self.assertEqual(Attendance.objects.count(), 0)

    @mock.patch(ENCODINGS_PATH, return_value=[FAKE_ENCODING])
    def test_same_person_tapping_is_recorded(self, _m):
        response = self.client.post(reverse('face'), {
            'face_image': fake_face_image(), 'attendance_type': Attendance.ENTRY,
            'expected_username': 'ana',
        })
        self.assertEqual(response.json()['status'], 'success')
        self.assertEqual(Attendance.objects.get().user, self.ana)


# Oversized or unsupported images are rejected before decoding
class ImageLimitTests(BaseTest):
    def _data_url(self, width, height, ext='.png'):
        ok, buf = cv2.imencode(ext, np.zeros((height, width, 3), dtype=np.uint8))
        mime = 'png' if ext == '.png' else 'jpeg'
        return f'data:image/{mime};base64,' + base64.b64encode(buf.tobytes()).decode()

    def test_oversized_image_rejected_before_decoding(self):
        from .utils import FaceImageError, decode_face_image

        with mock.patch('accounts.utils.cv2.imdecode') as imdecode:
            with self.assertRaises(FaceImageError):
                decode_face_image(self._data_url(4000, 4000))
            imdecode.assert_not_called()

    def test_huge_declared_size_rejected_cleanly(self):
        # A PNG header claiming 20000x20000 must raise FaceImageError
        import struct
        import zlib
        from .utils import FaceImageError, decode_face_image

        def chunk(kind, data):
            return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))

        png = (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 20000, 20000, 8, 2, 0, 0, 0))
               + chunk(b'IEND', b''))
        with self.assertRaises(FaceImageError):
            decode_face_image('data:image/png;base64,' + base64.b64encode(png).decode())

    def test_attendance_frame_size_accepted(self):
        from .utils import decode_face_image

        _, rgb = decode_face_image(self._data_url(640, 480, '.jpg'))
        self.assertEqual(rgb.shape, (480, 640, 3))

    def test_unsupported_format_rejected(self):
        from PIL import Image
        import io
        from .utils import FaceImageError, decode_face_image

        buf = io.BytesIO()
        Image.new('RGB', (10, 10)).save(buf, format='GIF')
        with self.assertRaises(FaceImageError):
            decode_face_image('data:image/gif;base64,' + base64.b64encode(buf.getvalue()).decode())

    def test_attendance_answers_oversized_image_with_error(self):
        create_registered_user()
        response = self.client.post(reverse('face'), {'face_image': self._data_url(3000, 3000)})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'error')


# Client IP resolution from X-Forwarded-For
class ClientIpTests(BaseTest):
    def _request(self, forwarded):
        from django.test import RequestFactory

        return RequestFactory().get('/', REMOTE_ADDR='10.0.0.1', HTTP_X_FORWARDED_FOR=forwarded)

    def test_one_proxy_takes_last_entry(self):
        from .utils import client_ip

        with self.settings(TRUSTED_PROXY_COUNT=1):
            self.assertEqual(client_ip(self._request('6.6.6.6, 200.1.2.3')), '200.1.2.3')

    def test_two_proxies_skip_load_balancer(self):
        from .utils import client_ip

        with self.settings(TRUSTED_PROXY_COUNT=2):
            self.assertEqual(client_ip(self._request('6.6.6.6, 200.1.2.3, 35.191.0.1')), '200.1.2.3')

    def test_no_header_uses_remote_addr(self):
        from .utils import client_ip

        self.assertEqual(client_ip(self._request('')), '10.0.0.1')


# Field lengths and uniqueness are enforced
class DataIntegrityTests(BaseTest):
    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_superuser(username='admin', password='password123')
        self.client.force_login(self.admin)

    @mock.patch(ENCODINGS_PATH, return_value=[FAKE_ENCODING])
    def test_identification_must_be_digits(self, _m):
        for bad in ('1e5', '-3', '1.5', 'abc', '١٢٣'):
            response = self.client.post(reverse('register'), {
                'username': f'u{bad}', 'identification_number': bad,
                'user_type': UserImages.STUDENT, 'face_image': fake_face_image(),
            })
            self.assertEqual(response.json()['status'], 'error', bad)
        self.assertEqual(UserImages.objects.count(), 0)

    @mock.patch(ENCODINGS_PATH, return_value=[FAKE_ENCODING])
    def test_failed_profile_leaves_no_orphan_user(self, _m):
        with mock.patch('accounts.views.UserImages.objects.create', side_effect=RuntimeError('db down')):
            with self.assertRaises(RuntimeError):
                self.client.post(reverse('register'), {
                    'username': 'alice', 'identification_number': '2002',
                    'user_type': UserImages.STUDENT, 'face_image': fake_face_image(),
                    'consent': 'si',
                })
        self.assertFalse(User.objects.filter(username='alice').exists())

    def test_delete_user_removes_profile_and_attendance(self):
        user, img = create_registered_user()
        Attendance.objects.create(user=user, attendance_type=Attendance.ENTRY)
        self.client.post(reverse('delete', args=[img.pk]))
        self.assertFalse(User.objects.filter(pk=user.pk).exists())
        self.assertEqual(UserImages.objects.count(), 0)
        self.assertEqual(Attendance.objects.count(), 0)

    def test_admin_redirect_goes_straight_to_login_url(self):
        from django.conf import settings

        self.client.logout()
        response = self.client.get(reverse('users'))
        self.assertTrue(response.url.startswith(settings.LOGIN_URL + '?'))
