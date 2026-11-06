# Import libraries
from django.db import models
from django.contrib.auth.models import User


# Face profile of a user: photo, face encoding, user type and consent
class UserImages(models.Model):
    # User type choices constants
    ADMINISTRATIVE = "administrative"
    STUDENT = "student"
    USER_TYPE_CHOICES = [
        (ADMINISTRATIVE, "Administrative"),
        (STUDENT, "Student"),
    ]
    # Model fields
    user_type = models.CharField(max_length=15, choices=USER_TYPE_CHOICES, default=STUDENT)
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='face_profile')
    identification_number = models.CharField(max_length=15, unique=True)
    face_image = models.BinaryField()
    face_encoding = models.BinaryField(null=True, blank=True)
    # Consent record (Ley 1581 de 2012), empty for users registered before the terms
    consent_accepted_at = models.DateTimeField(null=True, blank=True)
    consent_version = models.CharField(max_length=20, blank=True)
    consent_recorded_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name='+'
    )

    def __str__(self):
        # Return the username as string representation
        return self.user.username


# Entry or exit recorded at the attendance screen
class Attendance(models.Model):
    # Attendance type choices constants
    ENTRY = "entry"
    EXIT = "exit"
    ATTENDANCE_TYPE_CHOICES = [
        (ENTRY, "Entry"),
        (EXIT, "Exit"),
    ]
    # Model fields
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='attendances')
    timestamp = models.DateTimeField(auto_now_add=True)
    attendance_type = models.CharField(max_length=7, choices=ATTENDANCE_TYPE_CHOICES)

    # Newest first, with indexes for the dashboard and the duplicate check
    class Meta:
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['-timestamp']),
            models.Index(fields=['user', 'attendance_type', 'timestamp']),
        ]

    def __str__(self):
        # Return username, attendance type and timestamp as string representation
        return f"{self.user.username} - {self.attendance_type} at {self.timestamp}"
