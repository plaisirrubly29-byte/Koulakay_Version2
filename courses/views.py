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
from .models import Enrollment, CourseTranslation, CourseCategory, CourseCategoryMembership, BundleCategoryMembership, CourseVisibility, CourseGroup
from payment.models import Transaction
from payment.exchange_service import convert_to_htg
from pages.models import SiteConfig

# Configuration Thinkific
thinkific = ThinkificExtend(settings.THINKIFIC['AUTH_TOKEN'], settings.THINKIFIC['SITE_ID'])


def _format_price(raw):
    """25.0 → '25', 25.50 → '25.50', None → None."""
    if raw is None:
        return None
    f = float(raw)
    return str(int(f)) if f == int(f) else f'{f:.2f}'


def _format_access_duration(days):
    """
    None → '6 mois' (défaut affiché)
    365 → '1 an', 730 → '2 ans'
    180 → '6 mois', 30 → '1 mois'
    14  → '14 jours'
    """
    if days is None:
        return '6 mois'
    days = int(days)
    if days >= 365 and days % 365 == 0:
        y = days // 365
        return f"{y} an{'s' if y > 1 else ''}"
    if days >= 30 and days % 30 == 0:
        m = days // 30
        return f"{m} mois"
    return f"{days} jours"


def _fetch_bundle_details(bundle_id):
    """GET /bundles/{id} → {id, name, course_ids, bundle_card_image_url, slug}"""
    url = f"https://api.thinkific.com/api/public/v1/bundles/{bundle_id}"
    headers = {
        "Authorization": f"Bearer {settings.THINKIFIC['AUTH_TOKEN']}",
        "X-Auth-Subdomain": settings.THINKIFIC['SITE_ID'],
        "Content-Type": "application/json",
    }
    resp = requests.get(url, headers=headers)
    resp.raise_for_status()
    return resp.json()


def _fetch_bundle_courses(bundle_id):
    """GET /bundles/{id}/courses → list of course dicts"""
    url = f"https://api.thinkific.com/api/public/v1/bundles/{bundle_id}/courses"
    headers = {
        "Authorization": f"Bearer {settings.THINKIFIC['AUTH_TOKEN']}",
        "X-Auth-Subdomain": settings.THINKIFIC['SITE_ID'],
        "Content-Type": "application/json",
    }
    resp = requests.get(url, headers=headers)
    resp.raise_for_status()
    return resp.json().get('items', [])


