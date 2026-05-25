from django.http import JsonResponse, HttpResponseNotAllowed
from django.conf import settings
from django.views.decorators.csrf import csrf_exempt
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.shortcuts import redirect, render
from django.urls import reverse
from datetime import timedelta
import hashlib
import hmac
import json
import requests

from courses.models import Enrollment
from .models import Transaction, Payment
from .plopplop_service import PlopPlopService
from .email_service import send_enrollment_confirmation
from courses.monkey_patch.patch_thinkific import ThinkificExtend as Thinkific

User = get_user_model()
thinkific = Thinkific(settings.THINKIFIC['AUTH_TOKEN'], settings.THINKIFIC['SITE_ID'])


def _verify_thinkific_hmac(request):
    """
    Valide la signature HMAC-SHA256 envoyée par Thinkific dans le header
    X-Thinkific-Hmac-SHA256.
    Retourne True si valide (ou si le header est absent = source non-Thinkific).
    Retourne False si le header est présent mais la signature ne correspond pas.
    """
    signature_header = request.headers.get('X-Thinkific-Hmac-SHA256')
    if not signature_header:
        # Pas un webhook Thinkific — on laisse passer (vérification PlopPlop suit)
        return True

    secret = settings.THINKIFIC.get('SECRET_KEY', '').encode('utf-8')
    expected = hmac.new(secret, request.body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


@csrf_exempt
def confirm(request):
    """
    Webhook de confirmation de paiement (appelé par PlopPlop ou Thinkific).

    Sécurité :
    - Si la requête vient de Thinkific : signature HMAC-SHA256 vérifiée.
    - Dans tous les cas : le statut du paiement est RE-VÉRIFIÉ directement
      chez PlopPlop (on ne fait jamais confiance au payload seul).
    """
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])

    # 1. Valider la signature HMAC si c'est un webhook Thinkific
    if not _verify_thinkific_hmac(request):
        return JsonResponse({'success': False, 'error': 'Invalid HMAC signature'}, status=401)

    try:
        payload = json.loads(request.body)
        transaction_number = payload.get('meta_data', {}).get('transaction_number')
        external_transaction_id = payload.get('external_transaction_id')

        if not transaction_number:
            return JsonResponse({'success': False, 'error': 'Transaction number is required'}, status=400)

        try:
            transaction = Transaction.objects.get(transaction_number=transaction_number)
        except Transaction.DoesNotExist:
            return JsonResponse({'success': False, 'error': 'Transaction not found'}, status=404)

        if transaction.is_completed:
            return JsonResponse({'success': True, 'message': 'Transaction already completed'}, status=200)

        if external_transaction_id:
            transaction.external_transaction_id = external_transaction_id

        # 2. Re-vérifier le statut réel du paiement chez PlopPlop
        #    On n'utilise PAS le statut du payload — un attaquant peut le forger.
        plopplop = PlopPlopService()
        verification = plopplop.verify_payment(transaction_number)

        if not verification.get('success'):
            return JsonResponse({'success': False, 'error': 'Payment verification failed with provider'}, status=502)

        if verification.get('paid'):
            result = process_successful_payment(transaction, payload)
            if result['success']:
                return JsonResponse({'success': True, 'message': 'Enrollment activated'}, status=200)
            return JsonResponse({'success': False, 'error': result['error']}, status=500)

        # Paiement non confirmé chez le fournisseur
        transaction.status = Transaction.Status.FAILED
        transaction.save()
        return JsonResponse({'success': False, 'error': 'Payment not confirmed by provider'}, status=400)

    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid JSON payload'}, status=400)
    except Exception as e:
        return JsonResponse({'success': False, 'error': f'Internal server error: {str(e)}'}, status=500)


