# patch_thinkific.py

import json
import requests as _requests
from thinkific.client import Client
from thinkific.utils import mergeURL, BASE_URL, ADMIN_API_URL, WEBHOOKS_API_URL
from thinkific.users import Users
from thinkific.promotions import Promotions
from thinkific.enrollments import Enrollments
from thinkific.chapters import Chapters
from thinkific.courses import Courses
from thinkific.coupons import Coupons
from thinkific.contents import Contents
from thinkific.products import Products
from thinkific.bundles import Bundles
from thinkific.course_reviews import CourseReviews
from thinkific.webhooks import Webhooks
from .instructor import Instructor
from .collection import Collection


class BearerClient(Client):
    """
    Thinkific's new JWT API tokens require 'Authorization: Bearer'
    instead of the legacy 'X-Auth-API-Key' header used by thinkific==0.1.1.
    """
    def __init__(self, api_key, subdomain, headers={}):
        super().__init__(api_key, subdomain, headers)
        self._api_key = api_key
        self._subdomain = subdomain

    def request(self, method=None, url=None, data=None, params=None, api='Admin'):
        headers = {
            'Authorization': f'Bearer {self._api_key}',
            'X-Auth-Subdomain': self._subdomain,
            'Content-Type': 'application/json',
        }
        if data:
            data = json.dumps(data)

        if api == 'Admin':
            full_url = mergeURL(BASE_URL + ADMIN_API_URL + url, params)
        elif api == 'Webhooks':
            full_url = mergeURL(BASE_URL + WEBHOOKS_API_URL + url, params)
        else:
            return 'API provided is not available'

        result = _requests.request(method=method, url=full_url, data=data, headers=headers)

        try:
            body = json.loads(result.text)
        except Exception:
            body = {}
        if result.status_code >= 400:
            raise Exception(result.status_code)
        return body


class ThinkificExtend:
    """
    Remplace Thinkific() en utilisant BearerClient directement.
    Ne fait PAS appel à super().__init__() pour éviter que la lib
    crée des Client legacy avec X-Auth-API-Key (HTTP 401 avec JWT tokens).
    """
    def __init__(self, api_key, subdomain):
        client = BearerClient(api_key, subdomain)
        self.__users = Users(client)
        self.__promotions = Promotions(client)
        self.__enrollments = Enrollments(client)
        self.__chapters = Chapters(client)
        self.__courses = Courses(client)
        self.__coupons = Coupons(client)
        self.__contents = Contents(client)
        self.__products = Products(client)
        self.__bundles = Bundles(client)
        self.__course_reviews = CourseReviews(client)
        self.__webhooks = Webhooks(client)
        self.__instructors = Instructor(client)
        self.__collections = Collection(client)

    @property
    def users(self): return self.__users

    @property
    def promotions(self): return self.__promotions

    @property
    def enrollments(self): return self.__enrollments

    @property
    def chapters(self): return self.__chapters

    @property
    def courses(self): return self.__courses

    @property
    def coupons(self): return self.__coupons

    @property
    def contents(self): return self.__contents

    @property
    def products(self): return self.__products

    @property
    def bundles(self): return self.__bundles

    @property
    def course_reviews(self): return self.__course_reviews

    @property
    def webhooks(self): return self.__webhooks

    @property
    def instructors(self): return self.__instructors

    @property
    def collections(self): return self.__collections