def _sync_user_enrollments(user, request=None):
    """
    Lit les enrollments Thinkific de l'utilisateur et crée les entrées manquantes
    en DB locale. Source de vérité = Thinkific.
    Throttle : 1 sync par heure par session pour limiter les appels API.
    """
    if not getattr(user, 'is_authenticated', False):
        return
    thinkific_user_id = getattr(user, 'thinkific_user_id', None)
    if not thinkific_user_id:
        return

    # Throttle via session : on ne re-sync pas si déjà fait dans la dernière heure
    if request is not None:
        import time
        last_sync = request.session.get('enrollment_sync_ts', 0)
        if time.time() - last_sync < 3600:
            return
        request.session['enrollment_sync_ts'] = int(time.time())

    try:
        from django.utils import timezone as dj_timezone
        response = thinkific.enrollments.list(user_id=thinkific_user_id, limit=100)

        # Auto-correction : si 0 résultat, le thinkific_user_id en DB est peut-être faux.
        # On re-vérifie par email et on corrige.
        if not response.get('items'):
            from accounts.views import get_thinkific_user_by_email
            tk_user = get_thinkific_user_by_email(user.email)
            if tk_user and tk_user.get('id') and tk_user['id'] != thinkific_user_id:
                thinkific_user_id = tk_user['id']
                user.thinkific_user_id = thinkific_user_id
                user.save(update_fields=['thinkific_user_id'])
                response = thinkific.enrollments.list(user_id=thinkific_user_id, limit=100)

        for item in response.get('items', []):
            course_id = item.get('course_id')
            if not course_id:
                continue
            raw_date = item.get('activated_at')
            try:
                activated_at = datetime.fromisoformat(raw_date.replace('Z', '+00:00')) if raw_date else dj_timezone.now()
            except Exception:
                activated_at = dj_timezone.now()
            expiry_date = activated_at.replace(year=activated_at.year + 10)
            Enrollment.objects.get_or_create(
                user=user,
                course_id=course_id,
                defaults={
                    'thinkific_user_id': thinkific_user_id,
                    'activated_at': activated_at,
                    'expiry_date': expiry_date,
                },
            )
    except Exception as e:
        print(f"[sync_enrollments] {user.email}: {e}")


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
        
        # Sync Thinkific → DB locale avant de vérifier le doublon
        _sync_user_enrollments(request.user, request)

        # Vérifier si déjà inscrit (DB locale = vérité après sync)
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

    is_bundle = enrollment_data.get('is_bundle', False)

    if is_bundle:
        bundle_id = enrollment_data['bundle_id']
        bundle_name = enrollment_data['bundle_name']
        course_price = Decimal(str(enrollment_data['bundle_price']))
        bundle_course_ids = enrollment_data['bundle_course_ids']
        product_id = enrollment_data.get('product_id')
        thinkific_user_id = enrollment_data['thinkific_user_id']
        course_id = None
        course_name = bundle_name
    else:
        course_id = enrollment_data['course_id']
        course_name = enrollment_data['course_name']
        course_price = Decimal(str(enrollment_data['course_price']))
        product_id = enrollment_data.get('product_id')
        thinkific_user_id = enrollment_data['thinkific_user_id']
        bundle_id = None
        bundle_course_ids = None

    def _err_redirect():
        return redirect('courses') if is_bundle else redirect('course_details', course_id=course_id)

    valid_methods = ['moncash', 'natcash', 'kashpaw', 'credit_card']
    if payment_method not in valid_methods:
        messages.error(request, _("Méthode de paiement invalide."))
        return _err_redirect()

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
                    return _err_redirect()
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
        if is_bundle:
            tx_meta = {
                "bundle": {
                    "bundle_id": bundle_id,
                    "bundle_name": bundle_name,
                    "bundle_course_ids": bundle_course_ids,
                    "product_id": product_id,
                },
                "user": {
                    "id": request.user.pk,
                    "email": request.user.email,
                    "thinkific_user_id": thinkific_user_id,
                },
            }
        else:
            tx_meta = {
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

        transaction = Transaction.objects.create(
            user=request.user,
            price=tx_price,
            currency=tx_currency,
            status=Transaction.Status.PENDING,
            payment_method=payment_method,
            meta_data=tx_meta,
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
            transaction.external_transaction_id = result.get('transaction_id')
            transaction.save()
            del request.session['enrollment_data']
            return redirect(result['url'])
        else:
            transaction.status = Transaction.Status.FAILED
            transaction.save()
            messages.error(request, _("Impossible de créer le paiement : ") + result.get('error', ''))
            return _err_redirect()

    except Exception as e:
        messages.error(request, _("Erreur lors du traitement du paiement."))
        print(f"Erreur payment plopplop: {e}")
        return _err_redirect()


def enroll_user_free(request, course_id, thinkific_user_id, course_name):
    """Inscrit un utilisateur à un cours gratuit"""
    try:
        from django.utils import timezone as dj_timezone
        import uuid

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

            # Email de confirmation (non bloquant)
            try:
                from payment.email_service import send_enrollment_confirmation
                ref = f"GRATUIT-{activated_at.strftime('%Y%m%d')}-{uuid.uuid4().hex[:8].upper()}"
                site_currency = SiteConfig.get().currency
                send_enrollment_confirmation(
                    user=request.user,
                    course_name=course_name,
                    transaction_number=ref,
                    amount=0,
                    currency=site_currency,
                    payment_method='Accès gratuit',
                    activated_at=activated_at,
                    expiry_date=local_expiry,
                )
            except Exception as e:
                print(f"[KouLakay] Email confirmation cours gratuit échoué : {e}")

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
def bundle_enrollment_step1(request, bundle_id):
    """Étape 1 d'inscription à un bundle (gratuit → direct, payant → payment_options)"""
    if request.method != 'POST':
        return redirect('courses')

    try:
        product_response = thinkific.products.list(limit=100)
        product_items = product_response.get('items', [])
        bundle_product = next(
            (p for p in product_items
             if p.get('productable_id') == bundle_id and p.get('productable_type') == 'Bundle'),
            None
        )
        if not bundle_product:
            messages.error(request, _("Bundle introuvable."))
            return redirect('courses')

        raw_price = bundle_product.get('price')
        bundle_price = Decimal(str(raw_price)) if raw_price else Decimal('0')
        product_id = bundle_product.get('id')

        bundle_info = _fetch_bundle_details(bundle_id)
        bundle_name = bundle_info.get('name', f'Bundle #{bundle_id}')
        course_ids = bundle_info.get('course_ids', [])

        thinkific_user_id = request.user.thinkific_user_id
        if not thinkific_user_id:
            from accounts.views import get_thinkific_user_by_email
            thinkific_user = get_thinkific_user_by_email(request.user.email)
            if thinkific_user:
                thinkific_user_id = thinkific_user.get('id')
                request.user.thinkific_user_id = thinkific_user_id
                request.user.save(update_fields=['thinkific_user_id'])

        if not thinkific_user_id:
            messages.error(request, _("Impossible de trouver votre profil Thinkific."))
            return redirect('courses')

        _sync_user_enrollments(request.user, request)
        enrolled_ids = set(
            Enrollment.objects.filter(user=request.user).values_list('course_id', flat=True)
        )

        if course_ids and all(cid in enrolled_ids for cid in course_ids):
            messages.info(request, _("Vous êtes déjà inscrit à tous les cours de ce bundle."))
            return redirect('mon_apprentissage')

        if bundle_price == 0:
            from django.utils import timezone as dj_timezone
            activated_at = dj_timezone.now()
            local_expiry = activated_at.replace(year=activated_at.year + 10)
            for cid in course_ids:
                if cid in enrolled_ids:
                    continue
                try:
                    thinkific.enrollments.create_enrollment({
                        "course_id": cid,
                        "user_id": thinkific_user_id,
                        "activated_at": activated_at.isoformat(),
                    })
                    Enrollment.objects.get_or_create(
                        user=request.user,
                        thinkific_user_id=thinkific_user_id,
                        course_id=cid,
                        defaults={'activated_at': activated_at, 'expiry_date': local_expiry},
                    )
                except Exception as e:
                    print(f"[bundle_free] Erreur inscription cours {cid}: {e}")
            messages.success(request, f"Vous êtes inscrit au bundle {bundle_name} !")
            return redirect('mon_apprentissage')

        # Payant → page de paiement
        site_currency = SiteConfig.get().currency
        htg_equivalent = None
        if site_currency != 'HTG':
            htg_equivalent = convert_to_htg(bundle_price, site_currency)

        request.session['enrollment_data'] = {
            'is_bundle': True,
            'bundle_id': bundle_id,
            'bundle_name': bundle_name,
            'bundle_price': float(bundle_price),
            'bundle_course_ids': course_ids,
            'product_id': product_id,
            'thinkific_user_id': thinkific_user_id,
        }

        # _fetch_bundle_courses est optionnel : sert uniquement à l'affichage
        # dans le résumé de payment_options. Une erreur ici ne doit pas bloquer le paiement.
        try:
            bundle_courses = _fetch_bundle_courses(bundle_id)
        except Exception as e:
            print(f"[bundle_step1] _fetch_bundle_courses ignoré: {e}")
            bundle_courses = []

        bundle_obj = {
            'name': bundle_name,
            'course_card_image_url': bundle_info.get('bundle_card_image_url') or '',
            'id': bundle_id,
            'courses': bundle_courses,
        }

        return render(request, 'pages/payment_options.html', {
            'course': bundle_obj,
            'is_bundle': True,
            'bundle_courses': bundle_courses,
            'course_price': bundle_price,
            'site_currency': site_currency,
            'htg_equivalent': htg_equivalent,
            'stripe_public_key': settings.STRIPE['PUBLIC_KEY'],
        })

    except Exception as e:
        messages.error(request, _("Une erreur est survenue."))
        print(f"[bundle_enrollment_step1] {e}")
        return redirect('courses')


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

    # ── 1 appel : tous les cours → map id → data (évite N+1) ──
    course_map = {}  # course_id → {name, slug, banner_image_url, description}
    try:
        for c in thinkific.courses.list(limit=100).get('items', []):
            course_map[c['id']] = c
    except Exception:
        pass

    # ── 1 appel : tous les produits → map prix ──
    price_map = {}
    try:
        for p in thinkific.products.list(limit=100).get('items', []):
            cid = p.get('productable_id')
            if cid and p.get('price') is not None:
                price_map[cid] = float(p['price'])
    except Exception:
        pass

    # ── Enrollments Thinkific ──
    if thinkific_user_id:
        try:
            response = thinkific.enrollments.list(user_id=thinkific_user_id, limit=100)
            for item in response.get('items', []):
                course_info = item.get('course') or {}
                course_id   = item.get('course_id') or course_info.get('id')
                if not course_id:
                    continue
                # Enrichissement depuis la course_map (sans appel supplémentaire)
                full        = course_map.get(course_id, {})
                slug        = full.get('slug') or course_info.get('slug', '')
                expiry      = _parse_date(item.get('expiry_date'))
                cours_inscrits.append({
                    'id':                   course_id,
                    'name':                 full.get('name') or course_info.get('name', f'Cours #{course_id}'),
                    'slug':                 slug,
                    'banner_image_url':     full.get('banner_image_url') or full.get('course_card_image_url'),
                    'description':          full.get('description', ''),
                    'price':                price_map.get(course_id),
                    'activated_at':         _parse_date(item.get('activated_at')),
                    'expiry_date':          expiry,
                    'lifetime':             expiry is None,
                    'percentage_completed': round(float(item.get('percentage_completed') or 0) * 100),
                })
        except Exception as e:
            print(f"[mon_apprentissage] Erreur API Thinkific: {e}")

    # ── Fallback : DB locale (si Thinkific indisponible ou pas de thinkific_user_id) ──
    if not cours_inscrits:
        for enrollment in Enrollment.objects.filter(user=request.user).order_by('-activated_at'):
            full  = course_map.get(enrollment.course_id, {})
            slug  = full.get('slug', '')
            cours_inscrits.append({
                'id':                   enrollment.course_id,
                'name':                 full.get('name', f'Cours #{enrollment.course_id}'),
                'slug':                 slug,
                'banner_image_url':     full.get('banner_image_url') or full.get('course_card_image_url'),
                'description':          full.get('description', ''),
                'price':                price_map.get(enrollment.course_id),
                'activated_at':         enrollment.activated_at,
                'expiry_date':          enrollment.expiry_date,
                'lifetime':             False,
                'percentage_completed': 0,
            })

    apply_course_translations(cours_inscrits)

    # ── Bundles achetés ──
    # Construit depuis les transactions COMPLETED avec meta_data['bundle']
    course_to_bundle = {}  # course_id → {'bundle_id', 'bundle_name'}
    mes_bundles = []
    seen_bundle_ids = set()
    try:
        txs = Transaction.objects.filter(user=request.user, status=Transaction.Status.COMPLETED)
        for tx in txs:
            bundle = tx.meta_data.get('bundle')
            if not bundle:
                continue
            bid = bundle.get('bundle_id')
            if not bid or bid in seen_bundle_ids:
                continue
            seen_bundle_ids.add(bid)
            bname = bundle.get('bundle_name', f'Bundle #{bid}')
            bcourse_ids = set(bundle.get('bundle_course_ids', []))
            for cid in bcourse_ids:
                course_to_bundle[cid] = {'bundle_id': bid, 'bundle_name': bname}
            # Cours du bundle qui sont dans les enrollments de l'utilisateur
            bundle_courses = [c for c in cours_inscrits if c['id'] in bcourse_ids]
            total_pct = sum(c.get('percentage_completed', 0) for c in bundle_courses)
            avg_progress = round(total_pct / len(bundle_courses)) if bundle_courses else 0
            mes_bundles.append({
                'id': bid,
                'name': bname,
                'courses': bundle_courses,
                'avg_progress': avg_progress,
                'course_count': len(bcourse_ids),
            })
    except Exception as e:
        print(f"[mon_apprentissage] Erreur bundles: {e}")

    # Annote chaque cours avec le bundle auquel il appartient
    for course in cours_inscrits:
        course['bundle_info'] = course_to_bundle.get(course['id'])

    return render(request, 'pages/mon_apprentissage.html', {
        'cours_inscrits':  cours_inscrits,
        'mes_bundles':     mes_bundles,
        'site_currency':   SiteConfig.get().currency,
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
    
    # Cours masqués par l'admin
    hidden_ids = set(
        CourseVisibility.objects.filter(is_visible=False).values_list('course_id', flat=True)
    )

    # Récupérer les cours populaires (top 6 pour la homepage)
    popular_courses = []
    top_course_ids_queryset = Enrollment.objects.values('course_id') \
                                             .annotate(num_enrollments=Count('course_id')) \
                                             .order_by('-num_enrollments')[:12]

    top_course_ids = [
        item['course_id'] for item in top_course_ids_queryset
        if item['course_id'] not in hidden_ids
    ][:6]

    # Récupérer les détails des produits
    try:
        product_response = thinkific.products.list(limit=100)
        product_items = product_response.get('items', [])
    except Exception:
        product_items = []

    access_map_home = {
        p['productable_id']: _format_access_duration(p.get('days_until_expiry'))
        for p in product_items
        if p.get('productable_id')
    }

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
                raw_price = next(
                    (p['price'] for p in product_items
                     if p.get('productable_id') == course_id and p.get('price') is not None), None
                )
                course_data['price'] = _format_price(raw_price)
                course_data['is_free'] = raw_price is None or float(raw_price) == 0
                course_data['access_duration'] = access_map_home.get(course_id, '6 mois')
                course_data['enroll'] = course_id in enrolled_ids
                popular_courses.append(course_data)
            except Exception as e:
                print(f"Erreur cours populaire {course_id}: {e}")
                continue
    else:
        try:
            for course_data in thinkific.courses.list(limit=100).get('items', []):
                course_id = course_data.get('id')
                if course_id in hidden_ids:
                    continue
                course_data['enrollment_count'] = 0
                raw_price = next(
                    (p['price'] for p in product_items
                     if p.get('productable_id') == course_id and p.get('price') is not None), None
                )
                course_data['price'] = _format_price(raw_price)
                course_data['is_free'] = raw_price is None or float(raw_price) == 0
                course_data['access_duration'] = access_map_home.get(course_id, '6 mois')
                course_data['enroll'] = course_id in enrolled_ids
                popular_courses.append(course_data)
                if len(popular_courses) >= 6:
                    break
        except Exception as e:
            print(f"Erreur cours fallback: {e}")
    
    return render(request, 'pages/home.html', {
        'stats': stats,
        'courses': popular_courses  # Changé de 'popular_courses' à 'courses' pour cohérence
    })


def courses(request):
    """Liste des cours — tous chargés en une passe, filtrage côté client."""
    # Sync Thinkific → DB locale (throttlé à 1x/heure par session)
    if request.user.is_authenticated:
        _sync_user_enrollments(request.user, request)

    try:
        courses_items = thinkific.courses.list(limit=100).get('items', [])
    except Exception:
        courses_items = []

    hidden_ids = set(
        CourseVisibility.objects.filter(is_visible=False).values_list('course_id', flat=True)
    )
    if hidden_ids:
        courses_items = [c for c in courses_items if c.get('id') not in hidden_ids]

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
    access_map = {
        p['productable_id']: _format_access_duration(p.get('days_until_expiry'))
        for p in product_items
        if p.get('productable_id')
    }

    categories = list(CourseCategory.objects.filter(is_active=True).order_by('order', 'name'))
    course_categories_map = {}
    for m in CourseCategoryMembership.objects.select_related('category').filter(category__is_active=True):
        course_categories_map.setdefault(m.course_id, []).append(m.category.slug)

    bundle_categories_map = {}
    for m in BundleCategoryMembership.objects.select_related('category').filter(category__is_active=True):
        bundle_categories_map.setdefault(m.bundle_id, []).append(m.category.slug)

    for c in courses_items:
        cid = c.get('id')
        raw_price = price_map.get(cid)
        c['price'] = _format_price(raw_price)
        c['is_free'] = raw_price is None or float(raw_price) == 0
        c['access_duration'] = access_map.get(cid)  # None = à vie
        c['enroll'] = cid in enrolled_ids
        c['enrollment_count'] = popular_counts.get(cid, 0)
        c['categories'] = course_categories_map.get(cid, [])

    apply_course_translations(courses_items)

    # ── Bundles (depuis Thinkific) ──
    bundles_data = []
    bundle_products = [p for p in product_items if p.get('productable_type') == 'Bundle']
    for bp in bundle_products:
        bundle_id = bp.get('productable_id')
        if not bundle_id or bundle_id in hidden_ids:
            continue
        try:
            bundle_info = _fetch_bundle_details(bundle_id)
            bundle_courses = _fetch_bundle_courses(bundle_id)
            raw_price = bp.get('price')
            course_ids = bundle_info.get('course_ids', [])
            all_enrolled = bool(course_ids) and all(cid in enrolled_ids for cid in course_ids)
            bundles_data.append({
                'id': bundle_id,
                'product_id': bp.get('id'),
                'name': bundle_info.get('name', f'Bundle #{bundle_id}'),
                'image_url': bundle_info.get('bundle_card_image_url') or '',
                'slug': bundle_info.get('slug') or '',
                'price': _format_price(raw_price),
                'is_free': raw_price is None or float(raw_price) == 0,
                'raw_price': float(raw_price) if raw_price is not None else 0,
                'access_duration': _format_access_duration(bp.get('days_until_expiry')),
                'course_ids': course_ids,
                'courses': bundle_courses,
                'all_enrolled': all_enrolled,
                'categories': bundle_categories_map.get(bundle_id, []),
            })
        except Exception as e:
            print(f"[bundle] Erreur bundle {bundle_id}: {e}")

    # ── Groupes de cours ──
    courses_by_id = {c.get('id'): c for c in courses_items}
    course_groups = []
    for group in CourseGroup.objects.filter(is_active=True).prefetch_related('memberships'):
        member_ids = {m.course_id for m in group.memberships.all()}
        group_courses = [courses_by_id[cid] for cid in member_ids if cid in courses_by_id]
        course_groups.append({
            'group': group,
            'courses': group_courses,
            'count': len(group_courses),
        })

    context = {
        'courses': courses_items,
        'bundles': bundles_data,
        'categories': categories,
        'course_groups': course_groups,
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

    # Récupérer le prix et la durée d'accès
    course['price'] = None
    course['access_duration'] = None  # None = accès à vie
    try:
        product_response = thinkific.products.list(limit=100)
        product_items = product_response.get('items', [])

        for p in product_items:
            if p.get('productable_id') == course_id:
                if p.get('price') is not None:
                    course['price'] = _format_price(p['price'])
                course['access_duration'] = _format_access_duration(p.get('days_until_expiry'))
                break
    except Exception as e:
        print(f"Erreur récupération prix: {e}")

    # Vérifier l'inscription (sync Thinkific → DB locale d'abord)
    course['enroll'] = False
    if request.user.is_authenticated:
        _sync_user_enrollments(request.user, request)
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