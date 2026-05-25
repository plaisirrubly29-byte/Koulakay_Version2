# courses/urls.py
from django.urls import path
from django.views.generic import RedirectView
from . import views

urlpatterns = [
    path('', RedirectView.as_view(pattern_name='home', permanent=False)),
    path('courses/', views.courses, name='courses'),
    path('course_details/<int:course_id>/', views.course_details, name="course_details"),
    
    # Inscription cours
    path('enrollment/<int:course_id>/', views.course_enrollment_step1, name='course_enrollment'),
    path('enrollment/payment/<str:payment_method>/', views.course_enrollment_payment, name='course_enrollment_payment'),

    # Inscription bundle
    path('bundle/<int:bundle_id>/enroll/', views.bundle_enrollment_step1, name='bundle_enrollment'),

    # Dashboard apprenant
    path('mon-apprentissage/', views.mon_apprentissage, name='mon_apprentissage'),
]