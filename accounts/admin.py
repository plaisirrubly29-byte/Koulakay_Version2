from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .forms import CustomUserCreationForm, CustomUserChangeForm
from .models import User

from import_export.admin import ImportExportModelAdmin

class CustomUserAdmin(ImportExportModelAdmin, UserAdmin):
    add_form = CustomUserCreationForm
    form = CustomUserChangeForm
    model = User
    list_display = ("email", "first_name", "last_name", "thinkific_user_id", "is_staff", "is_active")
    list_filter = ("is_staff", "is_active")
    fieldsets = (
        ("Identité", {"fields": ("first_name", "last_name", "email", "password")}),
        ("Thinkific", {"fields": ("thinkific_user_id",)}),
        ("Permissions", {"fields": ("is_staff", "is_active", "groups", "user_permissions")}),
    )
    add_fieldsets = (
        ("Identité", {
            "classes": ("wide",),
            "fields": ("first_name", "last_name", "email", "password1", "password2"),
        }),
        ("Permissions", {
            "fields": ("is_staff", "is_active"),
        }),
    )
    search_fields = ("email", "first_name", "last_name")
    ordering = ("email",)

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        # Lier automatiquement au compte Thinkific si pas encore fait
        if not obj.thinkific_user_id:
            from accounts.views import get_thinkific_user_by_email
            from courses.monkey_patch.patch_thinkific import ThinkificExtend
            from django.conf import settings
            tk = ThinkificExtend(settings.THINKIFIC['AUTH_TOKEN'], settings.THINKIFIC['SITE_ID'])
            # 1. Chercher s'il existe déjà dans Thinkific
            thinkific_user = get_thinkific_user_by_email(obj.email)
            if not thinkific_user:
                # 2. Le créer dans Thinkific
                try:
                    raw_password = form.cleaned_data.get('password1')
                    thinkific_user = tk.users.create_user({
                        'email': obj.email,
                        'first_name': obj.first_name or '',
                        'last_name': obj.last_name or '',
                        'password': raw_password or '',
                        'send_welcome_email': False,
                    })
                except Exception as e:
                    self.message_user(request, f"Avertissement Thinkific : {e}", level='warning')
            if thinkific_user and thinkific_user.get('id'):
                obj.thinkific_user_id = thinkific_user['id']
                obj.save(update_fields=['thinkific_user_id'])


admin.site.register(User, CustomUserAdmin)