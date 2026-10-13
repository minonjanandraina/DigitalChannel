from django.urls import path

from . import views

app_name = 'reports'

urlpatterns = [
    path('<str:module>/', views.report, name='report'),
    path('<str:module>/export/', views.report_export, name='export'),
]
