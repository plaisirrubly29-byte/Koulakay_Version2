from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.conf import settings
from django.utils.translation import gettext_lazy as _
from django.http import Http404
from datetime import datetime, timezone
from decimal import Decimal
from django.db.models import Count
import requests

from .monkey_patch.patch_thinkific import ThinkificExtend
from .models import Enrollment, CourseTranslation
from payment.models import Transaction
from payment.exchange_service import convert_to_htg
from pages.models import SiteConfig

# Configuration Thinkific
thinkific = ThinkificExtend(settings.THINKIFIC['AUTH_TOKEN'], settings.THINKIFIC['SITE_ID'])


def apply_course_translations(courses, lang=None):
    """
    Remplace name/description des cours Thinkific par les traductions locales
    enregistrées dans CourseTranslation pour la langue active.
    Ne fait rien si lang == 'fr' (langue source).
    courses : dict unique ou liste de dicts.
    """
    from django.utils.translation import get_language
    lang = lang or get_language() or 'fr'
    if not lang or lang.startswith('fr'):
        return

    if isinstance(courses, dict):
        course_list = [courses]
    else:
        course_list = list(courses)

    if not course_list:
        return

    ids = [c['id'] for c in course_list if c.get('id')]
    translations = {
        t.course_id: t
        for t in CourseTranslation.objects.filter(course_id__in=ids, language=lang)
    }

    for c in course_list:
        t = translations.get(c.get('id'))
        if t:
            if t.name:
                c['name'] = t.name
            if t.description:
                c['description'] = t.description


@login_required
def course_enrollment_step1(request, course_id):
    """Étape 1: Afficher les options de paiement ou inscription directe"""
    if request.method != 'POST':
        return redirect('courses')
    
    try:
        # Récupérer les détails du cours
        course = thinkific.courses.retrieve_course(id=course_id)
        course_name = course.get('name', f'Course ID {course_id}')
        
        # Récupérer le prix et product_id
        course_price = Decimal('0.00')
        product_id = None
        product_response = thinkific.products.list(limit=100)
        product_items = product_response.get('items', [])

        for p in product_items:
            if p.get('productable_id') == course_id:
                if p.get('price') is not None:
                    course_price = Decimal(str(p['price']))
                product_id = p.get('id')
                break
        
        # Trouver l'ID Thinkific de l'utilisateur (champ local en priorité)
        thinkific_user_id = request.user.thinkific_user_id
        if not thinkific_user_id:
            # Fallback : chercher par email dans Thinkific
            from accounts.views import get_thinkific_user_by_email
            thinkific_user = get_thinkific_user_by_email(request.user.email)
            if thinkific_user:
                thinkific_user_id = thinkific_user.get('id')
                request.user.thinkific_user_id = thinkific_user_id
                request.user.save(update_fields=['thinkific_user_id'])

        if not thinkific_user_id:
            messages.error(request, _("Impossible de trouver votre profil Thinkific."))
            return redirect('course_details', course_id=course_id)
        
        # Vérifier si déjà inscrit
        if Enrollment.objects.filter(user=request.user, course_id=course_id).exists():
            messages.info(request, _("Vous êtes déjà inscrit à ce cours."))
            return redirect('course_details', course_id=course_id)
        
        # Si gratuit, inscription directe
        if course_price == 0:
            return enroll_user_free(request, course_id, thinkific_user_id, course_name)
        
        # Si payant, afficher les options de paiement
        request.session['enrollment_data'] = {
            'course_id': course_id,
            'course_name': course_name,
            'course_price': float(course_price),
            'product_id': product_id,
            'thinkific_user_id': thinkific_user_id
        }
        
        site_currency = SiteConfig.get().currency
        htg_equivalent = None
        if site_currency != 'HTG':
            htg_equivalent = convert_to_htg(course_price, site_currency)

        apply_course_translations(course)
        return render(request, 'pages/payment_options.html', {
            'course': course,
            'course_price': course_price,
            'site_currency': site_currency,
            'htg_equivalent': htg_equivalent,
            'stripe_public_key': settings.STRIPE['PUBLIC_KEY'],
        })
        
    except Exception as e:
        messages.error(request, _("Une erreur est survenue."))
        print(f"Erreur enrollment step1: {e}")
        return redirect('course_details', course_id=course_id)


