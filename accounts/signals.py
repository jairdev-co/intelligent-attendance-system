# Standard library imports
import time

# Django imports
from django.contrib.auth.signals import user_logged_in
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

# Local imports
from .models import UserImages
from .utils import invalidate_face_index


@receiver(post_save, sender=UserImages)
@receiver(post_delete, sender=UserImages)
def _refresh_face_index(sender, **kwargs):
    # Rebuild the face index when a face profile is saved or deleted
    invalidate_face_index()


@receiver(user_logged_in)
def _store_login_time(sender, request, **kwargs):
    # Start of the session, used by SessionMaxAgeMiddleware
    request.session['login_at'] = time.time()
