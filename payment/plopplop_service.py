"""
Service d'intégration avec l'API plopplop.solutionip.app
Supporte MonCash, NatCash, Kashpaw
"""
import requests
from django.conf import settings


class PlopPlopService:
    """Client pour l'API de paiement plopplop"""

    BASE_URL = "https://plopplop.solutionip.app"

    def __init__(self):
        self.client_id = settings.PLOPPLOP['CLIENT_ID']
        self.return_url = settings.PLOPPLOP['RETURN_URL']

    def create_payment(self, refference_id, montant, payment_method='all'):
        """
        Crée une transaction de paiement.

        Args:
            refference_id (str): Référence unique (ex: KOULKY000001)
            montant (float): Montant en gourdes HTG (minimum 20)
            payment_method (str): 'moncash', 'natcash', 'kashpaw' ou 'all'

        Returns:
            dict: { 'success': bool, 'url': str, 'transaction_id': str, 'error': str }
        """


        import math
        payload = {
            'client_id': self.client_id,
            'refference_id': str(refference_id),
            'montant': math.ceil(float(montant)),
            'payment_method': payment_method,
            'return_url': self.return_url,
        }

        try:
            response = requests.post(
                f"{self.BASE_URL}/api/paiement-marchand",
                data=payload,
                timeout=30,
            )
            try:
                data = response.json()
            except Exception:
                response.raise_for_status()
                return {'success': False, 'error': 'Réponse invalide du serveur de paiement'}

            if data.get('status') is True:
                return {
                    'success': True,
                    'url': data.get('url'),
                    'transaction_id': data.get('transaction_id'),
                }
            return {'success': False, 'error': data.get('message', 'Erreur inconnue')}

        except requests.exceptions.Timeout:
            return {'success': False, 'error': 'Délai de connexion dépassé'}
        except requests.exceptions.RequestException as e:
            return {'success': False, 'error': str(e)}

    def verify_payment(self, refference_id):
        """
        Vérifie le statut d'une transaction.

        Args:
            refference_id (str): Référence utilisée lors de la création

        Returns:
            dict: { 'success': bool, 'paid': bool, 'method': str, 'montant': float, ... }
        """
        payload = {
            'client_id': self.client_id,
            'refference_id': str(refference_id),
        }

        try:
            response = requests.post(
                f"{self.BASE_URL}/api/paiement-verify",
                data=payload,
                timeout=30,
            )
            try:
                data = response.json()
            except Exception:
                response.raise_for_status()
                return {'success': False, 'paid': False, 'error': 'Réponse invalide du serveur de paiement'}

            if data.get('status') is True:
                return {
                    'success': True,
                    'paid': data.get('trans_status') == 'ok',
                    'montant': data.get('montant'),
                    'method': data.get('method'),
                    'id_transaction': data.get('id_transaction'),
                    'date': data.get('date'),
                    'heure': data.get('heure'),
                }
            return {'success': False, 'paid': False, 'error': data.get('message', 'Erreur inconnue')}

        except requests.exceptions.Timeout:
            return {'success': False, 'paid': False, 'error': 'Délai de connexion dépassé'}
        except requests.exceptions.RequestException as e:
            return {'success': False, 'paid': False, 'error': str(e)}