def process_successful_payment(transaction, payload):
    """
    Traite un paiement réussi (cours ou bundle).
    Retourne un dict {'success': bool, 'error': str, 'course_id': int|None, 'bundle_id': int|None}.
    N'interrompt JAMAIS l'enrollment à cause d'un échec de l'External Order.
    """
    try:
        meta_data = transaction.meta_data
        user_data = meta_data.get('user', {})
        bundle_data = meta_data.get('bundle')
        course_data = meta_data.get('course', {})

        user_id           = user_data.get('id')
        thinkific_user_id = user_data.get('thinkific_user_id')

        if not all([user_id, thinkific_user_id]):
            return {'success': False, 'error': 'Missing user data in transaction metadata', 'course_id': None, 'bundle_id': None}

        try:
            user = User.objects.get(pk=user_id)
        except User.DoesNotExist:
            return {'success': False, 'error': 'User not found', 'course_id': None, 'bundle_id': None}

        activated_at = timezone.now()
        expiry_date  = activated_at.replace(year=activated_at.year + 10)

        if bundle_data:
            # ── Bundle : inscrire à tous les cours ──
            bundle_id        = bundle_data.get('bundle_id')
            bundle_name      = bundle_data.get('bundle_name', f'Bundle #{bundle_id}')
            bundle_course_ids = bundle_data.get('bundle_course_ids', [])
            product_id       = bundle_data.get('product_id')

            if not bundle_course_ids:
                return {'success': False, 'error': 'No course IDs in bundle metadata', 'course_id': None, 'bundle_id': bundle_id}

            order_ok = create_thinkific_external_order(transaction, thinkific_user_id, product_id or bundle_id)
            if not order_ok:
                print(f"[KouLakay] External Order échoué pour bundle {transaction.transaction_number}")

            first_enrollment = None
            for cid in bundle_course_ids:
                try:
                    thinkific.enrollments.create_enrollment({
                        'user_id':      thinkific_user_id,
                        'course_id':    cid,
                        'activated_at': activated_at.isoformat(),
                    })
                    enrollment, _ = Enrollment.objects.get_or_create(
                        user=user,
                        thinkific_user_id=thinkific_user_id,
                        course_id=cid,
                        defaults={'activated_at': activated_at, 'expiry_date': expiry_date},
                    )
                    if first_enrollment is None:
                        first_enrollment = enrollment
                except Exception as e:
                    print(f"[bundle] Erreur inscription cours {cid}: {e}")

            if first_enrollment is None:
                transaction.status = Transaction.Status.FAILED
                transaction.save()
                return {'success': False, 'error': 'Tous les enrollments bundle ont échoué', 'course_id': None, 'bundle_id': bundle_id}

            Payment.objects.get_or_create(user=user, enrollment=first_enrollment, transaction=transaction)
            transaction.status       = Transaction.Status.COMPLETED
            transaction.completed_at = timezone.now()
            transaction.save()

            try:
                send_enrollment_confirmation(
                    user=user,
                    course_name=bundle_name,
                    transaction_number=transaction.transaction_number,
                    amount=transaction.price,
                    currency=transaction.currency,
                    payment_method=transaction.payment_method or 'mobile',
                    activated_at=activated_at,
                    expiry_date=None,
                )
            except Exception as e:
                print(f"[KouLakay] Email confirmation bundle échoué : {e}")

            return {'success': True, 'course_id': None, 'bundle_id': bundle_id, 'enrollment_id': first_enrollment.id}

        else:
            # ── Cours unique (logique existante) ──
            course_id  = course_data.get('course_id')
            product_id = course_data.get('product_id')

            if not course_id:
                return {'success': False, 'error': 'Missing course_id in transaction metadata', 'course_id': None, 'bundle_id': None}

            order_ok = create_thinkific_external_order(transaction, thinkific_user_id, product_id or course_id)
            if not order_ok:
                print(f"[KouLakay] External Order échoué pour transaction {transaction.transaction_number}")

            try:
                thinkific.enrollments.create_enrollment({
                    'user_id':      thinkific_user_id,
                    'course_id':    course_id,
                    'activated_at': activated_at.isoformat(),
                })
            except Exception as e:
                transaction.status = Transaction.Status.FAILED
                transaction.save()
                return {'success': False, 'error': f'Enrollment Thinkific échoué: {e}', 'course_id': course_id, 'bundle_id': None}

            enrollment, _ = Enrollment.objects.get_or_create(
                user=user,
                thinkific_user_id=thinkific_user_id,
                course_id=course_id,
                defaults={'activated_at': activated_at, 'expiry_date': expiry_date},
            )

            Payment.objects.get_or_create(user=user, enrollment=enrollment, transaction=transaction)
            transaction.status       = Transaction.Status.COMPLETED
            transaction.completed_at = timezone.now()
            transaction.save()

            try:
                send_enrollment_confirmation(
                    user=user,
                    course_name=course_data.get('course_name') or course_data.get('name') or f'Cours #{course_id}',
                    transaction_number=transaction.transaction_number,
                    amount=transaction.price,
                    currency=transaction.currency,
                    payment_method=transaction.payment_method or 'mobile',
                    activated_at=activated_at,
                    expiry_date=None,
                )
            except Exception as e:
                print(f"[KouLakay] Email confirmation échoué : {e}")

            return {'success': True, 'course_id': course_id, 'bundle_id': None, 'enrollment_id': enrollment.id}

    except Exception as e:
        transaction.status = Transaction.Status.FAILED
        transaction.save()
        return {'success': False, 'error': str(e), 'course_id': None, 'bundle_id': None}