@login_required
def course_enrollment_payment(request, payment_method):
    """Étape 2: Traiter le paiement via plopplop (MonCash / NatCash / Kashpaw)"""
    if request.method != 'POST':
        return redirect('courses')

    enrollment_data = request.session.get('enrollment_data')
    if not enrollment_data:
        messages.error(request, _("Session expirée. Veuillez recommencer."))
        return redirect('courses')

    course_id = enrollment_data['course_id']
    course_name = enrollment_data['course_name']
    course_price = Decimal(str(enrollment_data['course_price']))
    product_id = enrollment_data.get('product_id')
    thinkific_user_id = enrollment_data['thinkific_user_id']

    valid_methods = ['moncash', 'natcash', 'kashpaw', 'credit_card']
    if payment_method not in valid_methods:
        messages.error(request, _("Méthode de paiement invalide."))
        return redirect('course_details', course_id=course_id)

    try:
        from payment.models import Transaction
        from payment.plopplop_service import PlopPlopService
        from payment.exchange_service import convert_to_htg

        # Méthodes Haïtiennes → montant en HTG ; Stripe → USD
        haitian_methods = {'moncash', 'natcash', 'kashpaw'}
        site_currency = SiteConfig.get().currency

        if payment_method in haitian_methods:
            # Convertir le prix en HTG pour PlopPlop
            if site_currency == 'HTG':
                montant_htg = float(course_price)
            else:
                montant_htg = convert_to_htg(course_price, site_currency)
                if montant_htg is None:
                    messages.error(request, _("Impossible d'obtenir le taux de change. Réessayez dans quelques instants."))
                    return redirect('course_details', course_id=course_id)
            tx_currency = Transaction.Currencies.HTG
            tx_price = Decimal(str(montant_htg))
        else:
            # Stripe / Carte bancaire → USD
            if site_currency == 'USD':
                montant_usd = float(course_price)
            else:
                from payment.exchange_service import convert_from_htg, get_htg_rate
                # course_price est dans site_currency, on veut USD
                # convert site_currency → HTG → USD
                htg_amount = convert_to_htg(course_price, site_currency)
                if htg_amount is None:
                    montant_usd = float(course_price)
                else:
                    usd_rate = get_htg_rate('USD')
                    montant_usd = round(htg_amount / usd_rate, 2) if usd_rate else float(course_price)
            tx_currency = Transaction.Currencies.USD
            tx_price = Decimal(str(montant_usd))
            montant_htg = montant_usd  # sera ignoré (pas PlopPlop)

        # Créer la transaction locale (PENDING)
        transaction = Transaction.objects.create(
            user=request.user,
            price=tx_price,
            currency=tx_currency,
            status=Transaction.Status.PENDING,
            payment_method=payment_method,
            meta_data={
                "course": {
                    "course_id": course_id,
                    "course_name": course_name,
                    "product_id": product_id,
                },
                "user": {
                    "id": request.user.pk,
                    "email": request.user.email,
                    "thinkific_user_id": thinkific_user_id,
                },
            }
        )

        if payment_method not in haitian_methods:
            # Carte bancaire → Stripe Elements
            request.session['stripe_transaction_number'] = transaction.transaction_number
            del request.session['enrollment_data']
            return redirect('payment:stripe_checkout')

        # Appel à plopplop (méthodes haïtiennes uniquement)
        plopplop = PlopPlopService()
        result = plopplop.create_payment(
            refference_id=transaction.transaction_number,
            montant=float(tx_price),
            payment_method=payment_method,
        )

        if result['success']:
            # Sauvegarder l'ID plopplop dans la transaction
            transaction.external_transaction_id = result.get('transaction_id')
            transaction.save()

            # Nettoyer la session
            del request.session['enrollment_data']

            # Rediriger l'utilisateur vers la page de paiement plopplop
            return redirect(result['url'])
        else:
            transaction.status = Transaction.Status.FAILED
            transaction.save()
            messages.error(request, _("Impossible de créer le paiement : ") + result.get('error', ''))
            return redirect('course_details', course_id=course_id)

    except Exception as e:
        messages.error(request, _("Erreur lors du traitement du paiement."))
        print(f"Erreur payment plopplop: {e}")
        return redirect('course_details', course_id=course_id)


