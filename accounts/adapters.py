import random
import string

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.contrib.sites.shortcuts import get_current_site
from django.utils.translation import gettext_lazy as _

from allauth.socialaccount.adapter import DefaultSocialAccountAdapter

import requests
from courses.monkey_patch.patch_thinkific import ThinkificExtend as Thinkific


def _generate_password(email: str) -> str:
    """
    Génère un mot de passe à partir de la partie locale de l'email (avant @)
    suivie de 5 caractères alphanumériques aléatoires.
    Ex: jean.pierre@gmail.com → jean.pierre_aB3xK
    """
    prefix = email.split('@')[0]
    suffix = ''.join(random.choices(string.ascii_letters + string.digits, k=5))
    return f"{prefix}_{suffix}"


def _get_or_create_thinkific_account(user, raw_password: str):
    """
    Même logique que l'admin :
    1. Cherche d'abord le user dans Thinkific par email
    2. Si trouvé → retourne son ID (sans rien modifier)
    3. Si absent → crée le compte → retourne l'ID
    Retourne None en cas d'échec.
    """
    tk = Thinkific(settings.THINKIFIC['AUTH_TOKEN'], settings.THINKIFIC['SITE_ID'])

    try:
        # 1. Chercher par email dans Thinkific
        result = tk.users.list(email=user.email)
        for u in result.get('items', []):
            if u.get('email', '').lower() == user.email.lower():
                return u.get('id')  # trouvé → on retourne l'ID directement
    except Exception as e:
        print(f"[Thinkific] Erreur recherche par email ({user.email}): {e}")
        return None

    try:
        # 2. Pas trouvé → créer le compte
        thinkific_user = tk.users.create_user({
            'email': user.email,
            'first_name': user.first_name or '',
            'last_name': user.last_name or '',
            'password': raw_password,
            'send_welcome_email': False,
        })
        return thinkific_user.get('id') if thinkific_user else None
    except Exception as e:
        print(f"[Thinkific] Erreur création compte ({user.email}): {e}")
        return None


def _send_credentials_email(request, user, raw_password: str):
    """
    Envoie par email les credentials générés automatiquement au user Google.
    """
    try:
        current_site = get_current_site(request) if request else None
        site_name = current_site.name if current_site else "KouLakay"
        site_domain = current_site.domain if current_site else "koulakay.ht"

        context = {
            'user': user,
            'raw_password': raw_password,
            'site_name': site_name,
            'site_domain': site_domain,
            'login_url': f"https://{site_domain}/accounts/login/",
        }

        subject = render_to_string(
            'account/email/google_signup_credentials_subject.txt',
            context
        ).strip()

        body_html = render_to_string(
            'account/email/google_signup_credentials_message.html',
            context
        )

        msg = EmailMultiAlternatives(
            subject=subject,
            body=body_html,  # fallback texte brut
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[user.email],
        )
        msg.attach_alternative(body_html, "text/html")
        msg.send()

    except Exception as e:
        print(f"[Google OAuth] Erreur envoi email credentials: {e}")


class CustomSocialAccountAdapter(DefaultSocialAccountAdapter):
    """
    Adapter allauth pour les comptes sociaux (Google, etc.).

    save_user()        → NOUVEL utilisateur : génère password, crée Thinkific, envoie email credentials
    pre_social_login() → UTILISATEUR EXISTANT qui se connecte : s'assure que thinkific_user_id
                         est bien en Django (cherche dans Thinkific, crée si absent)
    """

    def save_user(self, request, sociallogin, form=None):
        user = super().save_user(request, sociallogin, form)

        # Générer et sauvegarder le mot de passe
        raw_password = _generate_password(user.email)
        user.set_password(raw_password)
        user.save(update_fields=['password'])

        # Chercher ou créer le compte Thinkific
        thinkific_user_id = _get_or_create_thinkific_account(user, raw_password)
        if thinkific_user_id:
            user.thinkific_user_id = thinkific_user_id
            user.save(update_fields=['thinkific_user_id'])
        else:
            print(f"[Google OAuth] thinkific_user_id non obtenu pour {user.email}")

        # Envoyer les credentials par email
        _send_credentials_email(request, user, raw_password)

        return user