def create_thinkific_external_order(transaction, thinkific_user_id, product_id):
    """
    Crée un External Order dans Thinkific via l'API
    Retourne True si succès, False sinon
    """
    try:
        api_url = "https://api.thinkific.com/api/public/v1/external_orders"
        headers = {
            "Authorization": f"Bearer {settings.THINKIFIC['AUTH_TOKEN']}",
            "X-Auth-Subdomain": settings.THINKIFIC['SITE_ID'],
            "Content-Type": "application/json"
        }
        
        # Construire le payload
        order_data = {
            "payment_provider": transaction.get_payment_method_display(),
            "user_id": thinkific_user_id,
            "product_id": product_id,
            "order_type": "one-time",
            "transaction": {
                "amount": int(float(transaction.price) * 100),  # Montant en cents
                "currency": transaction.currency,
                "reference": transaction.transaction_number,
                "action": "purchase"
            }
        }
        
        # Faire la requête
        response = requests.post(api_url, headers=headers, json=order_data)
        response.raise_for_status()
        
        # Récupérer l'ID de l'External Order
        response_data = response.json()
        external_order_id = response_data.get('id')
        
        if external_order_id:
            transaction.thinkific_external_order_id = external_order_id
            transaction.save()
            return True
        
        return False
        
    except requests.exceptions.RequestException as e:
        print(f"Erreur création External Order Thinkific: {e}")
        return False
    except Exception as e:
        print(f"Erreur inattendue External Order: {e}")
        return False


@csrf_exempt
def refund_transaction(request, transaction_number):
    """
    Endpoint pour rembourser une transaction
    """
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])

    try:
        # Récupérer la transaction
        try:
            transaction = Transaction.objects.get(transaction_number=transaction_number)
        except Transaction.DoesNotExist:
            return JsonResponse({
                'success': False,
                'error': 'Transaction not found'
            }, status=404)

        # Vérifier si remboursable
        if not transaction.is_refundable:
            return JsonResponse({
                'success': False,
                'error': 'Transaction cannot be refunded'
            }, status=400)

        # Parser les données de remboursement
        payload = json.loads(request.body)
        refund_amount = payload.get('amount', float(transaction.price))
        refund_reason = payload.get('reason', 'Customer request')

        # Créer le remboursement dans Thinkific si External Order existe
        if transaction.thinkific_external_order_id:
            refund_created = create_thinkific_refund(
                transaction.thinkific_external_order_id,
                refund_amount,
                transaction.currency,
                transaction.transaction_number
            )
            
            if not refund_created:
                return JsonResponse({
                    'success': False,
                    'error': 'Failed to create refund in Thinkific'
                }, status=500)

        # Mettre à jour la transaction
        transaction.status = Transaction.Status.REFUNDED
        transaction.meta_data['refund'] = {
            'amount': refund_amount,
            'reason': refund_reason,
            'refunded_at': timezone.now().isoformat()
        }
        transaction.save()

        # Désactiver l'enrollment si nécessaire
        try:
            payment = Payment.objects.get(transaction=transaction)
            enrollment = payment.enrollment
            # Vous pouvez ici désactiver l'enrollment dans Thinkific
            # thinkific.enrollments.delete_enrollment(enrollment_id)
        except Payment.DoesNotExist:
            pass

        return JsonResponse({
            'success': True,
            'message': 'Transaction refunded successfully',
            'data': {
                'transaction_number': transaction.transaction_number,
                'refund_amount': refund_amount
            }
        }, status=200)

    except json.JSONDecodeError:
        return JsonResponse({
            'success': False,
            'error': 'Invalid JSON payload'
        }, status=400)
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': f'Error processing refund: {str(e)}'
        }, status=500)