def enroll_user_free(request, course_id, thinkific_user_id, course_name):
    """Inscrit un utilisateur à un cours gratuit"""
    try:
        from django.utils import timezone as dj_timezone
        
        activated_at = dj_timezone.now()
        local_expiry = activated_at.replace(year=activated_at.year + 10)

        enrollment_data = {
            "course_id": course_id,
            "user_id": thinkific_user_id,
            "activated_at": activated_at.isoformat(),
        }

        enrollment_result = thinkific.enrollments.create_enrollment(enrollment_data)

        if enrollment_result:
            # Créer l'entrée locale (get_or_create évite les doublons)
            Enrollment.objects.get_or_create(
                user=request.user,
                thinkific_user_id=thinkific_user_id,
                course_id=course_id,
                defaults={
                    'activated_at': activated_at,
                    'expiry_date': local_expiry,
                },
            )
            
            messages.success(request, f"Vous êtes inscrit au cours {course_name}!")
            return redirect('course_details', course_id=course_id)
        else:
            messages.error(request, _("Erreur lors de l'inscription."))
            return redirect('course_details', course_id=course_id)
            
    except Exception as e:
        messages.error(request, _("Erreur lors de l'inscription."))
        print(f"Erreur enroll free: {e}")
        return redirect('course_details', course_id=course_id)


@login_required
def mon_apprentissage(request):
    """
    Dashboard 'Mon apprentissage'.
    Source de vérité : API Thinkific (enrollments.list par user_id).
    Fallback : DB locale si thinkific_user_id absent ou API indisponible.
    """
    thinkific_user_id = request.user.thinkific_user_id
    site_id = settings.THINKIFIC['SITE_ID']
    cours_inscrits = []

    def _parse_date(val):
        """Convertit une string ISO Thinkific en datetime aware, ou retourne la valeur telle quelle."""
        if not val or not isinstance(val, str):
            return val
        try:
            return datetime.fromisoformat(val.replace('Z', '+00:00'))
        except (ValueError, AttributeError):
            return val

    # ── 1 appel : tous les produits pour construire la map prix ──
    price_map = {}  # course_id → price (float)
    try:
        product_items = thinkific.products.list(limit=100).get('items', [])
        for p in product_items:
            cid = p.get('productable_id')
            if cid and p.get('price') is not None:
                price_map[cid] = float(p['price'])
    except Exception:
        pass

    # ── Tentative : 1 seul appel API Thinkific (enrollments) ──
    if thinkific_user_id:
        try:
            response = thinkific.enrollments.list(user_id=thinkific_user_id, limit=100)
            for item in response.get('items', []):
                course_info = item.get('course') or {}
                course_id   = item.get('course_id') or course_info.get('id')
                slug        = course_info.get('slug', '')
                cours_inscrits.append({
                    'id':                   course_id,
                    'name':                 course_info.get('name', f'Cours #{course_id}'),
                    'slug':                 slug,
                    'banner_image_url':     None,
                    'description':          '',
                    'price':                price_map.get(course_id),
                    'activated_at':         _parse_date(item.get('activated_at')),
                    'expiry_date':          _parse_date(item.get('expiry_date')),
                    'percentage_completed': item.get('percentage_completed', 0),
                    'thinkific_url': (
                        f"https://{site_id}.thinkific.com/products/courses/{slug}"
                        if slug else '#'
                    ),
                })
        except Exception as e:
            print(f"[mon_apprentissage] Erreur API Thinkific: {e}")

    # ── Enrichissement : récupérer banner_image_url + description pour chaque cours ──
    if cours_inscrits:
        for c in cours_inscrits:
            cid = c.get('id')
            if not cid:
                continue
            try:
                full = thinkific.courses.retrieve_course(id=cid)
                c['banner_image_url'] = (
                    full.get('banner_image_url') or full.get('course_card_image_url')
                )
                c['description'] = full.get('description', '')
                if full.get('name'):
                    c['name'] = full['name']
                if not c['slug']:
                    c['slug'] = full.get('slug', '')
                    if c['slug']:
                        c['thinkific_url'] = f"https://{site_id}.thinkific.com/products/courses/{c['slug']}"
            except Exception:
                pass

    # ── Fallback : DB locale ──
    if not cours_inscrits:
        for enrollment in Enrollment.objects.filter(user=request.user).order_by('-activated_at'):
            slug = ''
            name = f'Cours #{enrollment.course_id}'
            banner = None
            try:
                cd   = thinkific.courses.retrieve_course(id=enrollment.course_id)
                slug = cd.get('slug', '')
                name = cd.get('name', name)
                banner = cd.get('banner_image_url')
            except Exception:
                pass
            cours_inscrits.append({
                'id':                   enrollment.course_id,
                'name':                 name,
                'slug':                 slug,
                'banner_image_url':     banner,
                'price':                price_map.get(enrollment.course_id),
                'activated_at':         enrollment.activated_at,
                'expiry_date':          enrollment.expiry_date,
                'percentage_completed': 0,
                'thinkific_url': (
                    f"https://{site_id}.thinkific.com/products/courses/{slug}"
                    if slug else '#'
                ),
            })

    apply_course_translations(cours_inscrits)
    return render(request, 'pages/mon_apprentissage.html', {
        'cours_inscrits': cours_inscrits,
    })


