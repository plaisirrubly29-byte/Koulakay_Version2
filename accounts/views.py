from django.shortcuts import render, redirect
from django.contrib.auth import login as auth_login
from django.contrib import messages
from django.conf import settings
from allauth.account.views import SignupView, LoginView
from courses.monkey_patch.patch_thinkific import ThinkificExtend as Thinkific
from django.contrib.auth.decorators import login_required
from django.views import View
from django.utils.translation import gettext_lazy as _
import requests

thinkific = Thinkific(settings.THINKIFIC['AUTH_TOKEN'], settings.THINKIFIC['SITE_ID'])


class ThinkificSignupView(SignupView):
    """
    Vue d'inscription — crée uniquement le compte Django local.
    La synchronisation Thinkific se fait dans accounts/signals.py
    via le signal email_confirmed, après vérification de l'adresse email.
    """

    def form_valid(self, form):
        return super().form_valid(form)


class ThinkificLoginView(LoginView):
    """
    Vue de connexion — délègue entièrement à allauth.
    Allauth vérifie email + password localement via AUTHENTICATION_BACKENDS,
    gère la session et la redirection. Pas besoin de vérifier Thinkific ici :
    Thinkific ne peut pas valider un mot de passe via son API.
    """

    def get_success_url(self):
        # Allauth gère déjà le ?next= correctement (GET + POST hidden field)
        return super().get_success_url()


# Vue alternative pour inscription directe sans allauth
class DirectThinkificSignupView(View):
    """Vue d'inscription directe sans utiliser django-allauth"""
    
    def get(self, request):
        return render(request, 'account/signup_direct.html')
    
    def post(self, request):
        # Récupérer les données du formulaire
        email = request.POST.get('email')
        first_name = request.POST.get('first_name')
        last_name = request.POST.get('last_name')
        password = request.POST.get('password1')
        password_confirm = request.POST.get('password2')
        
        # Validation basique
        if not all([email, first_name, last_name, password, password_confirm]):
            messages.error(request, _("Tous les champs sont requis."))
            return render(request, 'account/signup_direct.html')
        
        if password != password_confirm:
            messages.error(request, _("Les mots de passe ne correspondent pas."))
            return render(request, 'account/signup_direct.html')
        
        try:
            # Créer l'utilisateur dans Thinkific
            thinkific_user_data = {
                'email': email,
                'first_name': first_name,
                'last_name': last_name,
                'password': password,
                'send_welcome_email': True
            }
            
            thinkific_user = thinkific.users.create_user(thinkific_user_data)
            
            if not thinkific_user:
                messages.error(request, _("Erreur lors de la création du compte Thinkific."))
                return render(request, 'account/signup_direct.html')
            
            # Créer l'utilisateur local
            from django.contrib.auth import get_user_model
            User = get_user_model()
            
            user = User.objects.create_user(
                email=email,
                password=password,
                first_name=first_name,
                last_name=last_name
            )
            user.thinkific_user_id = thinkific_user.get('id')
            user.save(update_fields=['thinkific_user_id'])

            # Connecter automatiquement
            auth_login(request, user)
            
            messages.success(request, _("Compte créé avec succès !"))
            return redirect('home')
            
        except Exception as e:
            messages.error(request, _("Erreur lors de la création du compte."))
            print(f"Erreur: {e}")
            return render(request, 'account/signup_direct.html')


def get_thinkific_user_by_email(email: str):
    """
    Cherche un utilisateur dans Thinkific par email.
    Retourne le dict utilisateur ou None.
    """
    try:
        result = thinkific.users.list(email=email)
        for u in result.get('items', []):
            if u.get('email', '').lower() == email.lower():
                return u
    except Exception as e:
        print(f"Erreur recherche Thinkific par email ({email}): {e}")
    return None


@login_required
def sync_thinkific_user(request):
    """Synchronise l'utilisateur local avec Thinkific"""
    try:
        thinkific_user = get_thinkific_user_by_email(request.user.email)

        if thinkific_user:
            update_fields = []
            if thinkific_user.get('first_name'):
                request.user.first_name = thinkific_user['first_name']
                update_fields.append('first_name')
            if thinkific_user.get('last_name'):
                request.user.last_name = thinkific_user['last_name']
                update_fields.append('last_name')
            if thinkific_user.get('id') and not request.user.thinkific_user_id:
                request.user.thinkific_user_id = thinkific_user['id']
                update_fields.append('thinkific_user_id')
            if update_fields:
                request.user.save(update_fields=update_fields)

            messages.success(request, _("Profil synchronisé avec Thinkific."))
        else:
            messages.warning(request, _("Utilisateur non trouvé dans Thinkific."))

    except Exception as e:
        messages.error(request, _("Erreur lors de la synchronisation."))
        print(f"Erreur sync: {e}")

    return redirect('account_profile')


@login_required
def thinkific_sso(request):
    """
    SSO JWT vers Thinkific — connecte l'utilisateur sans re-login.
    Génère un JWT signé avec THINKIFIC_SSO_SECRET et redirige vers
    https://{site}.thinkific.com/api/sso/v2/sso/jwt?jwt={token}&return_to={path}
    """
    import jwt as pyjwt
    import time
    from urllib.parse import urlencode

    user = request.user
    site_id = settings.THINKIFIC['SITE_ID']
    sso_secret = settings.THINKIFIC.get('SSO_SECRET', '')
    return_to = request.GET.get('return_to', '/enrollments')
    fallback_url = f"https://{site_id}.thinkific.com{return_to}"

    if not user.thinkific_user_id:
        messages.error(request, _("Votre compte n'est pas lié à Thinkific."))
        return redirect('home')

    if not sso_secret:
        print("[SSO Thinkific] THINKIFIC_SSO_SECRET absent — fallback sans SSO")
        return redirect(fallback_url)

    try:
        payload = {
            'email': user.email,
            'first_name': user.first_name or '',
            'last_name': user.last_name or '',
            'iat': int(time.time()),
        }
        token = pyjwt.encode(payload, sso_secret, algorithm='HS256')
        params = {'jwt': token, 'return_to': return_to}
        sso_url = f"https://{site_id}.thinkific.com/api/sso/v2/sso/jwt?{urlencode(params)}"
        return redirect(sso_url)

    except Exception as e:
        print(f"[SSO Thinkific] Erreur génération JWT user={user.email}: {e}")
        return redirect(fallback_url)