from allauth.account.signals import email_confirmed
from allauth.socialaccount.signals import social_account_added, social_account_updated
from django.dispatch import receiver

from .adapters import _get_or_create_thinkific_account, _generate_password


def _ensure_thinkific_linked(user):
    """
    Si le user n'a pas encore de thinkific_user_id,
    on cherche/crée son compte Thinkific et on l'enregistre.
    """
    if user.thinkific_user_id:
        return  # Déjà lié

    raw_password = _generate_password(user.email)
    thinkific_user_id = _get_or_create_thinkific_account(user, raw_password)

    if thinkific_user_id:
        user.thinkific_user_id = thinkific_user_id
        user.save(update_fields=['thinkific_user_id'])
        print(f"[Signal] thinkific_user_id={thinkific_user_id} lié à {user.email}")
    else:
        print(f"[Signal] thinkific_user_id non obtenu pour {user.email}")


@receiver(email_confirmed)
def on_email_confirmed(sender, request, email_address, **kwargs):
    """
    Déclenché quand l'utilisateur clique sur le lien de vérification email.
    C'est ici que le compte Thinkific est créé pour les inscriptions classiques.
    """
    _ensure_thinkific_linked(email_address.user)


@receiver(social_account_added)
def on_social_account_added(sender, request, sociallogin, **kwargs):
    """
    Déclenché quand un compte social est connecté à un user Django
    (nouvel utilisateur OU utilisateur existant qui lie Google pour la 1ère fois).
    Pour les nouveaux users : save_user() a déjà tenté de lier Thinkific.
    Ce signal sert de filet de sécurité si ça a échoué, et couvre les users existants.
    """
    _ensure_thinkific_linked(sociallogin.user)


@receiver(social_account_updated)
def on_social_account_updated(sender, request, sociallogin, **kwargs):
    """
    Déclenché à chaque sign in Google d'un user qui a déjà son compte Google lié.
    S'assure que thinkific_user_id est bien présent.
    """
    _ensure_thinkific_linked(sociallogin.user)
