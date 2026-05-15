from django import forms
from django.contrib import admin
from django.utils.safestring import mark_safe
import courses.models as models


class EnrollmentAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'thinkific_user_id', 'course_id', 'activated_at', 'expiry_date')
    list_filter = ('user', 'activated_at', 'expiry_date', 'id', 'thinkific_user_id', 'course_id')


def _register(model, admin_class):
    admin.site.register(model, admin_class)


_register(models.Enrollment, EnrollmentAdmin)


@admin.register(models.CourseTranslation)
class CourseTranslationAdmin(admin.ModelAdmin):
    list_display = ('course_id', 'language', 'name')
    list_filter = ('language',)
    search_fields = ('course_id', 'name')
    ordering = ('course_id', 'language')


def _fetch_thinkific_courses():
    """Retourne [(id, name), ...] depuis l'API Thinkific, trié par nom."""
    try:
        from courses.views import thinkific
        items = thinkific.courses.list(limit=100).get('items', [])
        return sorted(
            [(c['id'], c.get('name', f"Cours #{c['id']}")) for c in items],
            key=lambda x: x[1].lower()
        )
    except Exception:
        return []


class CourseCategoryAdminForm(forms.ModelForm):
    selected_courses = forms.MultipleChoiceField(
        widget=forms.CheckboxSelectMultiple,
        required=False,
        label='Cours inclus dans cette catégorie',
        help_text='Cochez les cours Thinkific à associer à cette catégorie.',
    )

    class Meta:
        model = models.CourseCategory
        fields = '__all__'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        courses = _fetch_thinkific_courses()
        self.fields['selected_courses'].choices = [
            (str(cid), name) for cid, name in courses
        ]
        if self.instance and self.instance.pk:
            self.fields['selected_courses'].initial = [
                str(m.course_id) for m in self.instance.memberships.all()
            ]


@admin.register(models.CourseCategory)
class CourseCategoryAdmin(admin.ModelAdmin):
    form = CourseCategoryAdminForm
    list_display = ('name', 'order', 'slug', 'icon', 'color', 'is_active', 'course_count')
    list_display_links = ('name',)
    list_editable = ('order', 'is_active')
    prepopulated_fields = {'slug': ('name',)}
    fields = ('name', 'slug', 'icon', 'color', 'image', 'description', 'order', 'is_active', 'selected_courses')

    class Media:
        css = {'all': ('admin/css/category_courses.css',)}

    def course_count(self, obj):
        return obj.memberships.count()
    course_count.short_description = 'Nb cours'

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        selected_ids = [int(x) for x in form.cleaned_data.get('selected_courses', [])]

        # Cache des noms pour affichage futur
        courses = _fetch_thinkific_courses()
        course_map = {cid: name for cid, name in courses}

        # Supprimer les memberships désélectionnés
        obj.memberships.exclude(course_id__in=selected_ids).delete()

        # Créer / mettre à jour les memberships sélectionnés
        for cid in selected_ids:
            models.CourseCategoryMembership.objects.update_or_create(
                category=obj,
                course_id=cid,
                defaults={'course_name_cache': course_map.get(cid, '')},
            )
