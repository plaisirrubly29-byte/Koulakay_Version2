#payment\models.py
from django.db import models
from django.utils.translation import gettext_lazy as _
from django.contrib.auth import get_user_model
from courses.models import Enrollment

User = get_user_model()


def transaction_number_generator(sender, instance, **kwargs):
    """
    Génère un numéro de transaction unique au format KL-YYYYMMDD-XXXXXXXX
    Ex: KL-20260311-A3F7K2B8

    Basé sur la date + 8 caractères UUID aléatoires → unique même après
    reset de base de données ou redéploiement.
    """
    if instance.transaction_number is not None:
        return

    import uuid
    from datetime import date

    def make_ref():
        date_part = date.today().strftime('%Y%m%d')
        rand_part = uuid.uuid4().hex[:8].upper()
        return f"KL-{date_part}-{rand_part}"

    ref = make_ref()
    # Collision extrêmement improbable, mais on vérifie quand même
    while Transaction.objects.filter(transaction_number=ref).exists():
        ref = make_ref()

    instance.transaction_number = ref


class Transaction(models.Model):
    """Modèle de transaction pour les paiements de cours"""
    
    class Status(models.TextChoices):
        PENDING = 'PENDING', _('En attente')
        COMPLETED = 'COMPLETED', _('Complétée')
        FAILED = 'FAILED', _('Échouée')
        CANCELLED = 'CANCELLED', _('Annulée')
        REFUNDED = 'REFUNDED', _('Remboursée')
    
    class Currencies(models.TextChoices):
        USD = 'USD', _('Dollar américain')
        HTG = 'HTG', _('Gourde haïtienne')
        EUR = 'EUR', _('Euro')
        GBP = 'GBP', _('Livre sterling')
    
    class PaymentMethods(models.TextChoices):
        CREDIT_CARD = 'credit_card', _('Carte de crédit')
        MONCASH = 'moncash', _('MonCash')
        NATCASH = 'natcash', _('NatCash')
        KASHPAW = 'kashpaw', _('Kashpaw')
        OTHER = 'other', _('Autre')
    
    # Identifiants
    transaction_number = models.CharField(
        _('numéro de transaction'),
        max_length=255,
        unique=True,
        null=True,
        blank=True,
        help_text='Ex: KOULKY000001'
    )
    
    # Utilisateur (optionnel pour invités)
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='transactions',
        null=True,
        blank=True,
        verbose_name=_('utilisateur')
    )
    
    # Montant et devise
    price = models.DecimalField(
        _('prix'),
        max_digits=10,
        decimal_places=2,
        help_text=_('Ex: 1000.00')
    )
    
    currency = models.CharField(
        _('devise'),
        max_length=3,
        choices=Currencies.choices,
        default=Currencies.USD
    )
    
    # Statut et méthode
    status = models.CharField(
        _('statut'),
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING
    )
    
    payment_method = models.CharField(
        _('méthode de paiement'),
        max_length=20,
        choices=PaymentMethods.choices,
        default=PaymentMethods.CREDIT_CARD
    )
    
    # Référence externe (ID de transaction du provider)
    external_transaction_id = models.CharField(
        _('ID transaction externe'),
        max_length=255,
        blank=True,
        null=True,
        help_text='ID de transaction du fournisseur de paiement'
    )
    
    # ID de commande externe Thinkific
    thinkific_external_order_id = models.IntegerField(
        _('ID commande externe Thinkific'),
        null=True,
        blank=True,
        help_text='ID de External Order créé dans Thinkific'
    )
    
    # Métadonnées
    meta_data = models.JSONField(
        _('métadonnées'),
        default=dict,
        blank=True,
        help_text='Informations supplémentaires (cours, utilisateur, etc.)'
    )
    
    # Dates
    created_at = models.DateTimeField(
        _('date de création'),
        auto_now_add=True
    )
    
    updated_at = models.DateTimeField(
        _('date de modification'),
        auto_now=True
    )
    
    completed_at = models.DateTimeField(
        _('date de complétion'),
        null=True,
        blank=True
    )
    
    class Meta:
        verbose_name = _('Transaction')
        verbose_name_plural = _('Transactions')
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['-created_at']),
            models.Index(fields=['status']),
            models.Index(fields=['transaction_number']),
            models.Index(fields=['external_transaction_id']),
            models.Index(fields=['user']),
        ]
    
    def __str__(self):
        return f"{self.transaction_number} - {self.get_status_display()} - {self.price} {self.currency}"
    
    @property
    def is_completed(self):
        """Vérifie si la transaction est complétée"""
        return self.status == self.Status.COMPLETED
    
    @property
    def is_pending(self):
        """Vérifie si la transaction est en attente"""
        return self.status == self.Status.PENDING
    
    @property
    def is_refundable(self):
        """Vérifie si la transaction peut être remboursée"""
        return self.status == self.Status.COMPLETED
    
    @property
    def course_name(self):
        """Retourne le nom du cours ou du bundle depuis les métadonnées"""
        bundle = self.meta_data.get('bundle', {})
        if bundle:
            return bundle.get('bundle_name', f'Bundle #{bundle.get("bundle_id")}')
        return self.meta_data.get('course', {}).get('course_name', 'N/A')
    
    @property
    def course_id(self):
        """Retourne l'ID du cours depuis les métadonnées"""
        return self.meta_data.get('course', {}).get('course_id')
    
    @property
    def user_email(self):
        """Retourne l'email de l'utilisateur"""
        if self.user:
            return self.user.email
        return self.meta_data.get('user', {}).get('email', 'N/A')


# Signal pour générer le numéro de transaction
models.signals.pre_save.connect(transaction_number_generator, sender=Transaction)


class Payment(models.Model):
    """Modèle de paiement liant utilisateur, inscription et transaction"""
    
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='payments',
        verbose_name=_('utilisateur')
    )
    
    enrollment = models.ForeignKey(
        Enrollment,
        on_delete=models.CASCADE,
        related_name='payments',
        verbose_name=_('inscription')
    )
    
    transaction = models.OneToOneField(
        Transaction,
        on_delete=models.CASCADE,
        related_name='payment',
        verbose_name=_('transaction')
    )
    
    created_at = models.DateTimeField(
        _('date de création'),
        auto_now_add=True
    )
    
    updated_at = models.DateTimeField(
        _('date de modification'),
        auto_now=True
    )
    
    class Meta:
        verbose_name = _('Paiement')
        verbose_name_plural = _('Paiements')
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['-created_at']),
            models.Index(fields=['user']),
            models.Index(fields=['enrollment']),
        ]
    
    def __str__(self):
        return f"Paiement {self.transaction.transaction_number} - {self.user.email}"