def create_thinkific_refund(external_order_id, amount, currency, reference):
    """
    Crée un remboursement dans Thinkific
    """
    try:
        api_url = f"https://api.thinkific.com/api/public/v1/external_orders/{external_order_id}/transactions/refund"
        headers = {
            "Authorization": f"Bearer {settings.THINKIFIC['AUTH_TOKEN']}",
            "X-Auth-Subdomain": settings.THINKIFIC['SITE_ID'],
            "Content-Type": "application/json"
        }
        
        refund_data = {
            "amount": int(float(amount) * 100),  # Montant en cents
            "currency": currency,
            "reference": f"REFUND-{reference}",
            "action": "refund"
        }
        
        response = requests.post(api_url, headers=headers, json=refund_data)
        response.raise_for_status()
        
        return True
        
    except requests.exceptions.RequestException as e:
        print(f"Erreur création remboursement Thinkific: {e}")
        return False
    except Exception as e:
        print(f"Erreur inattendue remboursement: {e}")
        return False





def payment_return(request):
    """
    Vue de retour après paiement plopplop.
    L'utilisateur est redirigé ici après avoir payé (ou annulé) sur plopplop.
    On vérifie le statut via /api/paiement-verify, puis on traite l'inscription.
    """
    # plopplop peut transmettre la référence en GET ou POST
    refference_id = request.GET.get('refference_id') or request.POST.get('refference_id')

    if not refference_id:
        messages.error(request, "Référence de paiement introuvable.")
        return redirect('courses')

    try:
        transaction = Transaction.objects.get(transaction_number=refference_id)
    except Transaction.DoesNotExist:
        messages.error(request, "Transaction introuvable.")
        return redirect('courses')

    # Si déjà traitée, ne pas re-traiter
    if transaction.is_completed:
        messages.info(request, "Ce paiement a déjà été traité.")
        course_id = transaction.course_id
        if course_id:
            return redirect('course_details', course_id=course_id)
        return redirect('courses')

    # Vérifier le statut sur plopplop
    plopplop = PlopPlopService()
    result = plopplop.verify_payment(refference_id)

    course_id = transaction.course_id

    if result.get('paid'):
        proc = process_successful_payment(transaction, {})
        if proc['success']:
            bundle_data = transaction.meta_data.get('bundle', {})
            course_data = transaction.meta_data.get('course', {})
            item_name = (bundle_data.get('bundle_name')
                         or course_data.get('course_name')
                         or course_data.get('name'))
            request.session['success_context'] = {
                'course_name': item_name,
                'course_id': proc.get('course_id'),
            }
            messages.success(request, "Paiement confirmé ! Votre accès est activé. Un email de confirmation vous a été envoyé.")
            return redirect('success_page')
        messages.error(request, f"Erreur lors de l'activation : {proc.get('error', 'Inconnue')}")
        course_id = proc.get('course_id') or transaction.course_id
        return redirect('course_details', course_id=course_id) if course_id else redirect('courses')
    elif not result.get('success'):
        messages.error(request, f"Erreur de vérification : {result.get('error', 'Inconnue')}")
        return redirect('course_details', course_id=course_id) if course_id else redirect('courses')
    else:
        # Paiement en cours ou annulé
        messages.warning(request, "Votre paiement n'a pas encore été confirmé. Veuillez réessayer dans quelques instants.")
        return redirect('course_details', course_id=course_id) if course_id else redirect('courses')


# ── Stripe Elements ────────────────────────────────────────────────────────────

@login_required
def stripe_checkout(request):
    """Page de paiement avec Stripe Elements."""
    transaction_number = request.session.get('stripe_transaction_number')
    if not transaction_number:
        messages.error(request, _("Session expirée. Veuillez recommencer."))
        return redirect('courses')

    try:
        transaction = Transaction.objects.get(
            transaction_number=transaction_number,
            user=request.user,
            status=Transaction.Status.PENDING,
        )
    except Transaction.DoesNotExist:
        messages.error(request, _("Transaction introuvable."))
        return redirect('courses')

    success_url = request.build_absolute_uri(reverse('payment:stripe_success'))

    return render(request, 'pages/stripe_checkout.html', {
        'transaction': transaction,
        'stripe_public_key': settings.STRIPE['PUBLIC_KEY'],
        'course_name': transaction.course_name,
        'success_url': success_url,
    })