def payment_callback(request):
    """
    Traite le callback après paiement.
    Redirige vers la page du cours avec un message.
    """
    transaction_id = request.GET.get('transaction_id')
    status = request.GET.get('status', '').lower()
    
    if not transaction_id:
        messages.error(request, _("Transaction introuvable."))
        return redirect('courses')
    
    try:
        transaction = Transaction.objects.get(pk=transaction_id)
        course_id = transaction.course_id
        
        if status in ['success', 'completed'] and transaction.is_completed:
            messages.success(request, _("Paiement réussi ! Vous êtes maintenant inscrit au cours."))
            return redirect('course_details', course_id=course_id)
        elif status in ['failed', 'cancelled']:
            messages.error(request, _("Le paiement a échoué ou a été annulé."))
            return redirect('course_details', course_id=course_id)
        else:
            messages.info(request, _("Votre paiement est en cours de traitement."))
            return redirect('course_details', course_id=course_id)
            
    except Transaction.DoesNotExist:
        messages.error(request, _("Transaction introuvable."))
        return redirect('courses')
    except Exception as e:
        messages.error(request, _("Erreur lors du traitement."))
        print(f"Erreur callback: {e}")
        return redirect('courses')


def home(request):
    """Vue pour la page d'accueil avec statistiques et cours populaires"""
    stats = {
        'total_courses': 0,
        'total_users': 0,
        'local_enrollments': 0,
    }
    
    try:
        # Statistiques via l'API Thinkific
        courses_response = thinkific.courses.list(limit=1)
        stats['total_courses'] = courses_response.get('meta', {}).get('pagination', {}).get('total_items', 0)

        users_response = thinkific.users.list(limit=1)
        stats['total_users'] = users_response.get('meta', {}).get('pagination', {}).get('total_items', 0)
        
        stats['local_enrollments'] = Enrollment.objects.count()
        
    except Exception as e:
        print(f"Erreur lors de la récupération des statistiques: {e}")
    
    # Récupérer les cours populaires (top 6 pour la homepage)
    popular_courses = []
    top_course_ids_queryset = Enrollment.objects.values('course_id') \
                                             .annotate(num_enrollments=Count('course_id')) \
                                             .order_by('-num_enrollments')[:6]

    top_course_ids = [item['course_id'] for item in top_course_ids_queryset]

    # Récupérer les détails des produits
    try:
        product_response = thinkific.products.list(limit=100)
        product_items = product_response.get('items', [])
    except Exception:
        product_items = []

    enrolled_ids = set()
    if request.user.is_authenticated:
        enrolled_ids = set(
            Enrollment.objects.filter(user=request.user).values_list('course_id', flat=True)
        )

    if top_course_ids:
        for course_id in top_course_ids:
            try:
                course_data = thinkific.courses.retrieve_course(id=course_id)
                course_data['enrollment_count'] = next(
                    (item['num_enrollments'] for item in top_course_ids_queryset if item['course_id'] == course_id), 0
                )
                course_data['price'] = next(
                    (p['price'] for p in product_items
                     if p.get('productable_id') == course_id and p.get('price') is not None), None
                )
                course_data['enroll'] = course_id in enrolled_ids
                popular_courses.append(course_data)
            except Exception as e:
                print(f"Erreur cours populaire {course_id}: {e}")
                continue
    else:
        try:
            for course_data in thinkific.courses.list(limit=6).get('items', []):
                course_id = course_data.get('id')
                course_data['enrollment_count'] = 0
                course_data['price'] = next(
                    (p['price'] for p in product_items
                     if p.get('productable_id') == course_id and p.get('price') is not None), None
                )
                course_data['enroll'] = course_id in enrolled_ids
                popular_courses.append(course_data)
        except Exception as e:
            print(f"Erreur cours fallback: {e}")
    
    return render(request, 'pages/home.html', {
        'stats': stats,
        'courses': popular_courses  # Changé de 'popular_courses' à 'courses' pour cohérence
    })


