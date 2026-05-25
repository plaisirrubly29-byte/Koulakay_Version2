from django import forms
from django.contrib import admin
from django.http import HttpResponseRedirect
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


def _fetch_thinkific_bundles():
    """Retourne [(bundle_id, name), ...] depuis l'API Thinkific (via products)."""
    try:
        from courses.views import thinkific, _fetch_bundle_details
        product_items = thinkific.products.list(limit=100).get('items', [])
        bundle_products = [p for p in product_items if p.get('productable_type') == 'Bundle']
        result = []
        for bp in bundle_products:
            bid = bp.get('productable_id')
            if not bid:
                continue
            try:
                info = _fetch_bundle_details(bid)
                name = info.get('name', f'Bundle #{bid}')
            except Exception:
                name = f'Bundle #{bid}'
            result.append((bid, name))
        return sorted(result, key=lambda x: x[1].lower())
    except Exception:
        return []


class CourseCategoryAdminForm(forms.ModelForm):
    selected_courses = forms.MultipleChoiceField(
        widget=forms.CheckboxSelectMultiple,
        required=False,
        label='Cours inclus dans cette catégorie',
        help_text='Cochez les cours Thinkific à associer à cette catégorie.',
    )
    selected_bundles = forms.MultipleChoiceField(
        widget=forms.CheckboxSelectMultiple,
        required=False,
        label='Bundles inclus dans cette catégorie',
        help_text='Cochez les bundles Thinkific à associer à cette catégorie.',
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
        bundles = _fetch_thinkific_bundles()
        self.fields['selected_bundles'].choices = [
            (str(bid), name) for bid, name in bundles
        ]
        if self.instance and self.instance.pk:
            self.fields['selected_courses'].initial = [
                str(m.course_id) for m in self.instance.memberships.all()
            ]
            self.fields['selected_bundles'].initial = [
                str(m.bundle_id) for m in self.instance.bundle_memberships.all()
            ]


@admin.register(models.CourseCategory)
class CourseCategoryAdmin(admin.ModelAdmin):
    form = CourseCategoryAdminForm
    list_display = ('name', 'order', 'slug', 'icon', 'color', 'is_active', 'course_count', 'bundle_count')
    list_display_links = ('name',)
    list_editable = ('order', 'is_active')
    prepopulated_fields = {'slug': ('name',)}
    fields = ('name', 'slug', 'icon', 'color', 'image', 'description', 'order', 'is_active', 'selected_courses', 'selected_bundles')

    class Media:
        css = {'all': ('admin/css/category_courses.css',)}

    def course_count(self, obj):
        return obj.memberships.count()
    course_count.short_description = 'Nb cours'

    def bundle_count(self, obj):
        return obj.bundle_memberships.count()
    bundle_count.short_description = 'Nb bundles'

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)

        # ── Cours ──
        selected_ids = [int(x) for x in form.cleaned_data.get('selected_courses', [])]
        courses = _fetch_thinkific_courses()
        course_map = {cid: name for cid, name in courses}
        obj.memberships.exclude(course_id__in=selected_ids).delete()
        for cid in selected_ids:
            models.CourseCategoryMembership.objects.update_or_create(
                category=obj,
                course_id=cid,
                defaults={'course_name_cache': course_map.get(cid, '')},
            )

        # ── Bundles ──
        selected_bundle_ids = [int(x) for x in form.cleaned_data.get('selected_bundles', [])]
        bundles = _fetch_thinkific_bundles()
        bundle_map = {bid: name for bid, name in bundles}
        obj.bundle_memberships.exclude(bundle_id__in=selected_bundle_ids).delete()
        for bid in selected_bundle_ids:
            models.BundleCategoryMembership.objects.update_or_create(
                category=obj,
                bundle_id=bid,
                defaults={'bundle_name_cache': bundle_map.get(bid, '')},
            )


@admin.register(models.CourseVisibility)
class CourseVisibilityAdmin(admin.ModelAdmin):
    change_list_template = 'admin/courses/coursevisibility/change_list.html'

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        if request.method == 'POST':
            all_courses = _fetch_thinkific_courses()
            visible_ids = set(int(x) for x in request.POST.getlist('visible'))
            for cid, name in all_courses:
                models.CourseVisibility.objects.update_or_create(
                    course_id=cid,
                    defaults={'course_name_cache': name, 'is_visible': cid in visible_ids},
                )
            self.message_user(request, f"Visibilité mise à jour pour {len(all_courses)} cours.")
            return HttpResponseRedirect(request.path)

        all_courses = _fetch_thinkific_courses()
        visibility_map = {
            v.course_id: v.is_visible
            for v in models.CourseVisibility.objects.all()
        }
        courses_with_state = [
            {'id': cid, 'name': name, 'visible': visibility_map.get(cid, True)}
            for cid, name in all_courses
        ]
        extra = extra_context or {}
        extra['courses_with_state'] = courses_with_state
        extra['title'] = 'Visibilité des cours'
        return super().changelist_view(request, extra_context=extra)
