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