@login_required
def stripe_create_intent(request):
    """AJAX POST — crée un PaymentIntent et retourne le client_secret."""
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)

    transaction_number = request.session.get('stripe_transaction_number')
    if not transaction_number:
        return JsonResponse({'error': 'Session expirée'}, status=400)

    try:
        transaction = Transaction.objects.get(
            transaction_number=transaction_number,
            user=request.user,
            status=Transaction.Status.PENDING,
        )
    except Transaction.DoesNotExist:
        return JsonResponse({'error': 'Transaction introuvable'}, status=404)

    # Si un PaymentIntent existe déjà (rechargement de page), on le réutilise
    if transaction.external_transaction_id and transaction.external_transaction_id.startswith('pi_'):
        from .stripe_service import StripeService
        stripe_svc = StripeService()
        result = stripe_svc.retrieve_payment_intent(transaction.external_transaction_id)
        if result['success'] and result['status'] in ('requires_payment_method', 'requires_confirmation', 'requires_action'):
            import stripe as _stripe
            _stripe.api_key = settings.STRIPE['SECRET_KEY']
            intent = _stripe.PaymentIntent.retrieve(transaction.external_transaction_id)
            return JsonResponse({'client_secret': intent.client_secret})

    from .stripe_service import StripeService
    stripe_svc = StripeService()
    result = stripe_svc.create_payment_intent(
        amount_usd=float(transaction.price),
        transaction_number=transaction.transaction_number,
        metadata={
            'course_id': str(transaction.meta_data.get('course', {}).get('course_id', '')),
            'user_email': request.user.email,
        },
    )

    if result['success']:
        transaction.external_transaction_id = result['payment_intent_id']
        transaction.save()
        return JsonResponse({'client_secret': result['client_secret']})

    return JsonResponse({'error': result['error']}, status=500)


@login_required
def stripe_success(request):
    """
    Page de retour après confirmation Stripe.
    Stripe ajoute ?payment_intent=pi_xxx&redirect_status=succeeded
    """
    payment_intent_id = request.GET.get('payment_intent')
    redirect_status = request.GET.get('redirect_status')

    if not payment_intent_id or redirect_status != 'succeeded':
        messages.error(request, _("Paiement non confirmé."))
        return redirect('courses')

    try:
        transaction = Transaction.objects.get(
            external_transaction_id=payment_intent_id,
            user=request.user,
        )
    except Transaction.DoesNotExist:
        messages.error(request, _("Transaction introuvable."))
        return redirect('courses')

    if transaction.is_completed:
        messages.info(request, _("Ce paiement a déjà été traité."))
        return redirect('success_page')

    # Vérifier côté Stripe (ne jamais faire confiance au GET seul)
    from .stripe_service import StripeService
    stripe_svc = StripeService()
    result = stripe_svc.retrieve_payment_intent(payment_intent_id)

    if result.get('success') and result.get('status') == 'succeeded':
        proc = process_successful_payment(transaction, {})
        request.session.pop('stripe_transaction_number', None)
        if proc['success']:
            bundle_data = transaction.meta_data.get('bundle', {})
            course_data = transaction.meta_data.get('course', {})
            item_name = (bundle_data.get('bundle_name')
                         or course_data.get('course_name')
                         or course_data.get('name'))
            request.session['success_context'] = {
                'course_name': item_name,
                'course_id': proc.get('course_id'),
            }
            messages.success(request, _("Paiement confirmé ! Votre accès est activé."))
            return redirect('success_page')
        messages.error(request, _(f"Erreur lors de l'activation : {proc.get('error', 'Inconnue')}"))
        course_id = proc.get('course_id') or transaction.course_id
        return redirect('course_details', course_id=course_id) if course_id else redirect('courses')

    messages.error(request, _("Le paiement n'a pas pu être confirmé. Veuillez réessayer."))
    course_id = transaction.course_id
    return redirect('course_details', course_id=course_id) if course_id else redirect('courses')