def courses(request):
    """Liste des cours — tous chargés en une passe, filtrage côté client."""
    try:
        courses_items = thinkific.courses.list(limit=100).get('items', [])
    except Exception:
        courses_items = []

    try:
        product_items = thinkific.products.list(limit=100).get('items', [])
    except Exception:
        product_items = []

    enrolled_ids = set()
    if request.user.is_authenticated:
        enrolled_ids = set(
            Enrollment.objects.filter(user=request.user).values_list('course_id', flat=True)
        )

    popular_counts = {
        item['course_id']: item['num']
        for item in Enrollment.objects.values('course_id')
                                      .annotate(num=Count('course_id'))
                                      .order_by('-num')[:10]
    }

    price_map = {
        p['productable_id']: p['price']
        for p in product_items
        if p.get('productable_id') and p.get('price') is not None
    }

    for c in courses_items:
        cid = c.get('id')
        price = price_map.get(cid)
        c['price'] = price
        c['is_free'] = price is None or float(price) == 0
        c['enroll'] = cid in enrolled_ids
        c['enrollment_count'] = popular_counts.get(cid, 0)

    apply_course_translations(courses_items)

    context = {
        'courses': courses_items,
        'site_currency': SiteConfig.get().currency,
    }
    
    return render(request, 'pages/courses.html', context)


def course_details(request, course_id):
    """Détails d'un cours avec contenu et instructeur"""
    try:
        course = thinkific.courses.retrieve_course(id=course_id)
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404:
            raise Http404("Le cours demandé n'existe pas.")
        raise

    # Récupérer le prix
    course['price'] = None
    try:
        product_response = thinkific.products.list(limit=100)
        product_items = product_response.get('items', [])
        product_id = course.get('product_id')
        
        if product_id is not None:
            for p in product_items:
                if p.get('productable_id') == course_id and p.get('price') is not None:
                    course['price'] = p['price']
                    break
    except Exception as e:
        print(f"Erreur récupération prix: {e}")

    # Vérifier l'inscription
    course['enroll'] = False
    if request.user.is_authenticated:
        course['enroll'] = Enrollment.objects.filter(
            user=request.user,
            course_id=course_id
        ).exists()

    # Contenu du cours
    course_content = []
    try:
        api_url = f"https://api.thinkific.com/api/v2/courses/{course_id}/content"
        headers = {
            "Authorization": f"Bearer {settings.THINKIFIC['AUTH_TOKEN']}",
            "X-Auth-Subdomain": settings.THINKIFIC['SITE_ID'],
            "Content-Type": "application/json"
        }
        
        content_response = requests.get(api_url, headers=headers)
        content_response.raise_for_status()
        course_content = content_response.json().get('items', [])
        
    except Exception as e:
        print(f"Erreur contenu du cours: {e}")
        course_content = []

    # Instructeur
    instructor_id = course.get('instructor_id')
    instructor = None

    if instructor_id:
        try:
            instructor = thinkific.instructors.retrieve_instructor(id=instructor_id)
        except requests.exceptions.HTTPError:
            instructor = {'first_name': 'Instructeur', 'last_name': 'Inconnu', 'bio': ''}
    else:
        instructor = {'first_name': 'Instructeur', 'last_name': 'Non Spécifié', 'bio': ''}
    
    apply_course_translations(course)
    return render(request, 'pages/course_details.html', {
        'course': course,
        'instructor': instructor,
        'course_content': course_content,
    })