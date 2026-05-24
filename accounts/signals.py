from allauth.account.signals import email_confirmed
from allauth.socialaccount.signals import social_account_added, social_account_updated
from django.dispatch import receiver
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.urls import reverse
from django.conf import settings

from .adapters import _get_or_create_thinkific_account, _generate_password


def _send_welcome_email(user, request):
    """Envoie un email de bienvenue avec les infos de compte après confirmation."""
    try:
        base = request.build_absolute_uri('/').rstrip('/')
        lang = getattr(request, 'LANGUAGE_CODE', settings.LANGUAGE_CODE)

        context = {
            'user': user,
            'courses_url': f"{base}/{lang}/courses/courses/",
            'reset_url':   f"{base}/{lang}/accounts/password/reset/",
        }

        subject = render_to_string('account/email/welcome_subject.txt').strip()
        html    = render_to_string('account/email/welcome_message.html', context)
        txt     = (
            f"Bonjour {user.first_name},\n\n"
            f"Votre compte KouLakay est activé.\n"
            f"E-mail : {user.email}\n"
            f"Mot de passe : celui que vous avez choisi à l'inscription.\n\n"
            f"Modifier mon mot de passe : {context['reset_url']}\n"
            f"Découvrir les cours : {context['courses_url']}\n"
        )

        msg = EmailMultiAlternatives(
            subject=subject,
            body=txt,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[user.email],
        )
        msg.attach_alternative(html, "text/html")
        msg.send()
    except Exception as e:
        print(f"[Signal] Erreur envoi email de bienvenue pour {user.email}: {e}")


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
    Crée le compte Thinkific puis envoie l'email de bienvenue avec les credentials.
    """
    user = email_address.user
    _ensure_thinkific_linked(user)
    _send_welcome_email(user, request)


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