@login_required
def stripe_init_inline(request):
    """
    AJAX POST — crée Transaction + PaymentIntent depuis enrollment_data de session.
    Utilisé pour le paiement Stripe inline directement dans payment_options.html.
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)

    enrollment_data = request.session.get('enrollment_data')
    if not enrollment_data:
        return JsonResponse({'error': 'Session expirée'}, status=400)

    # Réutiliser transaction + PI existants si dispo (rechargement de page)
    tx_number = request.session.get('stripe_transaction_number')
    if tx_number:
        try:
            tx = Transaction.objects.get(
                transaction_number=tx_number,
                user=request.user,
                status=Transaction.Status.PENDING,
            )
            if tx.external_transaction_id and tx.external_transaction_id.startswith('pi_'):
                import stripe as _stripe
                _stripe.api_key = settings.STRIPE['SECRET_KEY']
                intent = _stripe.PaymentIntent.retrieve(tx.external_transaction_id)
                if intent.status in ('requires_payment_method', 'requires_confirmation', 'requires_action'):
                    return JsonResponse({
                        'client_secret': intent.client_secret,
                        'success_url': request.build_absolute_uri(reverse('payment:stripe_success')),
                    })
        except Transaction.DoesNotExist:
            pass

    from decimal import Decimal
    from pages.models import SiteConfig
    from payment.exchange_service import convert_to_htg, get_htg_rate

    is_bundle = enrollment_data.get('is_bundle', False)
    product_id = enrollment_data.get('product_id')
    thinkific_user_id = enrollment_data['thinkific_user_id']

    if is_bundle:
        item_price = Decimal(str(enrollment_data['bundle_price']))
        meta_data = {
            "bundle": {
                "bundle_id": enrollment_data['bundle_id'],
                "bundle_name": enrollment_data['bundle_name'],
                "bundle_course_ids": enrollment_data['bundle_course_ids'],
                "product_id": product_id,
            },
            "user": {"id": request.user.pk, "email": request.user.email, "thinkific_user_id": thinkific_user_id},
        }
        stripe_metadata_id = str(enrollment_data['bundle_id'])
    else:
        item_price = Decimal(str(enrollment_data['course_price']))
        meta_data = {
            "course": {
                "course_id": enrollment_data['course_id'],
                "course_name": enrollment_data['course_name'],
                "product_id": product_id,
            },
            "user": {"id": request.user.pk, "email": request.user.email, "thinkific_user_id": thinkific_user_id},
        }
        stripe_metadata_id = str(enrollment_data['course_id'])

    site_currency = SiteConfig.get().currency
    if site_currency == 'USD':
        montant_usd = float(item_price)
    else:
        htg_amount = convert_to_htg(item_price, site_currency)
        if htg_amount:
            usd_rate = get_htg_rate('USD')
            montant_usd = round(htg_amount / usd_rate, 2) if usd_rate else float(item_price)
        else:
            montant_usd = float(item_price)

    tx = Transaction.objects.create(
        user=request.user,
        price=Decimal(str(montant_usd)),
        currency=Transaction.Currencies.USD,
        status=Transaction.Status.PENDING,
        payment_method='credit_card',
        meta_data=meta_data,
    )

    from .stripe_service import StripeService
    stripe_svc = StripeService()
    result = stripe_svc.create_payment_intent(
        amount_usd=montant_usd,
        transaction_number=tx.transaction_number,
        metadata={'item_id': stripe_metadata_id, 'user_email': request.user.email},
    )

    if not result['success']:
        tx.delete()
        return JsonResponse({'error': result['error']}, status=500)

    tx.external_transaction_id = result['payment_intent_id']
    tx.save(update_fields=['external_transaction_id'])
    request.session['stripe_transaction_number'] = tx.transaction_number

    return JsonResponse({
        'client_secret': result['client_secret'],
        'success_url': request.build_absolute_uri(reverse('payment:stripe_success')),
    })


@csrf_exempt
def stripe_webhook(request):
    """
    Webhook Stripe — backup pour payment_intent.succeeded.
    URL fixe hors i18n : /payment/webhook/stripe/
    """
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])

    sig_header = request.headers.get('Stripe-Signature')
    if not sig_header:
        return JsonResponse({'error': 'Missing Stripe-Signature header'}, status=400)

    from .stripe_service import StripeService
    stripe_svc = StripeService()
    result = stripe_svc.construct_webhook_event(request.body, sig_header)

    if not result['success']:
        return JsonResponse({'error': result['error']}, status=400)

    event = result['event']

    if event['type'] == 'payment_intent.succeeded':
        payment_intent = event['data']['object']
        transaction_number = payment_intent.get('metadata', {}).get('transaction_number')

        if not transaction_number:
            return JsonResponse({'error': 'Missing transaction_number in metadata'}, status=400)

        try:
            transaction = Transaction.objects.get(transaction_number=transaction_number)
        except Transaction.DoesNotExist:
            return JsonResponse({'error': 'Transaction not found'}, status=404)

        if not transaction.is_completed:
            proc = process_successful_payment(transaction, {})
            if not proc['success']:
                print(f"[KouLakay] Webhook: enrollment échoué pour {transaction_number}: {proc['error']}")

    return JsonResponse({'received': True})
