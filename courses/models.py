from django.db import models
from django.contrib.auth import get_user_model
from django.conf import settings
from django.utils.text import slugify


class Enrollment(models.Model):
    user = models.ForeignKey(get_user_model(),on_delete=models.CASCADE,blank=False)
    thinkific_user_id = models.IntegerField(('thinkific user id'),blank=False)
    course_id = models.IntegerField('Course id',blank=False)
    activated_at = models.DateTimeField(('activated at'),blank=False)
    expiry_date = models.DateTimeField(('expiry date'), blank=False)


LANGUAGE_CHOICES = [(code, name) for code, name in settings.LANGUAGES]


class CourseTranslation(models.Model):
    """Traductions locales pour les cours provenant de Thinkific."""
    course_id = models.IntegerField('Thinkific course ID', db_index=True)
    language = models.CharField('Langue', max_length=5, choices=LANGUAGE_CHOICES)
    name = models.CharField('Nom du cours', max_length=255, blank=True)
    description = models.TextField('Description', blank=True)

    class Meta:
        unique_together = ('course_id', 'language')
        verbose_name = 'Traduction de cours'
        verbose_name_plural = 'Traductions de cours'

    def __str__(self):
        return f'Cours {self.course_id} [{self.language}]'


class CourseCategory(models.Model):
    name = models.CharField('Nom', max_length=100)
    slug = models.SlugField('Slug', unique=True, blank=True)
    icon = models.CharField(
        'Icône FA', max_length=50, blank=True, default='fa-graduation-cap',
        help_text='Classe Font Awesome, ex: fa-laptop-code'
    )
    color = models.CharField(
        'Couleur', max_length=20, blank=True, default='#6366F1',
        help_text="Couleur hex, ex: #6366F1 — utilisee si aucune image n'est definie"
    )
    image = models.ImageField(
        'Image', upload_to='categories/', blank=True,
        help_text="Image d'en-tete de la carte (recommande : 400x220 px)"
    )
    description = models.CharField(
        'Description courte', max_length=200, blank=True,
        help_text="Affichee sous le nom sur la page d'accueil"
    )
    order = models.PositiveSmallIntegerField('Ordre', default=0)
    is_active = models.BooleanField('Actif', default=True)

    class Meta:
        verbose_name = 'Catégorie'
        verbose_name_plural = 'Catégories'
        ordering = ['order', 'name']

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)


class CourseVisibility(models.Model):
    """Contrôle quels cours Thinkific sont affichés sur le site."""
    course_id = models.IntegerField('ID Thinkific', unique=True)
    course_name_cache = models.CharField('Nom du cours', max_length=255, blank=True)
    is_visible = models.BooleanField('Visible sur le site', default=True)

    class Meta:
        verbose_name = 'Visibilité de cours'
        verbose_name_plural = 'Visibilité des cours'
        ordering = ['course_name_cache']

    def __str__(self):
        state = 'visible' if self.is_visible else 'masqué'
        return f"{self.course_name_cache or self.course_id} ({state})"


class CourseCategoryMembership(models.Model):
    category = models.ForeignKey(
        CourseCategory, on_delete=models.CASCADE,
        related_name='memberships', verbose_name='Catégorie'
    )
    course_id = models.IntegerField('ID cours Thinkific')
    course_name_cache = models.CharField('Nom (cache)', max_length=255, blank=True)

    class Meta:
        verbose_name = 'Cours de la catégorie'
        verbose_name_plural = 'Cours de la catégorie'
        unique_together = ('category', 'course_id')

    def __str__(self):
        return self.course_name_cache or str(self.course_id)


class CourseGroup(models.Model):
    """Regroupement éditorial de cours — affiché sous forme de cartes sur la page catalogue."""
    name = models.CharField('Nom', max_length=200)
    description = models.TextField('Description', blank=True)
    image = models.ImageField(
        'Image', upload_to='course_groups/', blank=True, null=True,
        help_text='Optionnel — fond de la carte (recommandé : 800×400 px)'
    )
    category = models.ForeignKey(
        'CourseCategory', on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='course_groups',
        verbose_name='Catégorie parente',
        help_text='Catégorie à laquelle appartient ce groupe — filtre automatique sur la page cours'
    )
    order = models.PositiveSmallIntegerField('Ordre', default=0)
    is_active = models.BooleanField('Actif', default=True)

    class Meta:
        verbose_name = 'Groupe de cours'
        verbose_name_plural = 'Groupes de cours'
        ordering = ['order', 'name']

    def __str__(self):
        return self.name


class CourseGroupMembership(models.Model):
    group = models.ForeignKey(
        CourseGroup, on_delete=models.CASCADE,
        related_name='memberships', verbose_name='Groupe'
    )
    course_id = models.IntegerField('ID cours Thinkific')
    course_name_cache = models.CharField('Nom (cache)', max_length=255, blank=True)

    class Meta:
        verbose_name = 'Cours du groupe'
        verbose_name_plural = 'Cours du groupe'
        unique_together = ('group', 'course_id')

    def __str__(self):
        return self.course_name_cache or str(self.course_id)


class BundleCategoryMembership(models.Model):
    category = models.ForeignKey(
        CourseCategory, on_delete=models.CASCADE,
        related_name='bundle_memberships', verbose_name='Catégorie'
    )
    bundle_id = models.IntegerField('ID bundle Thinkific')
    bundle_name_cache = models.CharField('Nom (cache)', max_length=255, blank=True)

    class Meta:
        verbose_name = 'Bundle de la catégorie'
        verbose_name_plural = 'Bundles de la catégorie'
        unique_together = ('category', 'bundle_id')

    def __str__(self):
        return self.bundle_name_cache or str(self.bundle_id)
