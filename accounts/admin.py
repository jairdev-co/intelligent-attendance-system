from django.contrib import admin
from .models import UserImages, Attendance

# Register your models here.
@admin.register(UserImages)
class UserImagesAdmin(admin.ModelAdmin):
    list_display = ['id', 'user', 'identification_number', 'user_type', 'has_face_encoding']
    search_fields = ['user__username', 'identification_number']
    list_filter = ['user_type']

    def has_face_encoding(self, obj):
        return bool(obj.face_encoding)
    has_face_encoding.boolean = True
    has_face_encoding.short_description = 'Face encoding'


@admin.register(Attendance)
class AttendanceAdmin(admin.ModelAdmin):
    list_display = ['id', 'user', 'attendance_type', 'timestamp']
    search_fields = ['user__username']
    list_filter = ['attendance_type', 'timestamp']
    date_hierarchy = 'timestamp'